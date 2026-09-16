# bi207pm-daq

Standalone DAQ for the Bi-207 purity monitor: logs histogram data straight from
the LeCroy X-Stream oscilloscope into structured HDF5 files, on a fixed timing
grid, with automatic rescue-to-CSV if a write ever fails.

This replaces the older `histogram_logger.vbs` text-file logger. Same
acquisition logic (clear sweeps → wait `INTERVAL_MINUTES` → read histograms in
`F1..F6`), but each snapshot becomes one appended row in an HDF5 file instead
of its own text file.

## Repo contents

| File | Purpose |
|---|---|
| `histogram_logger.py` | The logger itself. Run this on the scope PC. |
| `requirements.txt` | Python dependencies. |
| `docs/SETUP.md` | First-time setup on a scope PC (Python, packages, scope link). |
| `docs/RUNNING.md` | How to start, monitor, and stop a run; what a crash/restart does. |
| `docs/DATA_FORMAT.md` | Full schema of the HDF5 files and the rescue CSV fallback. |

## Quick start

```powershell
pip install -r requirements.txt
python histogram_logger.py
```

Histograms get written under `C:\Histograms\<year>_<month>\...h5` (or one
file per month — see `docs/SETUP.md`), with status lines echoed to the console
and appended to `C:\Histograms\logger.log`.

See `docs/RUNNING.md` for the full walkthrough and `docs/DATA_FORMAT.md` for
how to read the data back out (e.g. with `h5py` / a companion `lecroy_hist.py`
reader, if/when that's added to this repo).

## Status

Runs on the LeCroy scope(s) used for Bi-207 purity monitor calibration in
liquid argon. See the project notes for current scope inventory and any
known issues (e.g. snapshot stalls on a given scope).
