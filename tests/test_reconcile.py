# -*- coding: utf-8 -*-
"""Pine/Python debug-field consistency.

TradingView cannot be driven from here, so what is testable is the machinery:
that the reconciler pairs the documented debug fields correctly, passes on an
exact match, and fails loudly - naming the first offending bar and field - on
any disagreement outside the published tolerances. The Pine side of the
contract is enforced by the reason-code table being duplicated verbatim in the
script and in msvsd/__init__.py, which this module also checks.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd import REASON_CODES                                  # noqa: E402
from msvsd.config import RunConfig                              # noqa: E402
from msvsd.reconcile import (ALL_FIELDS, python_frame, reconcile,
                             load_pine_export)                  # noqa: E402
from msvsd.run import build_and_run                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINE = os.path.join(ROOT, "XAU_MultiSpeed_VolScaled_Donchian.pine")


def fake_export(res, path, tweak=None):
    """Write a TradingView-shaped CSV from the Python run itself.

    Column names carry the 'dbg_' prefix and a plot-title prefix, exactly as
    TradingView emits them, so the matcher is exercised rather than bypassed.
    """
    f = python_frame(res).iloc[300:1200].copy()
    if tweak:
        tweak(f)
    out = pd.DataFrame({"time": f["time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
    for c in ALL_FIELDS:
        out["XAU MS-VSD: dbg_" + c] = f[c].to_numpy()
    out.to_csv(path, index=False)
    return path


class TestReasonCodeContract(unittest.TestCase):
    def test_pine_and_python_declare_the_same_codes(self):
        with open(PINE, encoding="utf-8") as fh:
            src = fh.read()
        block = src.split("REASON CODES")[1].split("STATE CODES")[0]
        found = dict(re.findall(r"//\s+(\d+)\s+([A-Z_]+)", block))
        self.assertTrue(found, "no reason-code table found in the Pine script")
        for code, name in found.items():
            self.assertIn(int(code), REASON_CODES,
                          "Pine declares code %s that Python does not know" % code)
            self.assertEqual(REASON_CODES[int(code)], name,
                             "code %s: Pine says %s, Python says %s"
                             % (code, name, REASON_CODES[int(code)]))
        for code in REASON_CODES:
            self.assertIn(str(code), found,
                          "Python code %d is undocumented in the Pine" % code)

    def test_pine_exports_every_field_the_reconciler_compares(self):
        with open(PINE, encoding="utf-8") as fh:
            src = fh.read()
        for f in ALL_FIELDS:
            self.assertIn('"dbg_%s"' % f, src,
                          "Pine debug export is missing dbg_%s" % f)

    def test_debug_export_is_gated_and_order_free(self):
        with open(PINE, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("dbgOn = input.bool(false", src,
                      "debug export must default to off")
        tail = src.split("8. DEBUG EXPORT")[1]
        for forbidden in ("strategy.order", "strategy.entry", "strategy.close",
                          "strategy.exit", ":="):
            self.assertNotIn(forbidden, tail,
                             "debug section must not change state or place orders "
                             "(found %r)" % forbidden)


class TestReconciler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = build_and_run(RunConfig(friday_basis="close"), verbose=False)
        cls.tmp = tempfile.mkdtemp(prefix="msvsd_recon_")

    def test_identical_export_passes(self):
        p = fake_export(self.res, os.path.join(self.tmp, "match.csv"))
        rep = reconcile(self.res, p, self.tmp, "match")
        self.assertEqual(rep["result"], "PASS", rep.get("first_mismatch"))
        self.assertEqual(rep["mismatch_count"], 0)
        self.assertGreater(rep["overlapping_bars"], 500)
        self.assertEqual(rep["fields_missing_from_export"], [])
        self.assertIn("RECONCILED", rep["statement"])

    def test_a_shifted_price_field_fails_and_names_the_bar(self):
        def tweak(f):
            f.loc[f.index[10], "stop_fast"] = float(f["stop_fast"].iloc[10]) + 0.5
        p = fake_export(self.res, os.path.join(self.tmp, "bad.csv"), tweak)
        rep = reconcile(self.res, p, self.tmp, "bad")
        self.assertEqual(rep["result"], "FAIL")
        self.assertEqual(rep["mismatch_count"], 1)
        self.assertEqual(rep["first_mismatch"]["field"], "stop_fast")
        self.assertGreater(rep["max_price_diff"], 0.4)
        self.assertIn("NOT RECONCILED", rep["statement"])
        self.assertTrue(os.path.isfile(rep["detail_csv"]))
        detail = pd.read_csv(rep["detail_csv"])
        self.assertEqual(len(detail), 1)

    def test_a_wrong_sleeve_state_fails_exactly(self):
        def tweak(f):
            f.loc[f.index[20], "state_slow"] = -1.0 if f["state_slow"].iloc[20] != -1 else 1.0
        p = fake_export(self.res, os.path.join(self.tmp, "state.csv"), tweak)
        rep = reconcile(self.res, p, self.tmp, "state")
        self.assertEqual(rep["result"], "FAIL")
        self.assertEqual(rep["first_mismatch"]["field"], "state_slow")

    def test_tiny_float_noise_is_inside_tolerance(self):
        def tweak(f):
            f["atr"] = f["atr"] + 1e-9
            f["net_target_lots"] = f["net_target_lots"] + 1e-12
        p = fake_export(self.res, os.path.join(self.tmp, "noise.csv"), tweak)
        rep = reconcile(self.res, p, self.tmp, "noise")
        self.assertEqual(rep["result"], "PASS")

    def test_a_qty_error_of_one_lot_step_is_caught(self):
        def tweak(f):
            f.loc[f.index[30], "qty_medium"] = float(f["qty_medium"].iloc[30]) + 0.01
        p = fake_export(self.res, os.path.join(self.tmp, "qty.csv"), tweak)
        rep = reconcile(self.res, p, self.tmp, "qty")
        self.assertEqual(rep["result"], "FAIL")
        self.assertAlmostEqual(rep["max_qty_diff"], 0.01, places=6)

    def test_missing_fields_are_reported_not_silently_skipped(self):
        p = os.path.join(self.tmp, "partial.csv")
        f = python_frame(self.res).iloc[300:900]
        out = pd.DataFrame({"time": f["time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "dbg_atr": f["atr"].to_numpy()})
        out.to_csv(p, index=False)
        rep = reconcile(self.res, p, self.tmp, "partial")
        self.assertEqual(rep["fields_present"], ["atr"])
        self.assertGreater(len(rep["fields_missing_from_export"]), 20)
        self.assertIn("were absent from the export", rep["statement"])

    def test_non_overlapping_export_fails_clearly(self):
        p = os.path.join(self.tmp, "nooverlap.csv")
        pd.DataFrame({"time": ["1990-01-01T00:00:00Z"], "dbg_atr": [1.0]}).to_csv(
            p, index=False)
        rep = reconcile(self.res, p, self.tmp, "nooverlap")
        self.assertEqual(rep["result"], "FAIL")
        self.assertIn("no overlapping bars", rep["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
