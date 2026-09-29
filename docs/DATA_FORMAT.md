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
| `/duration_s` | `(N,)` | `float64` | Seconds accumulated, from the clear to the *start* of that row's readout (NaN if unknown). **Use this, not `INTERVAL_MINUTES`, to convert counts to a rate** — readout time makes the real interval longer than the nominal one. |
| `/read_s` | `(N,)` | `float64` | How long that row's readout itself took (NaN if unknown) |

The functions are read one after another, so a given function accumulated
somewhere between `duration_s` and `duration_s + read_s` seconds — `read_s`
is there to bound that. It is normally a couple of seconds against an hour,
but a function that is not triggering can push it to tens of seconds, and
then the distinction starts to matter.

Rows are always the same length across every dataset in the file. A column
introduced after a file was started is back-filled with its fill value for
the earlier rows rather than left short.

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

**Always mask on `available`.** A row exists for every snapshot attempt, and
a function that was unavailable — or a snapshot interrupted partway through
its append, e.g. by a `taskkill` — leaves a row of zeros behind. Those rows
are indistinguishable from a genuinely empty histogram unless you check the
flag. `first_bin = -1` and `bin_width = NaN` mark the same rows.

Bin populations are counts, so `counts` is never negative by construction:
the logger zeroes any negative or non-finite value the scope reports and
logs a warning, rather than letting it wrap around to ~1.8e19 in the cast
to `uint64`. If you see that warning, the function is not configured as a
plain histogram and the row should not be trusted.

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
instead as `<name>.rescue_HHMM.csv` next to the intended `.h5` file. If that
folder is itself the problem, the log folder and then the OS temp folder are
tried; the log line names wherever the file actually landed.

```
#lecroy-histograms v2
#timestamp=<ISO timestamp>
#interval_min=<INTERVAL_MINUTES>
#duration_s=<seconds accumulated, or NAN>
#columns=name,binWidth,offset,firstBin,lastBin,nBinsTotal,sum,counts...
F1,<width>,<offset>,<first>,<last>,<nBinsTotal>,<sum>,<c0>,<c1>,...
F2,unavailable
...
```

One line per function; `unavailable` if that function had no valid histogram
that snapshot. Only the bins in `[firstBin, lastBin]` are listed, not the
full axis, and `sum` is their total. `#duration_s` is the same quantity as
`/duration_s` in the HDF5 file, so a rescued snapshot can be turned into a
rate the same way as any other; older rescue files predate that header and
only have the nominal `#interval_min`.

This is a fallback for a single bad snapshot, not a parallel recording
format — check for stray `.rescue_*.csv` files after a run.
