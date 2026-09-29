"""The reader has to accept exactly what the logger writes, with no scope attached."""
import importlib.util
import math
import os
import shutil
import sys
import tempfile
import types
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "srcs")
sys.path.insert(0, SRC)

import lecroy_hist as lh  # noqa: E402


def load_logger(tmpdir):
    spec = importlib.util.spec_from_file_location("histogram_logger_reader_test",
                                                   os.path.join(SRC, "histogram_logger.py"))
    hl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hl)
    hl.BASE_FOLDER = tmpdir
    hl.LOG_FILE = os.path.join(tmpdir, "logger.log")
    hl.FUNC_NAMES = ["F1", "F2"]
    hl.say = lambda msg: None
    return hl


class CrossCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hl = load_logger(self.tmp)

    def hist(self, counts, first, last, units="s"):
        return dict(width=1.25e-10, offset=-1.2e-07, first=first, last=last,
                    counts=np.asarray(counts, dtype=float), units=units)

    def test_hdf5_from_the_logger_verifies(self):
        path = os.path.join(self.tmp, "day.h5")
        counts = [0, 0, 0, 7, 14, 21]
        self.hl._descriptions["F1"] = "Histogram(C1)"
        self.hl.append_snapshot(path, "2026-09-06T14:00:00",
                                {"F1": self.hist(counts, 3, 5), "F2": None},
                                duration_s=3601.0, read_s=2.5)
        self.assertEqual(lh.main(["verify", path]), 0)
        snaps = list(lh.hdf5_snapshots(path))
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertAlmostEqual(snap.duration_s, 3601.0)
        self.assertAlmostEqual(snap.read_s, 2.5)
        self.assertEqual(snap.interval_min, 60)
        self.assertEqual(snap.descriptions["F1"], "Histogram(C1)")
        self.assertEqual(snap.units["F1"], "s")
        self.assertEqual(snap.unavailable, ["F2"])
        self.assertEqual(snap.hists["F1"].counts.tolist(), [7.0, 14.0, 21.0])
        self.assertIn("duration=3601s", lh._describe(snap))
        self.assertIn("Histogram(C1)", lh._describe(snap))

    def test_rescue_csv_verifies_and_matches_the_hdf5_row(self):
        counts = [0, 5, 6, 0]
        results = {"F1": self.hist(counts, 1, 2), "F2": None}
        h5 = os.path.join(self.tmp, "day.h5")
        csv = os.path.join(self.tmp, "day.rescue_1400.csv")
        self.hl.append_snapshot(h5, "2026-09-06T14:00:00", results, duration_s=3601.0)
        self.hl.write_rescue_text(csv, "2026-09-06T14:00:00", results, duration_s=3601.0)
        self.assertEqual(lh.main(["verify", csv]), 0)
        text = lh.read_snapshot(csv)
        self.assertAlmostEqual(text.duration_s, 3601.0)
        same, msgs = lh.compare_snapshots(text, list(lh.hdf5_snapshots(h5))[0])
        self.assertTrue(same, msgs)

    def test_unknown_duration_is_nan_and_still_verifies(self):
        csv = os.path.join(self.tmp, "nodur.csv")
        self.hl.write_rescue_text(csv, "2026-09-06T14:00:00",
                                  {"F1": self.hist([1, 2, 3], 0, 2)})
        snap = lh.read_snapshot(csv)
        self.assertTrue(math.isnan(snap.duration_s))
        self.assertEqual(lh.main(["verify", csv]), 0)

    def test_pack_carries_duration_through(self):
        csv = os.path.join(self.tmp, "a.csv")
        self.hl.write_rescue_text(csv, "2026-09-06T14:00:00",
                                  {"F1": self.hist([1, 2, 3], 0, 2), "F2": None},
                                  duration_s=3599.0)
        out = os.path.join(self.tmp, "month.h5")
        self.assertEqual(lh.pack_hdf5([csv], out), 1)
        snap = next(lh.hdf5_snapshots(out))
        self.assertAlmostEqual(snap.duration_s, 3599.0)
        self.assertEqual(snap.interval_min, 60)
        self.assertIsNone(snap.read_s)          # pack has no readout timing


class DescribeFunction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="desc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hl = load_logger(self.tmp)

    def scope_with(self, **attrs):
        result = types.SimpleNamespace(**{k: v for k, v in attrs.items() if k.startswith("unit_")})
        # function-level properties live on the function, result-level on Out.Result
        fn_attrs = {k: v for k, v in attrs.items() if not k.startswith("unit_")}
        fn = types.SimpleNamespace(Out=types.SimpleNamespace(Result=result), **fn_attrs)
        self.hl.scope = types.SimpleNamespace(
            Math=types.SimpleNamespace(Functions=lambda name: fn))

    def test_equation_wins(self):
        self.scope_with(Equation="Histogram(C2)", Operator1Name="Histogram", Source1="C1")
        self.assertEqual(self.hl.describe_function("F1"), "Histogram(C2)")
        self.scope_with(Equation="something else")
        self.assertEqual(self.hl.describe_function("F1"), "Histogram(C2)", "cached")

    def test_operator_and_source_when_there_is_no_equation(self):
        wrapped = types.SimpleNamespace(Value="Histogram")
        self.scope_with(Operator1Name=wrapped, Source1="C3")
        self.assertEqual(self.hl.describe_function("F1"), "Histogram(C3)")

    def test_missing_properties_are_empty_not_fatal(self):
        self.hl.scope = types.SimpleNamespace(
            Math=types.SimpleNamespace(Functions=lambda name: types.SimpleNamespace()))
        self.assertEqual(self.hl.describe_function("F1"), "")

    def test_axis_units_tries_the_known_names(self):
        h = types.SimpleNamespace(HorizontalUnits=types.SimpleNamespace(Value="s"))
        self.assertEqual(self.hl.axis_units(h), "s")
        self.assertEqual(self.hl.axis_units(types.SimpleNamespace()), "")


if __name__ == "__main__":
    unittest.main()
