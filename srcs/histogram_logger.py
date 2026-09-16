#!/usr/bin/env python3
"""
histogram_logger.py - LeCroy X-Stream histogram logger that writes HDF5 directly.

Same job as histogram_logger.vbs (clear sweeps, wait INTERVAL_MINUTES, read the
histograms in F1..F6), but each snapshot is appended as one row to an HDF5
file instead of becoming its own text file:

    FILE_PERIOD = "day"    ->  C:\\Histograms\\2026_Sep\\2026_Sep_06.h5   (24 rows)
    FILE_PERIOD = "month"  ->  C:\\Histograms\\2026_Sep.h5              (~720 rows)

The file layout is the one lecroy_hist.pack_hdf5() writes, so lecroy_hist.py
(hdf5_histogram, to_th1) reads both:

    /timestamp        (N,)         "YYYY-MM-DDTHH:MM:SS"  (end of the accumulation)
    /duration_s       (N,)         float64, seconds actually accumulated (clear -> read)
    /F1/counts        (N, nBins)   uint64, one gzip+shuffle chunk per row, fletcher32 checksum
    /F1/available     (N,)         bool
    /F1/bin_width     (N,)         float64   (NaN where unavailable)
    /F1/offset        (N,)         float64
    /F1/first_bin     (N,)         int32     (-1 where unavailable)
    /F1/last_bin      (N,)         int32
    /F1/n_bins        (N,)         int32     length of the scope's BinPopulations
    centre of scope bin j = offset + bin_width*(j + shift), shift=0 as in the old logger

If the HDF5 append fails for any reason, the snapshot is written as a v2 text
file next to it (<name>.rescue_HHMM.csv) so nothing is lost.

Requirements on the scope PC (Python 3.8 is the last version that runs on
Windows 7):   pip install pywin32 h5py numpy
Run in a console:      python histogram_logger.py
Run with no console:   pythonw histogram_logger.py     (status goes to LOG_FILE only)

A console window can freeze the script: clicking in a Command Prompt with QuickEdit
enabled blocks the process on its next write to stdout until a key is pressed. Running
under pythonw.exe (or with output redirected to a file) removes that failure mode.
"""
import os
import re
import sys
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

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
scope = None   # set by connect()


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
def connect():
    """Attach to the running scope application (same COM object the VBScript used)."""
    global scope
    import win32com.client
    scope = win32com.client.Dispatch("LeCroy.XStreamDSO")
    return scope


def _sweep_count():
    """Total population of the first available function - used to check a clear worked."""
    for name in FUNC_NAMES:
        try:
            return float(np.asarray(scope.Math.Functions(name).Out.Result.BinPopulations,
                                    dtype=float).sum())
        except Exception:  # noqa: BLE001
            continue
    return None


_verified = False


def clear_sweeps() -> str:
    """Clear the accumulated sweeps. Returns the method used, or "" if it failed.

    XStream models this as an action object, so it is ClearSweeps.ActNow() - plain
    ClearSweeps() raises DISP_E_MEMBERNOTFOUND. Verified once at startup by checking
    that the histogram population actually drops: on this scope
    app.Acquisition.ClearSweeps.ActNow() returned cleanly while clearing nothing,
    so "did not raise" is not proof that it worked.
    Run probe_scope.py if the scope firmware changes and this stops working.
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


def poll_histogram(name):
    """Result object of math function `name`, or None if not ready within the timeout."""
    for _ in range(POLL_MAX_TRIES):
        try:
            h = scope.Math.Functions(name).Out.Result
            if num(h.LastPopulatedBin) > num(h.FirstPopulatedBin):
                return h
        except Exception:  # noqa: BLE001 - function off / not a histogram yet
            pass
        time.sleep(POLL_SLEEP_S)
    return None


_last_read_times = {}          # name -> seconds spent in the last read_histogram call


def read_histogram(name):
    """dict(width, offset, first, last, counts[full axis]) or None if unavailable."""
    t0 = time.time()
    try:
        return _read_histogram(name)
    finally:
        _last_read_times[name] = time.time() - t0


def _read_histogram(name):
    h = poll_histogram(name)
    if h is None:
        return None
    counts = np.asarray(h.BinPopulations, dtype=float).ravel()
    if counts.size == 0:
        return None
    first = max(int(num(h.FirstPopulatedBin)), 0)
    last = min(int(num(h.LastPopulatedBin)), counts.size - 1)
    width, offset = num(h.BinWidth), num(h.OffsetAtLeftEdge)
    if width == 0 or last <= first:
        return None
    return dict(width=width, offset=offset, first=first, last=last, counts=counts)


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


def append_snapshot(path: str, timestamp: str, results: dict,
                    duration_s: float = float("nan")) -> int:
    """Append one snapshot (dict name -> read_histogram() result) as a row. Returns the row index."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with h5py.File(path, "a") as hf:
        if "timestamp" not in hf:
            hf.attrs["format"] = "lecroy-histograms h5 v1"
            hf.attrs["bin_center"] = "offset + bin_width*(j+shift); shift=0 is the old logger convention"
            hf.attrs["interval_min"] = INTERVAL_MINUTES
            hf.create_dataset("timestamp", shape=(0,), maxshape=(None,), dtype="S19", chunks=(24,))
            # seconds actually accumulated (clear -> read). Divide counts by this for a rate:
            # the wall-clock interval is only nominal, readout time makes the real one longer.
            hf.create_dataset("duration_s", shape=(0,), maxshape=(None,), dtype="f8", chunks=(24,),
                              fillvalue=np.nan)
        i = hf["timestamp"].shape[0]
        _grow(hf["timestamp"], i + 1)
        hf["timestamp"][i] = timestamp.encode()
        if "duration_s" in hf:
            _grow(hf["duration_s"], i + 1)
            hf["duration_s"][i] = duration_s

        for name in FUNC_NAMES:
            r = results.get(name)
            g = hf.require_group(name)
            if "available" not in g:
                for key, dtype, fill in _SCALARS:
                    g.create_dataset(key, shape=(0,), maxshape=(None,), dtype=dtype, chunks=(24,),
                                     fillvalue=fill)
            for key, _, _ in _SCALARS:
                _grow(g[key], i + 1)
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


def write_rescue_text(path: str, timestamp: str, results: dict) -> None:
    lines = ["#lecroy-histograms v2", f"#timestamp={timestamp}", f"#interval_min={INTERVAL_MINUTES}",
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


# ---------------- main loop ----------------
def log_once(now: datetime, duration_s: float = float("nan")) -> str:
    """Read all functions once and store them. Returns a one-line status."""
    t0 = time.time()
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
        row = append_snapshot(path, timestamp, results, duration_s)
        return f"{path} row {row}  ({n_ok}/{len(FUNC_NAMES)} histograms, {detail})"
    except Exception as e:  # noqa: BLE001
        rescue = path[:-3] + f".rescue_{now:%H%M}.csv"
        write_rescue_text(rescue, timestamp, results)
        return f"HDF5 append FAILED ({e}); snapshot saved as {rescue}"


def main() -> None:
    connect()
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

    while True:
        wait = (next_run - datetime.now()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        now = datetime.now()
        duration = (now - cleared_at).total_seconds()      # true accumulation time
        try:
            say(log_once(now, duration))
        except Exception as e:  # noqa: BLE001 - keep logging; the next cycle may succeed
            say(f"SNAPSHOT LOST: {e}")
        try:
            clear_sweeps()
        except Exception as e:  # noqa: BLE001 - scope app restarted? try to reattach
            say(f"clear failed ({e}); reconnecting")
            try:
                connect()
            except Exception as e2:  # noqa: BLE001
                say(f"reconnect failed: {e2}")
        cleared_at = datetime.now()

        next_run += step
        if next_run <= cleared_at:                          # readout overran the interval
            missed = int((cleared_at - next_run) / step) + 1
            next_run += missed * step
            say(f"  (readout took {duration:.0f}s, longer than the interval - "
                f"skipping {missed} slot(s); consider a larger INTERVAL_MINUTES)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        say("CRASHED:\n" + traceback.format_exc())
        raise
    

