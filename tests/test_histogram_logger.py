"""Tests for histogram_logger that do not need a scope.

Everything below the COM boundary runs against a stub scope object, which is the
whole point: the real logger only runs on a Windows PC wired to the instrument,
so without these the storage and recovery logic can only be tested by taking a
beamline machine out of service.

Run with:   python -m unittest discover -s tests
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest

import h5py
import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "srcs", "histogram_logger.py")


def load_logger(tmpdir):
    """A fresh copy of the module, writing inside tmpdir and logging to a list."""
    spec = importlib.util.spec_from_file_location("histogram_logger_under_test", SRC)
    hl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hl)
    hl.BASE_FOLDER = tmpdir
    hl.LOG_FILE = os.path.join(tmpdir, "logger.log")
    hl.FUNC_NAMES = ["F1", "F2"]
    hl.POLL_SLEEP_S = 0
    hl.POLL_MAX_TRIES = 2
    hl.messages = []
    hl.say = hl.messages.append      # keeps the tests quiet and off the filesystem
    return hl


class FakeResult:
    """One math function's Out.Result, as the COM layer would present it."""

    def __init__(self, counts, first, last, width=1.0, offset=0.0):
        self.BinPopulations = list(counts)
        self.FirstPopulatedBin = first
        self.LastPopulatedBin = last
        self.BinWidth = width
        self.OffsetAtLeftEdge = offset


class FakeScope:
    """Stands in for the LeCroy.XStreamDSO application object."""

    def __init__(self, results=None, clear_raises=False):
        self.results = dict(results or {})
        self.clear_raises = clear_raises
        self.clears = 0
        self.Math = types.SimpleNamespace(Functions=self._function)
        self.ClearSweeps = types.SimpleNamespace(ActNow=self._clear)

    def _function(self, name):
        if name not in self.results:
            raise RuntimeError("function %s is not configured" % name)
        return types.SimpleNamespace(Out=types.SimpleNamespace(Result=self.results[name]))

    def _clear(self):
        if self.clear_raises:
            raise OSError("-2147023174 RPC server is unavailable")
        self.clears += 1


class DeadScope:
    """A COM object whose backing application has exited."""

    def __getattr__(self, name):
        raise OSError("-2147023174 RPC server is unavailable")


class LoggerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="histlog-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hl = load_logger(self.tmp)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def result(self, counts, first, last, width=1.0, offset=0.0):
        """What read_histogram() hands to the storage layer."""
        return dict(width=width, offset=offset, first=first, last=last,
                    counts=np.asarray(counts, dtype=float))


class TestReadHistogram(LoggerTestCase):
    def test_single_populated_bin_is_kept(self):
        """A one-bin histogram is real data, not an unavailable function."""
        self.hl.scope = FakeScope({"F1": FakeResult([0, 0, 7, 0, 0], 2, 2)})
        r = self.hl.read_histogram("F1")
        self.assertIsNotNone(r, "a histogram with one populated bin was discarded")
        self.assertEqual((r["first"], r["last"]), (2, 2))

    def test_empty_histogram_is_unavailable(self):
        self.hl.scope = FakeScope({"F1": FakeResult([0, 0, 0, 0], 0, 0)})
        self.assertIsNone(self.hl.read_histogram("F1"))

    def test_normal_histogram(self):
        self.hl.scope = FakeScope({"F1": FakeResult([0, 3, 4, 0], 1, 2, width=0.5, offset=-1.0)})
        r = self.hl.read_histogram("F1")
        self.assertEqual((r["first"], r["last"], r["width"], r["offset"]), (1, 2, 0.5, -1.0))

    def test_zero_bin_width_is_unavailable(self):
        self.hl.scope = FakeScope({"F1": FakeResult([1, 2, 3], 0, 2, width=0)})
        self.assertIsNone(self.hl.read_histogram("F1"))

    def test_missing_function_is_unavailable(self):
        self.hl.scope = FakeScope({})
        self.assertIsNone(self.hl.read_histogram("F1"))

    def test_dead_scope_reads_as_unavailable(self):
        self.hl.scope = DeadScope()
        self.assertIsNone(self.hl.read_histogram("F1"))

    def test_negative_and_non_finite_bins_become_zero(self):
        """Casting -1.0 to uint64 would silently store 1.8e19 with a valid checksum."""
        self.hl.scope = FakeScope({"F1": FakeResult([-1.0, 5.0, float("nan"), 2.0], 0, 3)})
        r = self.hl.read_histogram("F1")
        self.assertEqual(r["counts"].tolist(), [0.0, 5.0, 0.0, 2.0])
        self.assertTrue(any("negative" in m for m in self.hl.messages),
                        "sanitising bad bins should be logged, not silent")


class TestNum(LoggerTestCase):
    def test_parses_com_values(self):
        for raw, expected in [("1.5ns", 1.5), (3, 3.0), (2.5, 2.5), ("1e3", 1000.0),
                              ("-2.5E-3", -0.0025), ("abc", 0.0), ("", 0.0)]:
            self.assertEqual(self.hl.num(raw), expected, raw)


class TestBuildPath(LoggerTestCase):
    def test_day_and_month(self):
        from datetime import datetime
        when = datetime(2026, 9, 6, 13, 0)
        self.hl.FILE_PERIOD = "day"
        self.assertEqual(self.hl.build_path(when), self.path("2026_Sep", "2026_Sep_06.h5"))
        self.hl.FILE_PERIOD = "month"
        self.assertEqual(self.hl.build_path(when), self.path("2026_Sep.h5"))


class TestAppendSnapshot(LoggerTestCase):
    def test_rows_stay_aligned_when_functions_come_and_go(self):
        p = self.path("a.h5")
        rows = [{"F1": None, "F2": self.result([1, 2, 3, 4], 0, 3)},
                {"F1": self.result([5, 6, 7, 8], 0, 3), "F2": None},
                {"F1": None, "F2": None},
                {"F1": self.result([9, 9, 9, 9], 0, 3), "F2": self.result([1, 1, 1, 1], 0, 3)}]
        for k, res in enumerate(rows):
            self.assertEqual(self.hl.append_snapshot(p, "2026-09-0%dT00:00:00" % (k + 1), res), k)
        with h5py.File(p, "r") as hf:
            self.assertEqual(hf["timestamp"].shape[0], 4)
            for key in ("duration_s", "read_s"):
                self.assertEqual(hf[key].shape[0], 4)
            self.assertEqual(hf["F1"]["available"][:].tolist(), [False, True, False, True])
            self.assertEqual(hf["F2"]["available"][:].tolist(), [True, False, False, True])
            for name in ("F1", "F2"):
                self.assertEqual(hf[name]["counts"].shape[0], 4)
            self.assertEqual(hf["F1"]["counts"][1].tolist(), [5, 6, 7, 8])
            self.assertEqual(hf["F1"]["counts"][0].tolist(), [0, 0, 0, 0])

    def test_counts_widen_when_the_scope_reports_more_bins(self):
        p = self.path("b.h5")
        self.hl.append_snapshot(p, "2026-09-01T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)})
        self.hl.append_snapshot(p, "2026-09-01T01:00:00", {"F1": self.result([1, 2, 3, 4, 5], 0, 4)})
        with h5py.File(p, "r") as hf:
            self.assertEqual(hf["F1"]["counts"].shape, (2, 5))
            self.assertEqual(hf["F1"]["counts"][0].tolist(), [1, 2, 3, 0, 0])
            self.assertEqual(hf["F1"]["n_bins"][:].tolist(), [3, 5])

    def test_one_chunk_per_row(self):
        """Multi-row compressed chunks were measured at ~6x bloat; keep it at one."""
        p = self.path("c.h5")
        self.hl.append_snapshot(p, "2026-09-01T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)})
        with h5py.File(p, "r") as hf:
            self.assertEqual(hf["F1"]["counts"].chunks[0], 1)
            self.assertTrue(hf["F1"]["counts"].fletcher32)

    def test_duration_and_read_time_are_recorded(self):
        p = self.path("d.h5")
        self.hl.append_snapshot(p, "2026-09-01T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)},
                                duration_s=3601.5, read_s=2.25)
        with h5py.File(p, "r") as hf:
            self.assertAlmostEqual(hf["duration_s"][0], 3601.5)
            self.assertAlmostEqual(hf["read_s"][0], 2.25)

    def test_column_missing_from_an_older_file_is_backfilled(self):
        """A file written before a column existed must not end up row-misaligned."""
        p = self.path("legacy.h5")
        with h5py.File(p, "w") as hf:
            hf.create_dataset("timestamp", shape=(2,), maxshape=(None,), dtype="S19")
        self.hl.append_snapshot(p, "2026-09-01T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)},
                                duration_s=3600.0, read_s=1.0)
        with h5py.File(p, "r") as hf:
            self.assertEqual(hf["timestamp"].shape[0], 3)
            for key in ("duration_s", "read_s"):
                self.assertEqual(hf[key].shape[0], 3, "%s is out of step with /timestamp" % key)
                self.assertTrue(np.isnan(hf[key][0]) and np.isnan(hf[key][1]))
            self.assertAlmostEqual(hf["duration_s"][2], 3600.0)

    def test_creates_the_output_folder(self):
        p = self.path("2026_Sep", "2026_Sep_06.h5")
        self.hl.append_snapshot(p, "2026-09-06T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)})
        self.assertTrue(os.path.exists(p))


class TestRescueText(LoggerTestCase):
    def read(self, p):
        with open(p) as f:
            return f.read().replace("\r\n", "\n").splitlines()

    def test_creates_missing_directories(self):
        """The usual cause of an HDF5 failure is an unwritable folder; don't repeat it."""
        p = self.path("nope", "deeper", "x.rescue_1200.csv")
        self.hl.write_rescue_text(p, "2026-09-01T00:00:00", {"F1": self.result([1, 2, 3], 0, 2)})
        self.assertTrue(os.path.exists(p))

    def test_carries_duration(self):
        p = self.path("x.rescue_1200.csv")
        self.hl.write_rescue_text(p, "2026-09-01T00:00:00",
                                  {"F1": self.result([0, 5, 6, 0], 1, 2)}, duration_s=3601.0)
        lines = self.read(p)
        self.assertIn("#duration_s=3601", lines)

    def test_format_matches_the_text_logger(self):
        p = self.path("y.rescue_1200.csv")
        self.hl.write_rescue_text(p, "2026-09-01T00:00:00",
                                  {"F1": self.result([0, 5, 6, 0], 1, 2), "F2": None})
        lines = self.read(p)
        self.assertEqual(lines[0], "#lecroy-histograms v2")
        self.assertEqual(lines[-2], "F1,1,0,1,2,4,11,5,6")
        self.assertEqual(lines[-1], "F2,unavailable")


class TestRescueFallback(LoggerTestCase):
    def test_falls_back_to_the_log_folder(self):
        blocked = self.path("blocker")
        with open(blocked, "w") as f:            # a file where a folder is needed
            f.write("")
        status = self.hl._rescue(os.path.join(blocked, "sub", "2026_Sep_06.h5"),
                                 __import__("datetime").datetime(2026, 9, 6, 12, 0),
                                 "2026-09-06T12:00:00", {"F1": self.result([1, 2, 3], 0, 2)},
                                 3600.0, OSError("disk full"))
        landed = self.path("2026_Sep_06.rescue_1200.csv")
        self.assertTrue(os.path.exists(landed), status)
        self.assertIn("saved as", status)

    def test_reports_loss_only_when_every_location_fails(self):
        blocked = self.path("blocker")
        with open(blocked, "w") as f:
            f.write("")
        self.hl.LOG_FILE = os.path.join(blocked, "logger.log")
        self.hl.tempfile = types.SimpleNamespace(gettempdir=lambda: os.path.join(blocked, "tmp"))
        status = self.hl._rescue(os.path.join(blocked, "sub", "x.h5"),
                                 __import__("datetime").datetime(2026, 9, 6, 12, 0),
                                 "2026-09-06T12:00:00", {"F1": self.result([1, 2, 3], 0, 2)},
                                 3600.0, OSError("disk full"))
        self.assertIn("SNAPSHOT LOST", status)


class TestLogOnce(LoggerTestCase):
    def test_reports_how_many_functions_were_read(self):
        from datetime import datetime
        self.hl.scope = FakeScope({"F1": FakeResult([1, 2, 3, 4], 0, 3)})   # F2 absent
        status, n_ok = self.hl.log_once(datetime(2026, 9, 6, 12, 0), 3600.0)
        self.assertEqual(n_ok, 1)
        self.assertIn("1/2 histograms", status)

    def test_dead_scope_yields_zero_and_still_writes_a_row(self):
        from datetime import datetime
        self.hl.scope = DeadScope()
        status, n_ok = self.hl.log_once(datetime(2026, 9, 6, 12, 0), 3600.0)
        self.assertEqual(n_ok, 0, "a dead link must be visible to the caller")
        self.assertIn("0/2 histograms", status)


class TestClearSweeps(LoggerTestCase):
    def test_returns_empty_string_on_a_dead_scope(self):
        self.hl.scope = DeadScope()
        self.hl._verified = True
        self.assertEqual(self.hl.clear_sweeps(), "")

    def test_reports_the_method_on_success(self):
        self.hl.scope = FakeScope({"F1": FakeResult([1, 2, 3], 0, 2)})
        self.hl._verified = True
        self.assertTrue(self.hl.clear_sweeps())
        self.assertEqual(self.hl.scope.clears, 1)

    def test_warns_when_clearing_does_not_clear(self):
        scope = FakeScope({"F1": FakeResult([10, 10, 10], 0, 2)})
        self.hl.scope = scope
        self.hl._verified = False
        self.hl.time = types.SimpleNamespace(sleep=lambda s: None, time=lambda: 0.0)
        self.assertTrue(self.hl.clear_sweeps())
        self.assertTrue(any("did not drop" in m for m in self.hl.messages))


class TestMaintainLink(LoggerTestCase):
    def setUp(self):
        super().setUp()
        self.connects = []
        self.hl.connect = lambda *a, **k: self.connects.append(1)
        self.hl.clear_sweeps = lambda: "app.ClearSweeps.ActNow()"

    def test_healthy_cycle_does_nothing(self):
        self.assertEqual(self.hl.maintain_link(True, 2, 2), (True, 0))
        self.assertEqual(self.connects, [])

    def test_failed_clear_reconnects_immediately(self):
        """The bug this replaces: clear_sweeps returns "" and never raises."""
        cleared, dead = self.hl.maintain_link(False, 0, 0)
        self.assertEqual(len(self.connects), 1)
        self.assertTrue(cleared, "a fresh connection should be cleared before use")
        self.assertEqual(dead, 0)

    def test_empty_readouts_reconnect_only_after_the_threshold(self):
        self.hl.RECONNECT_AFTER_DEAD_CYCLES = 3
        dead = 0
        for expected in (1, 2):
            cleared, dead = self.hl.maintain_link(True, 0, dead)
            self.assertEqual(dead, expected)
            self.assertEqual(self.connects, [], "one quiet function is not a dead link")
        cleared, dead = self.hl.maintain_link(True, 0, dead)
        self.assertEqual(len(self.connects), 1)
        self.assertEqual(dead, 0)

    def test_a_single_good_readout_clears_the_count(self):
        self.assertEqual(self.hl.maintain_link(True, 1, 2), (True, 0))
        self.assertEqual(self.connects, [])

    def test_failed_reconnect_is_survivable(self):
        def boom(*a, **k):
            raise OSError("scope application not running")
        self.hl.connect = boom
        cleared, dead = self.hl.maintain_link(False, 0, 0)
        self.assertFalse(cleared, "histograms were not cleared, so duration must keep running")
        self.assertTrue(any("reconnect failed" in m for m in self.hl.messages))


class TestConnect(LoggerTestCase):
    def install_fake_win32(self, fail_times=0):
        calls = {"n": 0}
        obj = FakeScope()

        def dispatch(progid):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise OSError("server execution failed")
            return obj

        client = types.ModuleType("win32com.client")
        client.Dispatch = dispatch
        pkg = types.ModuleType("win32com")
        pkg.client = client
        self.addCleanup(sys.modules.pop, "win32com", None)
        self.addCleanup(sys.modules.pop, "win32com.client", None)
        sys.modules["win32com"] = pkg
        sys.modules["win32com.client"] = client
        return calls

    def test_resets_the_clear_verification(self):
        """A new COM object may be a new app instance, so re-check that clears clear."""
        self.install_fake_win32()
        self.hl._verified = True
        self.hl.connect()
        self.assertFalse(self.hl._verified)

    def test_retries_while_the_scope_application_starts(self):
        calls = self.install_fake_win32(fail_times=2)
        self.hl.connect(retries=4, delay_s=0)
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_the_last_retry(self):
        self.install_fake_win32(fail_times=99)
        with self.assertRaises(OSError):
            self.hl.connect(retries=2, delay_s=0)


if __name__ == "__main__":
    unittest.main()
