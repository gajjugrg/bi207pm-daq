# bi207pm-daq

Standalone DAQ for the Bi-207 purity monitor: logs histogram data straight from
the LeCroy X-Stream oscilloscope into structured HDF5 files, on a fixed timing
grid, with automatic rescue-to-CSV if a write ever fails.

This replaces the older `histogram_logger.vbs` text-file logger. Same
acquisition logic (clear sweeps → wait `INTERVAL_MINUTES` → read the histogram
functions listed in `FUNC_NAMES`, `F1`–`F5` by default), but each snapshot
becomes one appended row in an HDF5 file instead of its own text file.

## Repo contents

| File | Purpose |
|---|---|
| `srcs/histogram_logger.py` | The logger itself. Run this on the scope PC. |
| `srcs/lecroy_hist.py` | Read, verify, compare, and pack the snapshots. |
| `srcs/show_mapping.py` | Print which parameter each math function histograms. |
| `legacy/histogram_logger.vbs` | The text-file logger this replaces. Kept so old files stay readable. |
| `tests/` | Tests for the storage, recovery, and reader. Run anywhere; no scope needed. |
| `requirements.txt` | Pinned dependencies for Windows 11 / Python 3.10–3.13. |
| `requirements-py38.txt` | Same, for Windows 7 / Python 3.8 scope PCs. |
| `docs/SETUP.md` | First-time setup on a scope PC (Python, packages, scope link). |
| `docs/RUNNING.md` | How to start, monitor, and stop a run; what a crash/restart does. |
| `docs/DATA_FORMAT.md` | Full schema of the HDF5 files and the rescue CSV fallback. |

## Quick start

```powershell
pip install -r requirements.txt
python srcs/histogram_logger.py
```

Histograms get written under `C:\Histograms\<year>_<month>\...h5` (or one
file per month — see `docs/SETUP.md`), with status lines echoed to the console
and appended to `C:\Histograms\logger.log`.

## Day-to-day commands

On the scope PCs the script is deployed to `C:\Scripts\histogram_logger.py`
and run without a console, so these four commands cover a normal session.
`docs/RUNNING.md` explains each one in full.

| Task | Command |
|---|---|
| Start a run (returns immediately) | `pythonw C:\Scripts\histogram_logger.py` |
| Check it is alive / find its PID | `tasklist /fi "imagename eq pythonw.exe"` |
| Stop it (repeat `/pid` for several) | `taskkill /f /pid 13292` |
| Read the status log | `type C:\Histograms\logger.log` |

Check the log after starting: the `logger started (pid ...)` line confirms
it came up, and tells you which PID to kill later.

See `docs/RUNNING.md` for the full walkthrough and `docs/DATA_FORMAT.md` for
how to read the data back out with `srcs/lecroy_hist.py`.

## Status

Runs on the LeCroy scope(s) used for Bi-207 purity monitor calibration in
liquid argon. See the project notes for current scope inventory and any
known issues (e.g. snapshot stalls on a given scope).
