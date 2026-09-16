# Setup

This runs **on the scope PC itself** (not on a remote machine) — it talks to
the scope over a local COM/ActiveX connection

## 1. Prerequisites

- LeCroy X-Stream scope application installed and running on the PC, with a
  live acquisition set up so that the math functions you care about
  (`F1`...`F5`/`F6`) are configured as **histograms**.
- Python 3.
  - Windows 7 scopes: use **Python 3.8** — the last version with an official
    Windows 7 installer.
  - Windows 11 scopes: any current Python 3.x works.
- Admin rights are *not* required to run the logger, only to install Python
  itself if it isn't already on the machine.

## 2. Install dependencies

```powershell
pip install -r requirements.txt
```

This installs `pywin32` (COM access to the scope app), `h5py` (HDF5 writer),
and `numpy`.

`pywin32` needs its post-install step run once per machine:

```powershell
python -m pywin32_postinstall -install
```

(Skip this if `pywin32` was already set up for another script on the same
PC.)

## 3. Configure

All settings are constants at the top of `histogram_logger.py` — there is no
separate config file:

| Setting | Meaning | Default |
|---|---|---|
| `BASE_FOLDER` | Root folder for output `.h5` files and the log | `C:\Histograms` |
| `INTERVAL_MINUTES` | Minutes between clear → read cycles | `60` |
| `FUNC_NAMES` | Which scope math functions to read | `["F1","F2","F3","F4","F5"]` |
| `FILE_PERIOD` | `"day"` → one `.h5` per day, `"month"` → one per month | `"day"` |
| `ALIGN_TO_CLOCK` | If `True`, snapshots land on clock marks (e.g. every 60 min on the hour) instead of 60 min after the script started | `False` |
| `GZIP_LEVEL` | HDF5 gzip compression level for histogram counts | `6` |
| `LOG_FILE` | Where status lines get appended | `C:\Histograms\logger.log` |
| `MAX_LOG_MB` | Log gets rolled to `<name>.1` past this size | `5` |

Edit these directly in the script before a run. Add `"F6"`, `"F7"`, `"F8"` to
`FUNC_NAMES` if you have more histogram functions configured on the scope.

## 4. Sanity-check the scope link once

The first `clear_sweeps()` call in a session checks that clearing the scope's
histograms actually drops the accumulated population — some firmware/COM
combinations return cleanly from `ClearSweeps.ActNow()` without clearing
anything. If you see:

```
WARNING: sweeps did not drop (... -> ...) - the histograms may not be
resetting, so snapshots would accumulate.
```

stop and check the scope firmware / COM binding before trusting a run. This
check only runs once per process start.

Next: `RUNNING.md` for how to actually start and monitor a run.
