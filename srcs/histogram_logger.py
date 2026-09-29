#!/usr/bin/env python3
"""
histogram_logger.py - LeCroy X-Stream histogram logger that writes HDF5 directly.

Same job as histogram_logger.vbs (clear sweeps, wait INTERVAL_MINUTES, read the
histogram functions in FUNC_NAMES), but each snapshot is appended as one row to
an HDF5 file instead of becoming its own text file:

    FILE_PERIOD = "day"    ->  C:\\Histograms\\2026_Sep\\2026_Sep_06.h5   (24 rows)
    FILE_PERIOD = "month"  ->  C:\\Histograms\\2026_Sep.h5              (~720 rows)

The file layout matches srcs/lecroy_hist.py (pack_hdf5, hdf5_histogram, to_th1),
which reads these files back. The VBScript this replaces is
legacy/histogram_logger.vbs, and srcs/show_mapping.py prints which parameter
each function histograms before a run. Layout:

    /timestamp        (N,)         "YYYY-MM-DDTHH:MM:SS"  (end of the accumulation)
    /duration_s       (N,)         float64, seconds accumulated (clear -> start of readout)
    /read_s           (N,)         float64, how long that readout itself took
    /F1/counts        (N, nBins)   uint64, one gzip+shuffle chunk per row, fletcher32 checksum
    /F1/available     (N,)         bool
    /F1/bin_width     (N,)         float64   (NaN where unavailable)
    /F1/offset        (N,)         float64
    /F1/first_bin     (N,)         int32     (-1 where unavailable)
    /F1/last_bin      (N,)         int32
    /F1/n_bins        (N,)         int32     length of the scope's BinPopulations
    centre of scope bin j = offset + bin_width*(j + shift), shift=0 as in the old logger

If the HDF5 append fails for any reason, the snapshot is written as a v2 text
file next to it (<name>.rescue_HHMM.csv) so nothing is lost; if that folder is
unwritable too, the log folder and the OS temp folder are tried in turn.

Requirements on the scope PC (Python 3.8 is the last version that runs on
Windows 7):   pip install pywin32 h5py numpy

Deployed to C:\\Scripts\\histogram_logger.py on the scope PCs; see docs/RUNNING.md.
Start (no console):    pythonw C:\\Scripts\\histogram_logger.py   (status -> LOG_FILE only)
Start (with console):  python  C:\\Scripts\\histogram_logger.py
Find it / its pid:     tasklist /fi "imagename eq pythonw.exe"
Stop it:               taskkill /f /pid <pid>                     (repeat /pid for several)
Read the log:          type C:\\Histograms\\logger.log

A console window can freeze the script: clicking in a Command Prompt with QuickEdit
enabled blocks the process on its next write to stdout until a key is pressed. Running
under pythonw.exe (or with output redirected to a file) removes that failure mode.
"""
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta

import h5py
import numpy as np

# ---------------- settings ----------------
BASE_FOLDER = r"C:\Histograms"
INTERVAL_MINUTES = 60
FUNC_NAMES = ["F1", "F2", "F3", "F4", "F5"]     # add "F6", "F7", "F8" here if needed
FILE_PERIOD = "day"                                    # "day" or "month"
ALIGN_TO_CLOCK = False
POLL_SLEEP_S = 0.5
POLL_MAX_TRIES = 40                                    # 40 x 0.5 s = 20 s max wait per function
GZIP_LEVEL = 6
LOG_FILE = r"C:\Histograms\logger.log"   # every status line is appended here as well
MAX_LOG_MB = 5                              # rolled to <name>.1 past this size
CONNECT_RETRIES = 6                         # startup only: the scope app may still be booting
CONNECT_RETRY_S = 10.0
RECONNECT_AFTER_DEAD_CYCLES = 3             # snapshots with no histograms at all before reattaching

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
scope = None       # set by connect()
_verified = False  # has the "clearing really clears" check run against this scope object?


def say(msg: str) -> None:
    """Print to the console if there is one, and always append to LOG_FILE.

    Under pythonw.exe there is no console and sys.stdout is None, so printing is
    skipped; the log file is then the only record. Logging never raises: a failure
    to write the log must not stop the acquisition.
    """
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:  # noqa: BLE001 - a blocked or closed console must not stop the loop
        pass
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_MB * 1024 * 1024:
            old = LOG_FILE + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(LOG_FILE, old)
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------- scope access ----------------
def connect(retries: int = 0, delay_s: float = CONNECT_RETRY_S):
    """Attach to the running scope application (same COM object the VBScript used).

    `retries` is for startup: launched from a scheduled task at boot, this can win
    the race against the X-Stream application and would otherwise die immediately.
    Mid-run reconnects pass 0 and let the main loop try again on the next cycle.
    """
    global scope, _verified
    import win32com.client
    for attempt in range(retries + 1):
        try:
            scope = win32com.client.Dispatch("LeCroy.XStreamDSO")
            # a fresh COM object may be a different firmware/app instance, so the
            # "clearing really clears" check has to run again against it
            _verified = False
            return scope
        except Exception:  # noqa: BLE001 - retried below, re-raised on the last attempt
            if attempt == retries:
                raise
            say(f"connect failed (attempt {attempt + 1}/{retries + 1}); "
                f"retrying in {delay_s:.0f}s")
            time.sleep(delay_s)


def _sweep_count():
    """Total population of the first available function - used to check a clear worked."""
    for name in FUNC_NAMES:
        try:
            return float(np.asarray(scope.Math.Functions(name).Out.Result.BinPopulations,
                                    dtype=float).sum())
        except Exception:  # noqa: BLE001
            continue
    return None


def clear_sweeps() -> str:
    """Clear the accumulated sweeps. Returns the method used, or "" if it failed.

    XStream models this as an action object, so it is ClearSweeps.ActNow() - plain
    ClearSweeps() raises DISP_E_MEMBERNOTFOUND. Verified once at startup by checking
    that the histogram population actually drops: on this scope
    app.Acquisition.ClearSweeps.ActNow() returned cleanly while clearing nothing,
    so "did not raise" is not proof that it worked. If the firmware changes and
    this warning comes back, the clear is not doing what the log claims.
    """
    global _verified
    before = None if _verified else _sweep_count()
    try:
        scope.ClearSweeps.ActNow()
    except Exception as e:  # noqa: BLE001
        say(f"ClearSweeps.ActNow() failed: {e}")
        return ""
    if not _verified:
        time.sleep(1.0)
        after = _sweep_count()
        if before and after is not None and after > 0.5 * before:
            say(f"WARNING: sweeps did not drop ({before:.0f} -> {after:.0f}) - the histograms "
                "may not be resetting, so snapshots would accumulate.")
        _verified = True
    return "app.ClearSweeps.ActNow()"


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def num(x) -> float:
    """Number from a COM value that may arrive as a string with units ('1.5ns')."""
    if isinstance(x, (int, float)):
        return float(x)
    m = _NUM.search(str(x))
    return float(m.group()) if m else 0.0


def _com_str(obj, attr: str) -> str:
    """str() of obj.attr, or "" if this firmware has no such property.

    Most XStream properties are control objects wrapping the value rather than
    the value itself, hence the .Value unwrap.
    """
    try:
        value = getattr(obj, attr)
        value = getattr(value, "Value", value)
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - absent properties are the normal case here
        return ""
    return "" if text.lower() in ("", "none", "undefined") else text


_descriptions = {}


def describe_function(name: str) -> str:
    """What math function `name` computes, as a short human string ("" if unknown).

    Which property holds this depends on the firmware, so try the known ones in
    turn and settle for "" rather than guessing. Recorded into the HDF5 file so
    that data read back months later still says which parameter it came from,
    and printed by show_mapping.py. Cached: it only changes when someone
    reconfigures the scope, and it is otherwise one COM traversal per snapshot.
    """
    if name in _descriptions:
        return _descriptions[name]
    text = ""
    try:
        f = scope.Math.Functions(name)
        equation = _com_str(f, "Equation")
        if equation:
            text = equation
        else:
            operator = _com_str(f, "Operator1Name") or _com_str(f, "ProcessorName")
            source = _com_str(f, "Source1")
            text = f"{operator}({source})" if operator and source else operator
    except Exception:  # noqa: BLE001 - never worth failing a snapshot over
        pass
    _descriptions[name] = text
    return text


def axis_units(h) -> str:
    """Units of the quantity a histogram result is binning ("" if the scope is silent)."""
    for attr in ("HorizontalUnits", "HorUnits", "XAxisUnits"):
        units = _com_str(h, attr)
        if units:
            return units
    return ""


def poll_histogram(name):
    """Result object of math function `name`, or None if not ready within the timeout.

    The fast test is LastPopulatedBin > FirstPopulatedBin, which costs two scalar
    COM reads. A histogram with exactly one populated bin never satisfies it, so
    rather than throw such a snapshot away we pay for one full BinPopulations read
    once the timeout expires and accept the function if anything landed in it at
    all. Low-rate channels early in a run sit in exactly that state.
    """
    h = None
    for _ in range(POLL_MAX_TRIES):
        try:
            h = scope.Math.Functions(name).Out.Result
            if num(h.LastPopulatedBin) > num(h.FirstPopulatedBin):
                return h
        except Exception:  # noqa: BLE001 - function off / not a histogram yet
            h = None
        time.sleep(POLL_SLEEP_S)
    if h is not None:
        try:
            if np.asarray(h.BinPopulations, dtype=float).sum() > 0:
                return h
        except Exception:  # noqa: BLE001
            pass
    return None


_last_read_times = {}          # name -> seconds spent in the last read_histogram call


def read_histogram(name):
    """dict(width, offset, first, last, counts[full axis]) or None if unavailable."""
    t0 = time.time()
    try:
        return _read_histogram(name)
    finally:
        _last_read_times[name] = time.time() - t0


def _sanitise(counts: np.ndarray, name: str) -> np.ndarray:
    """Zero out NaN/inf and negative bins before they can be cast to uint64.

    A -1.0 cast straight to uint64 becomes 1.8e19 and carries a perfectly valid
    fletcher32 checksum, so nothing downstream would ever flag it. Bin populations
    are counts and cannot legitimately be negative; if they are, the function is
    not configured as a plain histogram and the value is not trustworthy anyway.
    """
    bad = ~np.isfinite(counts)
    if bad.any():
        counts = np.where(bad, 0.0, counts)
    neg = counts < 0
    if neg.any():
        counts = np.where(neg, 0.0, counts)
    if bad.any() or neg.any():
        say(f"WARNING: {name} returned {int(bad.sum())} non-finite and {int(neg.sum())} "
            "negative bin(s); stored as 0 - is it really configured as a histogram?")
    return counts


def _read_histogram(name):
    h = poll_histogram(name)
    if h is None:
        return None
    counts = _sanitise(np.asarray(h.BinPopulations, dtype=float).ravel(), name)
    if counts.size == 0:
        return None
    first = max(int(num(h.FirstPopulatedBin)), 0)
    last = min(int(num(h.LastPopulatedBin)), counts.size - 1)
    width, offset = num(h.BinWidth), num(h.OffsetAtLeftEdge)
    if width == 0 or last < first:          # last == first is a single populated bin, which is valid
        return None
    return dict(width=width, offset=offset, first=first, last=last, counts=counts,
                units=axis_units(h))


# ---------------- file naming ----------------
def build_path(t: datetime) -> str:
    month = f"{t.year}_{MONTH_NAMES[t.month - 1]}"
    if FILE_PERIOD == "month":
        return os.path.join(BASE_FOLDER, month + ".h5")
    return os.path.join(BASE_FOLDER, month, f"{month}_{t.day:02d}.h5")


# ---------------- HDF5 append ----------------
_SCALARS = (("available", "bool", False), ("bin_width", "f8", np.nan), ("offset", "f8", np.nan),
            ("first_bin", "i4", -1), ("last_bin", "i4", -1), ("n_bins", "i4", 0))


def _grow(ds, n):
    """Resize a 1-D dataset to n rows (new rows take the dataset's fill value)."""
    ds.resize(n, axis=0)


def _column(parent, key, dtype, fill, n_rows):
    """1-D dataset `key`, created already n_rows long if the file does not have it.

    Creating it at the current row count keeps every column aligned with /timestamp
    even in a file written by a version that did not have this column yet; the
    back-filled rows read as the fill value, which is what "unknown" means for all
    of them. Keying every column off one sentinel instead would leave the missing
    ones permanently shorter than /timestamp.
    """
    if key not in parent:
        return parent.create_dataset(key, shape=(n_rows,), maxshape=(None,), dtype=dtype,
                                     chunks=(24,), fillvalue=fill)
    return parent[key]


def append_snapshot(path: str, timestamp: str, results: dict,
                    duration_s: float = float("nan"), read_s: float = float("nan")) -> int:
    """Append one snapshot (dict name -> read_histogram() result) as a row. Returns the row index."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with h5py.File(path, "a") as hf:
        if "timestamp" not in hf:
            hf.attrs["format"] = "lecroy-histograms h5 v1"
            hf.attrs["bin_center"] = "offset + bin_width*(j+shift); shift=0 is the old logger convention"
            hf.attrs["interval_min"] = INTERVAL_MINUTES
            hf.create_dataset("timestamp", shape=(0,), maxshape=(None,), dtype="S19", chunks=(24,))
        i = hf["timestamp"].shape[0]
        _grow(hf["timestamp"], i + 1)
        hf["timestamp"][i] = timestamp.encode()
        # duration_s is clear -> start of readout, read_s is the readout itself. Divide
        # counts by duration_s for a rate: the wall-clock interval is only nominal.
        for key, value in (("duration_s", duration_s), ("read_s", read_s)):
            ds = _column(hf, key, "f8", np.nan, i)
            _grow(ds, i + 1)
            ds[i] = value

        for name in FUNC_NAMES:
            r = results.get(name)
            g = hf.require_group(name)
            for key, dtype, fill in _SCALARS:
                _grow(_column(g, key, dtype, fill, i), i + 1)
            if r is None:
                g["available"][i] = False
                continue

            n_bins = r["counts"].size
            if "counts" not in g:
                # created on first availability; earlier rows stay zero / available=False.
                # ONE CHUNK PER ROW: appending into multi-row compressed chunks rewrites
                # them every hour and leaves the old copies as garbage (measured: 6x bloat).
                # uint64 so no realistic count can overflow (the VBScript stores the raw value);
                # fletcher32 adds a checksum per chunk - the analogue of the text file's "sum" field.
                g.create_dataset("counts", shape=(i, n_bins), maxshape=(None, None), dtype="uint64",
                                 chunks=(1, n_bins), compression="gzip", compression_opts=GZIP_LEVEL,
                                 shuffle=True, fletcher32=True, fillvalue=0)
                # what this function was measuring, so the file is still self-describing
                # months later; lecroy_hist.hdf5_snapshots carries these on the Snapshot
                for key, value in (("description", describe_function(name)),
                                   ("units", r.get("units", ""))):
                    if value:
                        g.attrs[key] = value
            ds = g["counts"]
            if n_bins > ds.shape[1]:                      # scope histogram was given more bins
                ds.resize(n_bins, axis=1)
            ds.resize(i + 1, axis=0)
            row = np.zeros(ds.shape[1], dtype=np.uint64)
            row[r["first"]:r["last"] + 1] = np.rint(r["counts"][r["first"]:r["last"] + 1])
            ds[i, :] = row
            g["available"][i] = True
            g["bin_width"][i], g["offset"][i] = r["width"], r["offset"]
            g["first_bin"][i], g["last_bin"][i], g["n_bins"][i] = r["first"], r["last"], n_bins
        return i


# ---------------- rescue path: plain text, same format as the VBScript logger ----------------
def _vb(x: float) -> str:
    return str(int(x)) if float(x).is_integer() and abs(x) < 1e15 else "%.15G" % x


def write_rescue_text(path: str, timestamp: str, results: dict,
                      duration_s: float = float("nan")) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # duration_s matters as much here as in the HDF5 file - it is what a rate is
    # computed from - so the rescued snapshot has to carry it too
    lines = ["#lecroy-histograms v2", f"#timestamp={timestamp}", f"#interval_min={INTERVAL_MINUTES}",
             f"#duration_s={_vb(duration_s)}",
             "#columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts..."]
    for name in FUNC_NAMES:
        r = results.get(name)
        if r is None:
            lines.append(f"{name},unavailable")
            continue
        c = r["counts"][r["first"]:r["last"] + 1]
        lines.append(",".join([name, _vb(r["width"]), _vb(r["offset"]), str(r["first"]), str(r["last"]),
                               str(r["counts"].size), _vb(float(c.sum()))] + [_vb(v) for v in c]))
    with open(path, "w", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")


def _rescue(path: str, now: datetime, timestamp: str, results: dict,
            duration_s: float, err: Exception) -> str:
    """Dump a snapshot whose HDF5 append failed to text. Returns a status line.

    The usual reason an append fails is that its own folder cannot be written to
    (new month folder, permissions, full disk), which is exactly the case where
    writing the rescue file beside it fails as well - so fall back to the log
    folder and then the temp folder before declaring the snapshot lost.
    """
    stem = (path[:-3] if path.endswith(".h5") else path) + f".rescue_{now:%H%M}.csv"
    leaf = os.path.basename(stem)
    candidates = [stem,
                  os.path.join(os.path.dirname(LOG_FILE) or ".", leaf),
                  os.path.join(tempfile.gettempdir(), leaf)]
    last = None
    for candidate in candidates:
        try:
            write_rescue_text(candidate, timestamp, results, duration_s)
            return f"HDF5 append FAILED ({err}); snapshot saved as {candidate}"
        except Exception as e:  # noqa: BLE001 - try the next location
            last = e
    return f"SNAPSHOT LOST: HDF5 append failed ({err}) and every rescue location failed ({last})"


# ---------------- main loop ----------------
def log_once(now: datetime, duration_s: float = float("nan")):
    """Read all functions once and store them. Returns (status line, functions read).

    The count is what the main loop watches to notice a dead COM link: a stale
    scope object still answers every call, it just never yields a histogram.
    """
    t0 = time.time()
    _last_read_times.clear()
    results = {name: read_histogram(name) for name in FUNC_NAMES}
    read_s = time.time() - t0
    n_ok = sum(r is not None for r in results.values())
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    path = build_path(now)
    # name the slow ones: a function that is not triggering costs POLL_MAX_TRIES *
    # POLL_SLEEP_S seconds and can push the whole snapshot past its slot
    slow = ", ".join(f"{n} {t:.0f}s" for n, t in _last_read_times.items() if t > 1.0)
    detail = f"read {read_s:.1f}s" + (f" [slow: {slow}]" if slow else "")
    try:
        row = append_snapshot(path, timestamp, results, duration_s, read_s)
        return f"{path} row {row}  ({n_ok}/{len(FUNC_NAMES)} histograms, {detail})", n_ok
    except Exception as e:  # noqa: BLE001
        return _rescue(path, now, timestamp, results, duration_s, e), n_ok


def maintain_link(cleared: bool, n_ok: int, dead_cycles: int):
    """Reattach to the scope application when the COM link looks dead.

    Returns (histograms are known to have been cleared, consecutive dead cycles).

    clear_sweeps() reports failure by returning "" rather than raising, so this
    has to be driven by its return value; watching for an exception instead means
    never reconnecting at all. A failed clear is acted on at once, since the scope
    application restarting is the likely cause. A readout where nothing at all was
    available is only suspicious after RECONNECT_AFTER_DEAD_CYCLES in a row: one
    function that is not triggering is normal, all of them never being ready is
    what a stale COM object looks like from the outside.
    """
    if cleared and n_ok > 0:
        return True, 0
    if cleared:
        dead_cycles += 1
        if dead_cycles < RECONNECT_AFTER_DEAD_CYCLES:
            return True, dead_cycles
        say(f"{dead_cycles} consecutive snapshots with no histograms at all - "
            "reattaching to the scope application")
    else:
        say("clear sweeps failed - reattaching to the scope application")
    try:
        connect()
    except Exception as e:  # noqa: BLE001 - the next cycle tries again
        say(f"reconnect failed: {e}; will retry next cycle")
        return cleared, dead_cycles
    say("reconnected to the scope application")
    return bool(clear_sweeps()), 0


def main() -> None:
    connect(retries=CONNECT_RETRIES)
    os.makedirs(BASE_FOLDER, exist_ok=True)
    say(f"logger started (pid {os.getpid()}, interval={INTERVAL_MINUTES} min, "
        f"{len(FUNC_NAMES)} functions, one file per {FILE_PERIOD})")
    first = clear_sweeps()
    say(f"clear sweeps: {first}" if first else "clear sweeps: FAILED - see the error above")

    # Snapshots are taken on a fixed wall-clock grid (e.g. every 5 min on the 5-minute
    # marks), NOT interval-after-the-last-one: reading 6 histograms takes time, and
    # sleeping a full interval on top of it makes each cycle drift later and later
    # until a grid slot is skipped entirely.
    step = timedelta(minutes=INTERVAL_MINUTES)
    cleared_at = datetime.now()
    if ALIGN_TO_CLOCK:
        epoch = cleared_at.replace(hour=0, minute=0, second=0, microsecond=0)
        next_run = epoch + (int((cleared_at - epoch) / step) + 1) * step
    else:
        next_run = cleared_at + step

    dead_cycles = 0
    while True:
        wait = (next_run - datetime.now()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        now = datetime.now()
        duration = (now - cleared_at).total_seconds()      # true accumulation time
        n_ok = 0
        try:
            status, n_ok = log_once(now, duration)
            say(status)
        except Exception as e:  # noqa: BLE001 - keep logging; the next cycle may succeed
            say(f"SNAPSHOT LOST: {e}")

        cleared, dead_cycles = maintain_link(bool(clear_sweeps()), n_ok, dead_cycles)
        # only restart the clock when the histograms really were reset: if they were
        # not, they keep accumulating from the old mark and duration_s must say so
        if cleared:
            cleared_at = datetime.now()

        next_run += step
        if next_run <= datetime.now():                      # readout overran the interval
            missed = int((datetime.now() - next_run) / step) + 1
            next_run += missed * step
            say(f"  (readout took {duration:.0f}s, longer than the interval - "
                f"skipping {missed} slot(s); consider a larger INTERVAL_MINUTES)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:          # the documented way to stop a console run
        say("stopped by user (Ctrl+C)")
    except Exception:
        import traceback
        say("CRASHED:\n" + traceback.format_exc())
        raise
    

