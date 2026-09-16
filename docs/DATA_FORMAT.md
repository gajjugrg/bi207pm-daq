# Recorded data format

## File layout

One row = one snapshot (one clear → wait `INTERVAL_MINUTES` → read cycle).

- `FILE_PERIOD = "day"` → `C:\Histograms\<year>_<Mon>\<year>_<Mon>_<DD>.h5`
  (≈24 rows/day at a 60-minute interval)
- `FILE_PERIOD = "month"` → `C:\Histograms\<year>_<Mon>.h5` (≈720 rows/month)

Each file is self-contained HDF5 (`h5py`-readable), built incrementally —
every snapshot is a flushed append to an existing file, not a rewrite.

## Top-level datasets

| Path | Shape | Dtype | Meaning |
|---|---|---|---|
| `/timestamp` | `(N,)` | `S19` string `"YYYY-MM-DDTHH:MM:SS"` | End time of the accumulation for that row |
| `/duration_s` | `(N,)` | `float64` | Seconds actually accumulated, clear→read (NaN if unknown). **Use this, not `INTERVAL_MINUTES`, to convert counts to a rate** — readout time makes the real interval longer than the nominal one. |

File-level attributes (`hf.attrs`):
- `format` — `"lecroy-histograms h5 v1"`
- `bin_center` — the convention string below
- `interval_min` — the nominal `INTERVAL_MINUTES` used for that file

## Per-function group (`/F1`, `/F2`, ... one per entry in `FUNC_NAMES`)

| Path | Shape | Dtype | Meaning |
|---|---|---|---|
| `<F>/available` | `(N,)` | `bool` | Whether this function had a valid histogram that snapshot |
| `<F>/bin_width` | `(N,)` | `float64` | Scope `BinWidth` (NaN if unavailable that row) |
| `<F>/offset` | `(N,)` | `float64` | Scope `OffsetAtLeftEdge` |
| `<F>/first_bin` | `(N,)` | `int32` | First populated bin index (-1 if unavailable) |
| `<F>/last_bin` | `(N,)` | `int32` | Last populated bin index |
| `<F>/n_bins` | `(N,)` | `int32` | Length of the scope's `BinPopulations` that row |
| `<F>/counts` | `(N, nBins)` | `uint64` | Full-axis bin populations, zero outside `[first_bin, last_bin]` |

**Bin centre convention:** `centre of bin j = offset + bin_width * (j + shift)`,
with `shift = 0` (matches the old VBScript logger).

**Storage notes** (only matter if you're writing to these files yourself,
not just reading them):
- `counts` uses **one HDF5 chunk per row** (`chunks=(1, n_bins)`) deliberately
  — appending into multi-row compressed chunks means HDF5 rewrites the whole
  chunk on every new row, which measured out to ~6x file bloat over a day.
  Don't "optimize" this to bigger chunks without re-checking that.
- `counts` carries a `fletcher32` checksum per chunk (the HDF5-native
  equivalent of the text logger's per-row `sum` column) and `gzip` +
  `shuffle` compression.
- `n_bins` (and hence the width of `counts`) can grow mid-file if the scope
  reports more bins than before; it never shrinks, and old rows are
  zero-padded, not resized away.

## Reading it back

Any `h5py` script works directly against these paths, e.g.:

```python
import h5py
with h5py.File("2026_Sep_11.h5", "r") as hf:
    ts = hf["timestamp"][:]
    counts = hf["F1/counts"][:]        # (N, nBins) uint64
    rate = counts / hf["duration_s"][:, None]   # counts/s, using true duration
```

## Rescue fallback (only on write failure)

If an HDF5 append fails for any reason, that single snapshot is written
instead as `<name>.rescue_HHMM.csv` next to the intended `.h5` file

```
#lecroy-histograms v2
#timestamp=<ISO timestamp>
#interval_min=<INTERVAL_MINUTES>
#columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts...
F1,<width>,<offset>,<first>,<last>,<nBinsTotal>,<sum>,<c0>,<c1>,...
F2,unavailable
...
```

One line per function; `unavailable` if that function had no valid histogram
that snapshot. This is a fallback for a single bad snapshot, not a parallel
recording format — check for stray `.rescue_*.csv` files after a run.
