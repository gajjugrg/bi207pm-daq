#!/usr/bin/env python3
"""
lecroy_hist.py - read, verify, compare and convert LeCroy histogram snapshots
written by histogram_logger.vbs or rescued by histogram_logger.py.

File formats
------------
v2 (current logger, plain or gzipped):

    #lecroy-histograms v2
    #timestamp=2026-09-06T14:00:00
    #interval_min=60
    #duration_s=3601          (histogram_logger.py rescue files; absent in older ones)
    #columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts...
    F1,1.25E-10,-1.2E-07,412,1587,2000,3600000,0,3,7,12,...
    F2,...
    F5,unavailable

    One line per function. "counts" are the populations of bins
    firstBin..lastBin (0-based scope bin index). "sum" is the total of those
    counts and is checked by `verify`. nBinsTotal is the length of the scope's
    BinPopulations array (0 = not recorded, e.g. files converted from v1).

v1 (old logger):  '#' metadata block, then F1_binCenter,F1,F2_binCenter,F2,...

Both formats are returned as the same Python objects, so old and new files can
be read - and compared - with the same code.

Bin axis
--------
Scope bin j has centre   offset + binWidth * (j + shift)
shift = 0   reproduces the old logger's convention (it wrote #binCenterShift=0)
shift = 0.5 if OffsetAtLeftEdge turns out to be the LEFT EDGE of bin 0.
Because v2 stores only offset/binWidth, this choice can be changed at read
time without re-recording anything.

Python
------
    import lecroy_hist as lh
    snap = lh.read_snapshot("Record_2026_Sep_06_14_00.csv.gz")
    h = snap.hists["F3"]
    h.counts, h.bin_centers(), h.bin_edges(), h.total
    th1 = lh.to_th1(h)            # pyROOT TH1D on the full scope axis

Command line
------------
    python lecroy_hist.py info    FILE...          (.csv, .csv.gz or .h5)
    python lecroy_hist.py verify  FILE...          exit code 1 on any problem (.h5: every row is read)
    python lecroy_hist.py compare FILE_A FILE_B    v1 vs v2, or any two snapshots
    python lecroy_hist.py convert OLD.csv [-o NEW.csv.gz]
    python lecroy_hist.py pack C:/Histograms/2026_Sep/*.csv.gz -o 2026_Sep.h5   (needs h5py)

HDF5 archive (one file per month, see pack_hdf5 for the layout):
    import h5py
    with h5py.File("2026_Sep.h5") as hf:
        f3 = hf["F3/counts"][:]          # (snapshots, bins) array - the whole month at once
        h  = lh.hdf5_histogram(hf, "F3", 10)   # one snapshot as a Histogram -> lh.to_th1(h)
"""
from __future__ import annotations

import argparse
import gzip
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

V2_MAGIC = "#lecroy-histograms v2"
V2_COLUMNS = "#columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts..."


# ---------------------------------------------------------------------------
# data structures
# ---------------------------------------------------------------------------
@dataclass
class Histogram:
    """One scope histogram. `counts[k]` is the population of scope bin first_bin + k."""
    name: str
    bin_width: float
    offset: float
    first_bin: int
    last_bin: int
    counts: np.ndarray
    n_bins_total: Optional[int] = None      # full scope axis length; None if unknown
    declared_sum: Optional[float] = None    # 'sum' field from a v2 file
    recorded_centers: Optional[np.ndarray] = None  # bin centres stored in a v1 file

    @property
    def n_bins(self) -> int:
        return self.last_bin - self.first_bin + 1

    @property
    def total(self) -> float:
        return float(np.nansum(self.counts))

    def bin_indices(self) -> np.ndarray:
        return np.arange(self.first_bin, self.last_bin + 1)

    def bin_centers(self, shift: float = 0.0) -> np.ndarray:
        return self.offset + self.bin_width * (self.bin_indices() + shift)

    def bin_edges(self, shift: float = 0.0) -> np.ndarray:
        """n_bins + 1 edges, consistent with bin_centers(shift)."""
        j = np.arange(self.first_bin, self.last_bin + 2)
        return self.offset + self.bin_width * (j + shift - 0.5)

    def full_counts(self) -> np.ndarray:
        """Counts on the full scope axis (zeros outside the populated range)."""
        n = self.n_bins_total or (self.last_bin + 1)
        full = np.zeros(n)
        full[self.first_bin:self.last_bin + 1] = self.counts
        return full


@dataclass
class Snapshot:
    path: str
    version: int
    timestamp: str
    interval_min: Optional[int] = None
    hists: Dict[str, Histogram] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)
    order: List[str] = field(default_factory=list)   # function names in file order
    bin_center_shift: Optional[float] = None         # only recorded by v1 files
    # seconds actually accumulated, clear -> start of readout. Divide counts by this
    # for a rate, not by interval_min: the nominal interval ignores readout time.
    # None in v1 files and in v2 files written before the header existed.
    duration_s: Optional[float] = None
    read_s: Optional[float] = None                   # how long that readout took
    descriptions: Dict[str, str] = field(default_factory=dict)   # name -> what it measures
    units: Dict[str, str] = field(default_factory=dict)          # name -> axis units


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def _open_text(path: str):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _num(s: str) -> float:
    """float() that maps blank cells to NaN (so `verify` can flag them)."""
    s = s.strip()
    return float(s) if s else math.nan


def read_snapshot(path: str) -> Snapshot:
    with _open_text(path) as f:
        lines = f.read().splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    if first.startswith(V2_MAGIC):
        return _parse_v2(lines, str(path))
    if first.startswith("#timestamp="):
        return _parse_v1(lines, str(path))
    raise ValueError(f"{path}: not a histogram snapshot (first line: {first[:40]!r})")


def _parse_v2(lines: List[str], path: str) -> Snapshot:
    snap = Snapshot(path=path, version=2, timestamp="")
    meta: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "=" in line:
                k, v = line[1:].split("=", 1)
                meta[k.strip()] = v.strip()
            continue
        f = line.split(",")
        name = f[0].strip()
        snap.order.append(name)
        if len(f) >= 2 and f[1].strip() == "unavailable":
            snap.unavailable.append(name)
            continue
        if len(f) < 8:
            raise ValueError(f"{path}: malformed line for {name!r}: {line[:60]!r}")
        width, offset = _num(f[1]), _num(f[2])
        first_bin, last_bin, n_total = int(f[3]), int(f[4]), int(f[5])
        declared = _num(f[6])
        counts = np.array([_num(x) for x in f[7:]])
        snap.hists[name] = Histogram(name, width, offset, first_bin, last_bin, counts,
                                     n_total if n_total > 0 else None, declared)
    snap.timestamp = meta.get("timestamp", "")
    if "interval_min" in meta:
        snap.interval_min = int(meta["interval_min"])
    if "duration_s" in meta:
        snap.duration_s = _num(meta["duration_s"])      # "NAN" when the logger did not know
    return snap


_V1_META = re.compile(r"#(\w+)\s+binWidth=(\S+)\s+offset=(\S+)\s+firstBin=(-?\d+)"
                      r"\s+lastBin=(-?\d+)\s+nBins=(\d+)")
_V1_UNAVAIL = re.compile(r"#(\w+)\s+unavailable")


def _parse_v1(lines: List[str], path: str) -> Snapshot:
    snap = Snapshot(path=path, version=1, timestamp="")
    meta: Dict[str, tuple] = {}
    header: Optional[List[str]] = None
    rows: List[List[str]] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            m = _V1_META.match(line)
            if m:
                meta[m.group(1)] = (float(m.group(2)), float(m.group(3)),
                                    int(m.group(4)), int(m.group(5)), int(m.group(6)))
                continue
            m = _V1_UNAVAIL.match(line)
            if m:
                snap.unavailable.append(m.group(1))
                continue
            if line.startswith("#timestamp="):
                snap.timestamp = line.split("=", 1)[1].strip()
            elif line.startswith("#binCenterShift="):
                snap.bin_center_shift = float(line.split("=", 1)[1])
            continue
        if header is None:
            header = line.split(",")
            continue
        rows.append(line.split(","))
    if header is None:
        raise ValueError(f"{path}: v1 file has no column header line")

    names = [c[:-len("_binCenter")] if c.endswith("_binCenter") else c for c in header[0::2]]
    snap.order = names
    for i, name in enumerate(names):
        if name not in meta:
            continue
        width, offset, first_bin, last_bin, nb = meta[name]
        col_c, col_n = 2 * i, 2 * i + 1
        centers = np.array([_num(r[col_c]) if col_c < len(r) else math.nan for r in rows[:nb]])
        counts = np.array([_num(r[col_n]) if col_n < len(r) else math.nan for r in rows[:nb]])
        snap.hists[name] = Histogram(name, width, offset, first_bin, last_bin, counts,
                                     None, None, centers)
    return snap


# ---------------------------------------------------------------------------
# writing (v2) - mirrors histogram_logger.vbs line for line
# ---------------------------------------------------------------------------
def vb_num(x) -> str:
    """Format a number like VBScript's CStr: integers plain, else <=15 significant digits."""
    xf = float(x)
    if xf.is_integer() and abs(xf) < 1e15:
        return str(int(xf))
    return "%.15G" % xf


def format_v2_line(h: Histogram) -> str:
    head = [h.name, vb_num(h.bin_width), vb_num(h.offset), str(h.first_bin), str(h.last_bin),
            str(h.n_bins_total or 0), vb_num(h.total)]
    return ",".join(head + [vb_num(c) for c in h.counts])


def v2_text(snap: Snapshot) -> str:
    out = [V2_MAGIC, f"#timestamp={snap.timestamp}"]
    if snap.interval_min is not None:
        out.append(f"#interval_min={snap.interval_min}")
    if snap.duration_s is not None:          # header order matches histogram_logger.py
        out.append(f"#duration_s={vb_num(snap.duration_s)}")
    out.append(V2_COLUMNS)
    for name in snap.order:
        h = snap.hists.get(name)
        out.append(format_v2_line(h) if h is not None else f"{name},unavailable")
    return "\r\n".join(out) + "\r\n"          # VBScript WriteLine uses CR LF


def write_v2(snap: Snapshot, path: str) -> None:
    data = v2_text(snap).encode("ascii")
    if str(path).endswith(".gz"):
        with gzip.open(path, "wb", compresslevel=6) as f:
            f.write(data)
    else:
        with open(path, "wb") as f:
            f.write(data)


# ---------------------------------------------------------------------------
# pyROOT
# ---------------------------------------------------------------------------
def to_th1(h: Histogram, name: Optional[str] = None, title: str = "",
           shift: float = 0.0, n_bins: Optional[int] = None):
    """
    Build a ROOT.TH1D on the full scope axis and fill bins first_bin..last_bin.
    Scope bin j -> ROOT bin j+1, centre = offset + binWidth*(j+shift).
    Bin errors are left to ROOT's default (sqrt(N)).
    """
    import ROOT  # imported lazily so the rest of the module works without ROOT
    n = n_bins or h.n_bins_total or (h.last_bin + 1)
    xlow = h.offset + h.bin_width * (shift - 0.5)
    xup = xlow + h.bin_width * n
    th = ROOT.TH1D(name or h.name, title, n, xlow, xup)
    for j, c in zip(h.bin_indices(), h.counts):
        if 0 <= j < n:
            th.SetBinContent(int(j) + 1, float(c))
    th.SetEntries(h.total)
    return th


# ---------------------------------------------------------------------------
# verify / compare
# ---------------------------------------------------------------------------
def verify_snapshot(path: str):
    """Return (ok, messages, snapshot_or_None)."""
    try:
        snap = read_snapshot(path)
    except Exception as e:  # noqa: BLE001
        return False, [f"cannot parse: {e}"], None
    msgs = check_snapshot(snap)
    return (not msgs), msgs, snap


def check_snapshot(snap: Snapshot) -> List[str]:
    """Consistency checks on an already-read snapshot; returns a list of problems (empty = OK)."""
    msgs: List[str] = []
    if not snap.timestamp:
        msgs.append("missing #timestamp")
    else:
        try:
            datetime.strptime(snap.timestamp, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            msgs.append(f"bad timestamp {snap.timestamp!r}")
    if not snap.hists and not snap.unavailable:
        msgs.append("no function lines")

    for name, h in snap.hists.items():
        p = f"{name}: "
        if not math.isfinite(h.bin_width) or h.bin_width == 0:
            msgs.append(p + f"bad binWidth {h.bin_width}")
        if not math.isfinite(h.offset):
            msgs.append(p + "bad offset")
        if h.first_bin < 0:
            msgs.append(p + f"firstBin {h.first_bin} < 0")
        if h.last_bin < h.first_bin:
            msgs.append(p + f"lastBin {h.last_bin} < firstBin {h.first_bin}")
        if len(h.counts) != h.n_bins:
            msgs.append(p + f"{len(h.counts)} counts for {h.n_bins} bins")
        if h.n_bins_total is not None and h.last_bin >= h.n_bins_total:
            msgs.append(p + f"lastBin {h.last_bin} outside nBinsTotal {h.n_bins_total}")
        if np.isnan(h.counts).any():
            msgs.append(p + f"{int(np.isnan(h.counts).sum())} blank/missing count cells")
        else:
            if (h.counts < 0).any():
                msgs.append(p + "negative counts")
            if not np.all(h.counts == np.round(h.counts)):
                msgs.append(p + "non-integer counts")
        if h.declared_sum is not None and abs(h.total - h.declared_sum) > 0.5:
            msgs.append(p + f"sum of counts {h.total:.0f} != declared sum {h.declared_sum:.0f}")
        if h.recorded_centers is not None and not np.isnan(h.recorded_centers).any():
            shift = snap.bin_center_shift or 0.0
            dev = np.max(np.abs(h.recorded_centers - h.bin_centers(shift)))
            if dev > 1e-6 * abs(h.bin_width):
                msgs.append(p + f"stored bin centres deviate from offset+width*j by up to {dev:g}")
    return msgs


def _natural_key(name: str):
    m = re.match(r"([A-Za-z]*)(\d*)", name)
    return (m.group(1), int(m.group(2) or 0), name)


def compare_snapshots(a: Snapshot, b: Snapshot, rtol: float = 1e-9):
    """Return (identical, messages). Only the histogram content is compared."""
    msgs: List[str] = []
    for name in sorted(set(a.hists) | set(b.hists), key=_natural_key):
        ha, hb = a.hists.get(name), b.hists.get(name)
        if ha is None or hb is None:
            msgs.append(f"{name}: present only in {'B' if ha is None else 'A'}")
            continue
        for attr in ("bin_width", "offset"):
            va, vb = getattr(ha, attr), getattr(hb, attr)
            if not math.isclose(va, vb, rel_tol=rtol, abs_tol=0.0):
                msgs.append(f"{name}: {attr} {va!r} != {vb!r}")
        for attr in ("first_bin", "last_bin"):
            if getattr(ha, attr) != getattr(hb, attr):
                msgs.append(f"{name}: {attr} {getattr(ha, attr)} != {getattr(hb, attr)}")
        if len(ha.counts) != len(hb.counts):
            msgs.append(f"{name}: {len(ha.counts)} vs {len(hb.counts)} counts")
        elif not np.array_equal(ha.counts, hb.counts, equal_nan=True):
            nd = int(np.count_nonzero(ha.counts != hb.counts))
            msgs.append(f"{name}: counts differ in {nd} bins")
    return (not msgs), msgs


# ---------------------------------------------------------------------------
# HDF5 archive: many snapshots -> one file  (needs h5py)
# ---------------------------------------------------------------------------
# Layout of the .h5 file written by pack_hdf5():
#   /timestamp            (N,)  ASCII "YYYY-MM-DDTHH:MM:SS", one per snapshot
#   /duration_s           (N,)  float64 seconds accumulated; NaN where not recorded.
#                         Divide counts by this for a rate, not by interval_min.
#   /source_file          (N,)  ASCII, the snapshot file each row came from
#                         (pack_hdf5 only; histogram_logger.py has no source file)
#   /read_s               (N,)  float64 readout duration (histogram_logger.py only)
#   /F1/counts            (N, nBins) uint64, zeros outside the populated range,
#                         chunked per day, gzip + shuffle compressed, fletcher32 checksum
#   /F1/available         (N,)  bool     False where the logger wrote "unavailable"
#   /F1/bin_width         (N,)  float64  NaN where unavailable
#   /F1/offset            (N,)  float64
#   /F1/first_bin         (N,)  int32    -1 where unavailable
#   /F1/last_bin          (N,)  int32
#   /F1/n_bins            (N,)  int32    length of the scope's BinPopulations for that snapshot
# Scope bin j of snapshot i is counts[i, j]; its centre is offset[i] + bin_width[i]*(j+shift).
# histogram_logger.py (direct HDF5 logging on the scope PC) writes the same layout,
# plus optional per-group attrs "description" and "units" (see show_mapping.py).

def pack_hdf5(files, out: str, level: int = 6) -> int:
    """Read snapshot files (v1/v2, plain or .gz) and write one HDF5 archive. Returns N."""
    import h5py
    snaps = [read_snapshot(f) for f in sorted(files)]
    snaps.sort(key=lambda s: s.timestamp)
    names: List[str] = []
    for s in snaps:
        names += [n for n in s.order if n not in names]
    n = len(snaps)
    with h5py.File(out, "w") as hf:
        hf.attrs["format"] = "lecroy-histograms h5 v1"
        hf.attrs["bin_center"] = "offset + bin_width*(j+shift); shift=0 is the old logger convention"
        hf.create_dataset("timestamp", data=np.array([s.timestamp for s in snaps], dtype="S19"))
        hf.create_dataset("source_file", data=np.array([os.path.basename(s.path) for s in snaps], dtype="S"))
        # carried through from #duration_s where the source file had it; NaN otherwise,
        # which is what histogram_logger.py writes when it did not know either
        hf.create_dataset("duration_s", data=np.array(
            [math.nan if s.duration_s is None else s.duration_s for s in snaps], dtype="f8"))
        if snaps and snaps[0].interval_min is not None:
            hf.attrs["interval_min"] = snaps[0].interval_min
        for name in sorted(names, key=_natural_key):
            hs = [s.hists.get(name) for s in snaps]
            n_bins = max([h.n_bins_total or (h.last_bin + 1) for h in hs if h is not None], default=0)
            counts = np.zeros((n, n_bins), dtype=np.uint64)
            avail = np.zeros(n, dtype=bool)
            width = np.full(n, np.nan)
            offset = np.full(n, np.nan)
            first = np.full(n, -1, dtype=np.int32)
            last = np.full(n, -1, dtype=np.int32)
            nbins = np.zeros(n, dtype=np.int32)
            for i, h in enumerate(hs):
                if h is None:
                    continue
                avail[i], width[i], offset[i] = True, h.bin_width, h.offset
                first[i], last[i] = h.first_bin, h.last_bin
                nbins[i] = h.n_bins_total or 0
                counts[i, h.first_bin:h.last_bin + 1] = np.nan_to_num(h.counts)
            g = hf.create_group(name)
            # a function that was unavailable in every snapshot has no bins, and a
            # chunk size of 0 is illegal, so that case is stored uncompressed
            if n_bins > 0:
                g.create_dataset("counts", data=counts, chunks=(min(n, 24), n_bins),
                                 compression="gzip", compression_opts=level,
                                 shuffle=True, fletcher32=True)
            else:
                g.create_dataset("counts", data=counts)
            for key, val in (("available", avail), ("bin_width", width), ("offset", offset),
                             ("first_bin", first), ("last_bin", last), ("n_bins", nbins)):
                g.create_dataset(key, data=val)
    return n


def _row_value(hf, key: str, i: int) -> Optional[float]:
    """hf[key][i] as a float, or None if this file has no such column.

    Columns are added to the format over time, so nothing may assume one is
    present; a file written by an older logger simply does not have it.
    """
    if key not in hf:
        return None
    ds = hf[key]
    return float(ds[i]) if i < ds.shape[0] else None


def hdf5_snapshots(path: str):
    """Yield one Snapshot per row of an HDF5 archive written by pack_hdf5 / histogram_logger.py."""
    import h5py
    with h5py.File(path, "r") as hf:
        names = sorted([k for k in hf if isinstance(hf[k], h5py.Group)], key=_natural_key)
        # what each function was measuring, where the logger recorded it. Carried on
        # the Snapshot rather than printed: this is a library function, and `verify`
        # walks the file twice.
        descriptions = {n: str(hf[n].attrs["description"]) for n in names
                        if "description" in hf[n].attrs}
        units = {n: str(hf[n].attrs["units"]) for n in names if "units" in hf[n].attrs}
        timestamps = [t.decode() for t in hf["timestamp"][:]]
        interval = int(hf.attrs["interval_min"]) if "interval_min" in hf.attrs else None
        for i, ts in enumerate(timestamps):
            snap = Snapshot(f"{path}[{i}]", 2, ts, interval, order=list(names),
                            duration_s=_row_value(hf, "duration_s", i),
                            read_s=_row_value(hf, "read_s", i),
                            descriptions=dict(descriptions), units=dict(units))
            for name in names:
                h = hdf5_histogram(hf, name, i)      # raises on a corrupted chunk (fletcher32)
                if h is None:
                    snap.unavailable.append(name)
                else:
                    snap.hists[name] = h
            yield snap


def hdf5_histogram(hf, name: str, i: int) -> Optional[Histogram]:
    """Snapshot i of function `name` from an open h5py.File, as a Histogram (None if unavailable)."""
    g = hf[name]
    if not bool(g["available"][i]):
        return None
    first, last = int(g["first_bin"][i]), int(g["last_bin"][i])
    counts = g["counts"][i, first:last + 1].astype(float)
    n_total = int(g["n_bins"][i]) if "n_bins" in g else 0
    return Histogram(name, float(g["bin_width"][i]), float(g["offset"][i]), first, last, counts,
                     n_total or int(g["counts"].shape[1]))


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def _describe(snap: Snapshot) -> str:
    head = f"{snap.path}: v{snap.version}  timestamp={snap.timestamp}"
    if snap.interval_min:
        head += f"  interval={snap.interval_min} min"
    if snap.duration_s is not None and math.isfinite(snap.duration_s):
        head += f"  duration={snap.duration_s:.0f}s"      # use this for rates
    if snap.read_s is not None and math.isfinite(snap.read_s):
        head += f"  read={snap.read_s:.1f}s"
    lines = [head]
    for name in snap.order:
        h = snap.hists.get(name)
        what = snap.descriptions.get(name, "")
        if what:
            what = f"  <- {what}" + (f" [{snap.units[name]}]" if name in snap.units else "")
        if h is None:
            lines.append(f"  {name:4s} unavailable{what}")
            continue
        c = h.counts
        lines.append(f"  {name:4s} bins {h.first_bin}..{h.last_bin} ({h.n_bins}"
                     + (f" of {h.n_bins_total}" if h.n_bins_total else "")
                     + f")  width={h.bin_width:.6g} offset={h.offset:.6g}"
                     f"  total={h.total:.0f}  max={np.nanmax(c):.0f}{what}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="summarise snapshot files")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("verify", help="check files for format/integrity problems")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("compare", help="compare the histograms in two snapshots")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("convert", help="convert an old v1 file to v2")
    p.add_argument("src")
    p.add_argument("-o", "--out", help="output path (default: <src>.v2.csv.gz)")
    p = sub.add_parser("pack", help="pack many snapshot files into one HDF5 archive (needs h5py)")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--out", required=True, help="output .h5 path")
    args = ap.parse_args(argv)

    if args.cmd == "info":
        for f in args.files:
            if f.endswith(".h5"):
                for snap in hdf5_snapshots(f):
                    print(_describe(snap))
            else:
                print(_describe(read_snapshot(f)))
        return 0

    if args.cmd == "verify":
        bad = 0
        for f in args.files:
            if f.endswith(".h5"):                      # every row is read, so checksums are exercised
                try:
                    problems = [f"row {i} ({s.timestamp}): {m}" for i, s in enumerate(hdf5_snapshots(f))
                                for m in check_snapshot(s)]
                    n_rows = sum(1 for _ in hdf5_snapshots(f))
                except Exception as e:  # noqa: BLE001
                    problems, n_rows = [f"cannot read: {e}"], 0
                if problems:
                    bad += 1
                    print(f"FAIL  {f}")
                    for m in problems:
                        print(f"        - {m}")
                else:
                    print(f"OK    {f}  ({n_rows} snapshots)")
                continue
            ok, msgs, snap = verify_snapshot(f)
            if ok:
                n_ok = len(snap.hists)
                n_all = n_ok + len(snap.unavailable)
                extra = f", unavailable: {' '.join(snap.unavailable)}" if snap.unavailable else ""
                print(f"OK    {f}  ({n_ok}/{n_all} histograms{extra})")
            else:
                bad += 1
                print(f"FAIL  {f}")
                for m in msgs:
                    print(f"        - {m}")
        return 1 if bad else 0

    if args.cmd == "compare":
        a, b = read_snapshot(args.a), read_snapshot(args.b)
        same, msgs = compare_snapshots(a, b)
        print(("IDENTICAL" if same else "DIFFERENT") + f"  {args.a}  vs  {args.b}")
        for m in msgs:
            print(f"  - {m}")
        return 0 if same else 1

    if args.cmd == "convert":
        snap = read_snapshot(args.src)
        out = args.out or re.sub(r"\.csv$", "", args.src) + ".v2.csv.gz"
        write_v2(snap, out)
        same, msgs = compare_snapshots(snap, read_snapshot(out))
        print(f"wrote {out}  (round-trip {'OK' if same else 'FAILED'})")
        for m in msgs:
            print(f"  - {m}")
        return 0 if same else 1
    if args.cmd == "pack":
        import h5py
        n = pack_hdf5(args.files, args.out)
        # round-trip check: every histogram read back from the archive must equal the source file
        bad = []
        with h5py.File(args.out) as hf:
            sources = [s.decode() for s in hf["source_file"][:]]
            by_name = {os.path.basename(f): f for f in args.files}
            for i, src_name in enumerate(sources):
                s = read_snapshot(by_name[src_name])
                back = Snapshot(args.out, 2, s.timestamp,
                                hists={nm: h for nm in s.order if (h := hdf5_histogram(hf, nm, i)) is not None})
                same, msgs = compare_snapshots(s, back)
                if not same:
                    bad.append(f"{src_name}: " + "; ".join(msgs))
        print(f"wrote {args.out}: {n} snapshots, {os.path.getsize(args.out) / 1e6:.2f} MB, "
              f"round-trip {'OK' if not bad else 'FAILED'}")
        for m in bad:
            print(f"  - {m}")
        return 1 if bad else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
