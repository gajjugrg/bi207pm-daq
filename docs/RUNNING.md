# Running a logging session

## Starting a run

**With a console** (you'll see live status lines):

```powershell
python histogram_logger.py
```

**Without a console** (for a long unattended run — status only goes to
`LOG_FILE`):

```powershell
pythonw histogram_logger.py
```

Use `pythonw`, not a minimized `python` console, for anything you plan to
leave running for days: a `python` console with QuickEdit enabled freezes the
whole process the moment someone clicks inside the window, until a key is
pressed. `pythonw` has no console to click into, so that failure mode is
gone.

To keep it running across a Remote Desktop disconnect, launch it inside a
tool that survives logoff (e.g. a scheduled task, or `Task Scheduler` set to
"run whether user is logged on or not"), rather than a plain terminal window.

## What happens on start

1. Connects to the running scope app over COM.
2. Clears the scope's histograms (`ClearSweeps.ActNow()`), verifying once
   that the population actually dropped.
3. Computes the first snapshot time:
   - `ALIGN_TO_CLOCK = False` (default): first snapshot is `INTERVAL_MINUTES`
     from *now*.
   - `ALIGN_TO_CLOCK = True`: first snapshot lands on the next clock-aligned
     mark (e.g. next `:00` past the hour for a 60-minute interval).
4. Loops forever: sleep until the next scheduled snapshot → read all
   `FUNC_NAMES` → append one row to the day's/month's `.h5` file → clear
   sweeps again → schedule the next snapshot.

## Monitoring a run

- Console output (if any) and `C:\Histograms\logger.log` show one line per
  snapshot, e.g.:
  ```
  2026-09-11 14:00:03  C:\Histograms\2026_Sep\2026_Sep_11.h5 row 14  (5/5 histograms, read 2.1s)
  ```
- A snapshot that's slow to read gets flagged in that same line, e.g.
  `[slow: F3 21s]` — useful for spotting a function that isn't triggering.
- If a whole cycle overruns `INTERVAL_MINUTES` (readout took longer than the
  interval), the log says how many slots were skipped and suggests raising
  `INTERVAL_MINUTES`.

## If something goes wrong mid-run

- **A single HDF5 append fails** (disk full, file locked, etc.): that one
  snapshot is written instead as `<name>.rescue_HHMM.csv` next to the `.h5`
  file, in the old text format, and the loop continues. Nothing is silently
  lost — check for stray `.rescue_*.csv` files after a run and fold them in
  by hand if needed.
- **The scope app itself dies or gets restarted**: the next `clear_sweeps()`
  call fails, the script logs `clear failed (...); reconnecting` and tries to
  re-`connect()`. If reconnecting also fails, it logs that and keeps trying
  on the following cycle — it does not exit.
- **The whole process crashes** (uncaught exception outside the main loop):
  the traceback is written to the log under `CRASHED:` and the process exits.
  There's no auto-restart built in — wrap the launch in a scheduled task or
  a supervisor script if you need one.

## Stopping a run

`Ctrl+C` in the console, or end the `python`/`pythonw` process (Task
Manager). The current `.h5` file is safe to interrupt between snapshots —
each snapshot is a complete, flushed append; there's no partial-row state
left behind.


## Restarting after a stop

Just run it again. It reconnects, clears sweeps, and starts a fresh
snapshot schedule from "now" (or the next clock mark, if
`ALIGN_TO_CLOCK = True`). Rows keep appending to the same day's/month's
`.h5` file — nothing needs to be renamed or moved first.
