# -*- coding: utf-8 -*-
"""Campaign construction, resampling determinism, and the golden baseline."""
from __future__ import annotations

import json
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd.campaigns import build_campaigns, concentration, daily_frames  # noqa: E402
from msvsd.config import RunConfig                                        # noqa: E402
from msvsd.run import build_and_run                                       # noqa: E402
from msvsd.statistics import (block_bootstrap, campaign_bootstrap,
                              iid_bootstrap)                              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bars(states, net, equity=None):
    """Minimal bar frame with just the columns build_campaigns reads."""
    n = len(net)
    t = pd.date_range("2024-01-01", periods=n, freq="4h")
    d = {"time": t, "net_target_lots": np.array(net, float),
         "equity": np.array(equity if equity is not None else [100000.0] * n, float)}
    for k, name in enumerate(("fast", "medium", "slow")):
        col = [s[k] if k < len(s) else 0 for s in states]
        d["state_" + name] = np.array(col, float)
        d["qty_" + name] = np.abs(np.array(col, float)) * 0.01
    return pd.DataFrame(d)


EMPTY_TR = pd.DataFrame(columns=["entry_time", "exit_time", "gross", "lots",
                                 "stop_dist", "r_multiple"])
EMPTY_FL = pd.DataFrame(columns=["time", "spread_slip_usd", "commission_usd"])
EMPTY_FI = pd.DataFrame(columns=["date", "amount", "lot_nights"])


class TestCampaignConstruction(unittest.TestCase):
    def test_one_campaign_from_first_entry_to_all_flat(self):
        states = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 0, 0), (0, 0, 0), (0, 0, 0)]
        net = [0, 1, 2, 1, 0, 0]
        c = build_campaigns(_bars(states, net), EMPTY_TR, EMPTY_FL, EMPTY_FI)
        self.assertEqual(len(c), 1)
        self.assertEqual(c.iloc[0]["bars"], 4)          # bars 1..4
        self.assertEqual(c.iloc[0]["close_reason"], "flat")
        self.assertEqual(c.iloc[0]["n_sleeves"], 2)
        self.assertEqual(sorted(c.iloc[0]["sleeves"].split(",")), ["fast", "medium"])

    def test_two_campaigns_separated_by_flat(self):
        states = [(0, 0, 0), (1, 0, 0), (0, 0, 0), (0, 0, 0), (1, 0, 0), (0, 0, 0)]
        net = [0, 1, 0, 0, 1, 0]
        c = build_campaigns(_bars(states, net), EMPTY_TR, EMPTY_FL, EMPTY_FI)
        self.assertEqual(len(c), 2)
        self.assertTrue((c["close_reason"] == "flat").all())

    def test_direction_reversal_without_flat_splits_the_campaign(self):
        """Long to short with no flat bar in between must close one campaign and
        open another - otherwise a reversal is scored as one continuous bet."""
        states = [(0, 0, 0), (1, 0, 0), (1, 0, 0), (-1, 0, 0), (-1, 0, 0), (0, 0, 0)]
        net = [0, 1, 1, -1, -1, 0]
        c = build_campaigns(_bars(states, net), EMPTY_TR, EMPTY_FL, EMPTY_FI)
        self.assertEqual(len(c), 2)
        self.assertEqual(c.iloc[0]["close_reason"], "reversal")
        self.assertEqual(c.iloc[0]["direction"], "long")
        self.assertEqual(c.iloc[1]["direction"], "short")
        self.assertEqual(c.iloc[1]["close_reason"], "flat")

    def test_open_campaign_at_end_of_data_is_closed_and_labelled(self):
        states = [(0, 0, 0), (1, 0, 0), (1, 0, 0)]
        net = [0, 1, 1]
        c = build_campaigns(_bars(states, net), EMPTY_TR, EMPTY_FL, EMPTY_FI)
        self.assertEqual(len(c), 1)
        self.assertEqual(c.iloc[0]["close_reason"], "end_of_data")

    def test_mae_and_mfe_track_the_equity_path(self):
        states = [(0, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (0, 0, 0)]
        net = [0, 1, 1, 1, 0]
        eq = [100000, 100000, 99500, 100800, 100600]
        c = build_campaigns(_bars(states, net, eq), EMPTY_TR, EMPTY_FL, EMPTY_FI)
        self.assertAlmostEqual(c.iloc[0]["mae_usd"], -500.0)
        self.assertAlmostEqual(c.iloc[0]["mfe_usd"], 800.0)

    def test_campaigns_partition_the_real_run_without_overlap(self):
        res = build_and_run(RunConfig(log_open_sleeves_at_end=True), verbose=False)
        c = build_campaigns(res.bars, res.trades, res.fills, res.financing)
        self.assertGreater(len(c), 20)
        starts = pd.DatetimeIndex(c["start"]).to_numpy()
        ends = pd.DatetimeIndex(c["end"]).to_numpy()
        self.assertTrue((ends >= starts).all())
        self.assertTrue((starts[1:] > ends[:-1]).all(),
                        "campaign windows overlap")
        # every campaign has at least one active sleeve bar
        self.assertTrue((c["n_sleeves"] >= 1).all())


class TestConcentration(unittest.TestCase):
    def test_top_k_shares_and_exclusions(self):
        v = np.array([10.0, 5.0, 1.0, -2.0, -3.0])   # total 11
        d = concentration(v, "x")
        self.assertAlmostEqual(d["x_total"], 11.0)
        self.assertAlmostEqual(d["x_top1_sum"], 10.0)
        self.assertAlmostEqual(d["x_top1_share_pct"], 100 * 10.0 / 11.0)
        self.assertAlmostEqual(d["x_excl_top1_total"], 1.0)
        self.assertAlmostEqual(d["x_excl_top3_total"], -5.0)


class TestBootstrapDeterminism(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        self.v = rng.normal(0.2, 2.0, 400)
        idx = pd.date_range("2022-01-01", periods=900, freq="D")
        self.r = pd.Series(rng.normal(0.0002, 0.004, 900), index=idx)

    def test_same_seed_gives_identical_intervals(self):
        a = iid_bootstrap(self.v, 3000, 123)
        b = iid_bootstrap(self.v, 3000, 123)
        self.assertEqual(a["ci95"], b["ci95"])
        self.assertEqual(a["p_mean_le_zero"], b["p_mean_le_zero"])

    def test_different_seed_gives_a_different_draw(self):
        a = iid_bootstrap(self.v, 3000, 123)
        b = iid_bootstrap(self.v, 3000, 999)
        self.assertNotEqual(a["ci95"], b["ci95"])

    def test_block_bootstrap_is_deterministic_and_uses_block_counts(self):
        a = block_bootstrap(self.r, 2000, 7, "M")
        b = block_bootstrap(self.r, 2000, 7, "M")
        self.assertEqual(a["ci95"], b["ci95"])
        # effective observations are BLOCKS, not days
        self.assertLess(a["effective_observations"], len(self.r))
        self.assertGreater(a["effective_observations"], 10)

    def test_quarterly_blocks_are_wider_than_monthly(self):
        m = block_bootstrap(self.r, 4000, 7, "M")
        q = block_bootstrap(self.r, 4000, 7, "Q")
        self.assertLess(q["effective_observations"], m["effective_observations"])
        self.assertGreater(q["standard_error"], m["standard_error"] * 0.9)

    def test_campaign_bootstrap_is_labelled_primary(self):
        d = campaign_bootstrap(self.v, 1000, 5)
        self.assertIn("PRIMARY", d["assumptions"])
        d2 = iid_bootstrap(self.v, 1000, 5)
        self.assertIn("SECONDARY", d2["assumptions"])


class TestGoldenBaseline(unittest.TestCase):
    """The frozen v1 result must remain exactly reproducible."""

    TOL = {"net_profit": 1e-6, "sleeve_gross_sum": 1e-6, "cost_swap": 1e-6,
           "cost_commission": 1e-9, "cost_spread_slip": 1e-9}

    def test_v1_compat_reproduces_the_frozen_fixture(self):
        with open(os.path.join(ROOT, "tests", "golden",
                               "v1_baseline_stats.json"), encoding="utf-8") as fh:
            g = json.load(fh)
        res = build_and_run(RunConfig(v1_compat=True), verbose=False)
        d = res.diagnostics
        got = {"net_profit": d["net_profit"],
               "sleeve_gross_sum": float(res.trades["gross"].sum()),
               "cost_swap": d["cost_swap"],
               "cost_commission": d["cost_commission"],
               "cost_spread_slip": d["cost_spread_slip"]}
        for k, tol in self.TOL.items():
            self.assertAlmostEqual(
                got[k], g[k], delta=max(tol, abs(g[k]) * 1e-9),
                msg="%s drifted from the frozen v1 baseline" % k)
        self.assertEqual(len(res.trades), g["sleeve_trades"])

    def test_defaults_differ_from_v1_only_by_the_two_declared_defects(self):
        fixed = build_and_run(RunConfig(), verbose=False)
        compat = build_and_run(RunConfig(v1_compat=True), verbose=False)
        self.assertNotAlmostEqual(fixed.diagnostics["net_profit"],
                                  compat.diagnostics["net_profit"], places=2)
        # the fix recovers trades v1 dropped, and never loses any
        self.assertGreaterEqual(len(fixed.trades), len(compat.trades))

    def test_books_reconcile_exactly_when_open_sleeves_are_logged(self):
        res = build_and_run(RunConfig(log_open_sleeves_at_end=True), verbose=False)
        d = res.diagnostics
        implied = (float(res.trades["gross"].sum()) - d["cost_spread_slip"]
                   - d["cost_commission"] + d["cost_swap"])
        self.assertAlmostEqual(implied, d["net_profit"], places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
