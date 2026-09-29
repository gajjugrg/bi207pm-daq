# Running a logging session

The examples below assume the script has been copied to
`C:\Scripts\histogram_logger.py` on the scope PC, which is where it lives on
the machines in use. Substitute your own path if you put it elsewhere.

## Starting a run

**Without a console** — this is the normal way to start a run. Status goes
only to `LOG_FILE`:

```bat
pythonw C:\Scripts\histogram_logger.py
```

The command returns immediately and the logger keeps running in the
background; there is no window to close by accident. Confirm it actually
came up by checking the log (see below) for the `logger started` line.

**With a console** — useful when you want live status lines while you set a
scope up:

```bat
python C:\Scripts\histogram_logger.py
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

## Checking that a run is live

`pythonw` runs with no window, so the only way to see it is in the process
list:

```bat
tasklist /fi "imagename eq pythonw.exe"
```

```
Image Name                     PID Session Name        Session#    Mem Usage
========================= ======== ================ =========== ============
pythonw.exe                  13292 Console                    1     42,168 K
```

An empty result ("INFO: No tasks are running which match the specified
criteria.") means the logger is not running.

This lists *every* `pythonw.exe` on the machine, not just this logger, so if
other Python scripts run on the same scope PC you need to tell them apart
before acting on a PID. Two ways to do that:

- The logger writes its own PID to the log on startup — the `logger started
  (pid 13292, ...)` line. That is the authoritative match.
- Or ask Windows for the command line behind each process:

  ```powershell
  Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" |
      Select-Object ProcessId, CommandLine
  ```

## Reading the log

Every status line is appended to `C:\Histograms\logger.log` (the `LOG_FILE`
setting). Dump the whole file with:

```bat
type C:\Histograms\logger.log
```

For a long-running session the whole file is usually more than you want.
From PowerShell, the last 20 lines, or a live feed that updates as snapshots
are taken:

```powershell
Get-Content C:\Histograms\logger.log -Tail 20
Get-Content C:\Histograms\logger.log -Tail 20 -Wait
```

Once the log passes `MAX_LOG_MB` (5 MB by default) it is rolled to
`C:\Histograms\logger.log.1` and a fresh `logger.log` is started, so check
the `.1` file too if you are looking for something older than the current
log. Only one rolled generation is kept — anything older is discarded.

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

If you started it with a console, `Ctrl+C` in that window is enough.

A `pythonw` run has no console, so stop it by PID. Find the PID(s) first:

```bat
tasklist /fi "imagename eq pythonw.exe"
```

then kill them:

```bat
taskkill /f /pid 13292
```

`/f` forces termination; without it Windows asks the process to close
politely and a `pythonw` process with no message loop will ignore that.
Several PIDs can be passed in one call by repeating `/pid`, which is handy
when an earlier run was left behind and more than one logger is competing
for the same output file:

```bat
taskkill /f /pid 13292 /pid 1180 /pid 5308
```

Kill only the PIDs you have confirmed are loggers — see "Checking that a run
is live" above. Every `pythonw.exe` on the machine shows up in that list,
and `/f` gives the other ones no chance to shut down cleanly.

Then verify nothing is left:

```bat
tasklist /fi "imagename eq pythonw.exe"
```

The current `.h5` file is safe to interrupt between snapshots — each
snapshot is a complete, flushed append, and no partial-row state is left
behind. The one window to avoid is the append itself (the few seconds after
a `row N` line is due): a kill in the middle of it can leave a row whose
`available` flags are all `False` and whose counts are zero. That row is
harmless as long as readers mask on `available`, but if you want a clean
cut, check the log for the most recent snapshot line and stop the process
just after it.

## Restarting after a stop

Just run it again:

```bat
pythonw C:\Scripts\histogram_logger.py
```

It reconnects, clears sweeps, and starts a fresh snapshot schedule from
"now" (or the next clock mark, if `ALIGN_TO_CLOCK = True`). Rows keep
appending to the same day's/month's `.h5` file — nothing needs to be renamed
or moved first.

A stop-and-start is also the remedy when the COM link to the scope has gone
stale, which the log shows as repeated `ClearSweeps.ActNow() failed` lines
or as every snapshot recording `0/5 histograms`. Rows keep being written in
that state, so the row count alone will not tell you anything is wrong —
read the log. Restart the X-Stream application first if it is the thing that
died, then `taskkill` the logger and start it again.
