"""show_mapping.py - print which parameter/channel each math function histograms.

Run on the scope PC, from this folder:   py show_mapping.py

The same strings are recorded into every HDF5 file the logger writes, as the
`description` and `units` attributes of each function group, so a file read
back later still says what it contains. This is the quick way to check the
mapping before starting a run.
"""
import histogram_logger as log

log.connect()
for name in log.FUNC_NAMES:
    try:
        units = log.axis_units(log.scope.Math.Functions(name).Out.Result)
    except Exception:  # noqa: BLE001 - function off, or not a histogram
        units = ""
    description = log.describe_function(name) or "(not reported by the scope)"
    print(f"{name}: {description}" + (f"  [{units}]" if units else ""))
