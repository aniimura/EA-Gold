# -*- coding: utf-8 -*-
"""Minimum-lot override: the risk gate, its caps, and its parity with MQL5.

Every case here is arithmetic that can be checked on paper. 0.01 lot of a
100 oz contract is one ounce, so a $20 stop distance is $20 of price risk -
which is why the numbers below are round.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd.config import PROFILES, RunConfig, apply_profile        # noqa: E402
from msvsd.run import build_and_run                                # noqa: E402
from msvsd.sizing import (CostModel, REASON_ACCEPT_NORMAL,
                          REASON_ACCEPT_OVERRIDE, REASON_OVERRIDE_DISABLED,
                          REASON_PORTFOLIO_RISK, REASON_SLEEVE_RISK,
                          CONDITION_BELOW_MIN, CONDITION_NORMAL,
                          decide, money_per_price_per_lot,
                          sleeve_open_risk, total_open_risk)        # noqa: E402
from msvsd.sleeves import Sleeve                                    # noqa: E402
from tests.parity_cases import PARITY_CASES, run_case               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FREE = CostModel()                       # zero-cost model, for clean arithmetic
EQ = 10000.0


def mk(dir_=1, stop_dist=20.0, equity=EQ, sleeves=None, costs=FREE,
       enable=True, sleeve_cap=0.50, total_cap=1.00, contract_oz=100.0,
       lot_step=0.01, minimum_lot=0.01, risk_pct=0.10, price=2000.0,
       tick_size=0.0, tick_value=0.0):
    """One decision with the stop distance stated directly, via ATR x 2.5."""
    return decide(sleeve_name="fast", direction=dir_, atr=stop_dist / 2.5,
                  atr_mult=2.5, equity=equity, risk_cash=equity * risk_pct / 100.0,
                  price=price, contract_oz=contract_oz, lot_step=lot_step,
                  minimum_lot=minimum_lot, costs=costs, sleeves=sleeves,
                  enable_override=enable, override_max_risk_pct=sleeve_cap,
                  max_total_open_risk_pct=total_cap,
                  tick_size=tick_size, tick_value=tick_value)


def open_sleeve(name, dir_, lots, stop_px):
    s = Sleeve(name=name, entry_len=20, exit_len=10)
    s.dir, s.lots, s.stop_px, s.entry_px = dir_, lots, stop_px, 2000.0
    return s


# ==========================================================================
class TestOverrideGate(unittest.TestCase):
    """Cases 1-4: the two caps, at and around their boundaries."""

    def test_1_twenty_dollar_stop_is_accepted(self):
        # 0.01 lot = 1 oz; a $20 stop distance is $20 = 0.20 % of $10,000
        d = mk(stop_dist=20.0)
        self.assertEqual(d.condition, CONDITION_BELOW_MIN)
        self.assertTrue(d.override_considered)
        self.assertTrue(d.override_used)
        self.assertEqual(d.reason, REASON_ACCEPT_OVERRIDE)
        self.assertAlmostEqual(d.final_lots, 0.01)
        self.assertAlmostEqual(d.price_stop_loss, 20.0)
        self.assertAlmostEqual(d.actual_stop_risk_pct, 0.20)

    def test_2_exactly_on_the_sleeve_cap_is_accepted(self):
        """$50 on $10,000 is exactly 0.50 %. A value sitting on the cap must be
        admitted, not rejected by floating-point noise."""
        d = mk(stop_dist=50.0)
        self.assertAlmostEqual(d.actual_stop_risk, 50.0)
        self.assertAlmostEqual(d.actual_stop_risk_pct, 0.50)
        self.assertEqual(d.reason, REASON_ACCEPT_OVERRIDE)
        self.assertAlmostEqual(d.final_lots, 0.01)

    def test_2b_on_the_cap_including_costs(self):
        """Same boundary, but reached through costs rather than price alone."""
        costs = CostModel(spread_price=0.0, entry_slip_price=1.0,
                          stop_slip_price=1.0, commission_per_oz_side=0.0)
        d = mk(stop_dist=48.0, costs=costs)     # 48 price + 1 + 1 on one ounce
        self.assertAlmostEqual(d.price_stop_loss, 48.0)
        self.assertAlmostEqual(d.estimated_costs, 2.0)
        self.assertAlmostEqual(d.actual_stop_risk, 50.0)
        self.assertEqual(d.reason, REASON_ACCEPT_OVERRIDE)

    def test_3_above_the_sleeve_cap_is_rejected(self):
        d = mk(stop_dist=51.0)
        self.assertFalse(d.override_used)
        self.assertEqual(d.final_lots, 0.0)
        self.assertEqual(d.reason, REASON_SLEEVE_RISK)
        self.assertGreater(d.actual_stop_risk_pct, 0.50)

    def test_4_portfolio_cap_rejects_what_the_sleeve_cap_allows(self):
        # existing sleeve: long 0.01 lot, price 2000, stop 1930 -> $70 at risk
        existing = [open_sleeve("slow", 1, 0.01, 1930.0)]
        d = mk(stop_dist=40.0, sleeves=existing)
        self.assertAlmostEqual(d.total_open_risk_before, 70.0)
        self.assertAlmostEqual(d.actual_stop_risk, 40.0)
        self.assertAlmostEqual(d.actual_stop_risk_pct, 0.40)   # inside 0.50 %
        self.assertAlmostEqual(d.total_open_risk_pct_after, 1.10)
        self.assertEqual(d.reason, REASON_PORTFOLIO_RISK)      # outside 1.00 %
        self.assertEqual(d.final_lots, 0.0)

    def test_4b_just_inside_the_portfolio_cap_is_accepted(self):
        existing = [open_sleeve("slow", 1, 0.01, 1940.0)]      # $60 at risk
        d = mk(stop_dist=40.0, sleeves=existing)
        self.assertAlmostEqual(d.total_open_risk_pct_after, 1.00)
        self.assertEqual(d.reason, REASON_ACCEPT_OVERRIDE)


class TestNormalPathUntouched(unittest.TestCase):
    """Case 5: a normally-sized position is never altered by the override."""

    def test_5_normal_size_is_used_and_not_enlarged(self):
        # $100k, 0.10 % = $100 risk, stop distance 32.5 -> 100/3250 = 0.0308
        d = mk(equity=100000.0, stop_dist=32.5)
        self.assertEqual(d.condition, CONDITION_NORMAL)
        self.assertFalse(d.override_considered)
        self.assertFalse(d.override_used)
        self.assertEqual(d.reason, REASON_ACCEPT_NORMAL)
        self.assertAlmostEqual(d.rounded_lots, 0.03)
        self.assertAlmostEqual(d.final_lots, 0.03)

    def test_5b_override_never_shrinks_a_normal_size_to_the_minimum(self):
        d = mk(equity=100000.0, stop_dist=32.5, enable=True)
        self.assertAlmostEqual(d.final_lots, 0.03)
        self.assertGreater(d.final_lots, d.minimum_lot)

    def test_5c_normal_path_ignores_the_caps_entirely(self):
        """A normal-sized position far above the override cap is still placed -
        the caps are permissions for the override, not a new risk limit."""
        d = mk(equity=100000.0, stop_dist=32.5, sleeve_cap=0.001, total_cap=0.002)
        self.assertEqual(d.reason, REASON_ACCEPT_NORMAL)
        self.assertAlmostEqual(d.final_lots, 0.03)

    def test_disabled_override_skips_exactly_as_before(self):
        d = mk(stop_dist=20.0, enable=False)
        self.assertEqual(d.condition, CONDITION_BELOW_MIN)
        self.assertFalse(d.override_considered)
        self.assertEqual(d.final_lots, 0.0)
        self.assertEqual(d.reason, REASON_OVERRIDE_DISABLED)


class TestInstrumentMetadata(unittest.TestCase):
    """Case 7: non-default contract size, minimum lot and lot step."""

    def test_7_micro_contract_needs_no_override_at_all(self):
        """A 10 oz contract makes $10,000 behave like $100,000 does on a 100 oz
        one: 0.10 % of $10,000 over a $20 stop buys 0.05 lots, which already
        clears the minimum. The override is never consulted."""
        d = mk(stop_dist=20.0, contract_oz=10.0)
        self.assertEqual(d.condition, CONDITION_NORMAL)
        self.assertEqual(d.reason, REASON_ACCEPT_NORMAL)
        self.assertFalse(d.override_considered)
        self.assertAlmostEqual(d.final_lots, 0.05)
        self.assertAlmostEqual(d.price_stop_loss, 10.0)        # 20 x 10 x 0.05
        self.assertAlmostEqual(d.actual_stop_risk_pct, 0.10)   # exactly on target

    def test_7a_micro_contract_still_gates_when_it_does_round_down(self):
        """Shrink the account until even the micro contract rounds below the
        minimum, and the override path takes over as designed."""
        d = mk(stop_dist=20.0, contract_oz=10.0, equity=1000.0)
        self.assertEqual(d.condition, CONDITION_BELOW_MIN)
        self.assertAlmostEqual(d.price_stop_loss, 2.0)         # 0.01 lot = 0.1 oz
        self.assertAlmostEqual(d.actual_stop_risk_pct, 0.20)
        self.assertEqual(d.reason, REASON_ACCEPT_OVERRIDE)

    def test_7b_coarse_minimum_and_step(self):
        d = mk(stop_dist=20.0, minimum_lot=0.1, lot_step=0.1, equity=EQ)
        self.assertAlmostEqual(d.rounded_lots, 0.0)
        self.assertAlmostEqual(d.price_stop_loss, 200.0)       # 0.1 lot = 10 oz
        self.assertEqual(d.reason, REASON_SLEEVE_RISK)         # 2 % >> 0.50 %

    def test_7c_tick_metadata_matches_contract_size(self):
        """tick_value / tick_size is the platform-native form of contract size;
        for a 100 oz gold contract the two must agree exactly."""
        self.assertAlmostEqual(money_per_price_per_lot(100.0, 0.01, 1.0), 100.0)
        self.assertAlmostEqual(money_per_price_per_lot(100.0), 100.0)
        a = mk(stop_dist=20.0)
        b = mk(stop_dist=20.0, contract_oz=0.0, tick_size=0.01, tick_value=1.0)
        self.assertAlmostEqual(a.price_stop_loss, b.price_stop_loss)

    def test_7d_finer_step_reaches_the_normal_path(self):
        d = mk(stop_dist=20.0, lot_step=0.001, minimum_lot=0.001)
        self.assertEqual(d.condition, CONDITION_NORMAL)
        self.assertEqual(d.reason, REASON_ACCEPT_NORMAL)
        self.assertAlmostEqual(d.final_lots, 0.005)            # 10/2000 = 0.005


class TestDirectionSymmetry(unittest.TestCase):
    """Case 8: long and short must be priced identically."""

    def test_8_long_and_short_agree_without_costs(self):
        a, b = mk(dir_=1, stop_dist=30.0), mk(dir_=-1, stop_dist=30.0)
        self.assertAlmostEqual(a.price_stop_loss, b.price_stop_loss)
        self.assertAlmostEqual(a.actual_stop_risk, b.actual_stop_risk)
        self.assertEqual(a.reason, b.reason)

    def test_8b_stop_price_is_on_the_correct_side(self):
        a = mk(dir_=1, stop_dist=30.0, price=2000.0)
        b = mk(dir_=-1, stop_dist=30.0, price=2000.0)
        self.assertAlmostEqual(a.stop_price, 1970.0)
        self.assertAlmostEqual(b.stop_price, 2030.0)

    def test_8c_round_trip_cost_is_direction_symmetric(self):
        """Bars are bid: a long pays the spread entering, a short paying it on
        exit. The round trip is the same either way, so the gate cannot favour
        one direction."""
        costs = CostModel(spread_price=0.3, entry_slip_price=0.05,
                          stop_slip_price=0.05, commission_per_oz_side=0.04)
        a, b = mk(dir_=1, stop_dist=30.0, costs=costs), mk(dir_=-1, stop_dist=30.0, costs=costs)
        self.assertAlmostEqual(a.estimated_costs, b.estimated_costs)
        self.assertAlmostEqual(a.actual_stop_risk, b.actual_stop_risk)


class TestCostsInTheGate(unittest.TestCase):
    """Case 9: costs must be able to flip the verdict."""

    def test_9_costs_push_a_trade_over_the_cap(self):
        free = mk(stop_dist=49.0)
        self.assertEqual(free.reason, REASON_ACCEPT_OVERRIDE)
        costs = CostModel(spread_price=0.5, entry_slip_price=0.5,
                          stop_slip_price=0.5, commission_per_oz_side=0.25)
        paid = mk(stop_dist=49.0, costs=costs)
        self.assertGreater(paid.actual_stop_risk, free.actual_stop_risk)
        self.assertEqual(paid.reason, REASON_SLEEVE_RISK)

    def test_9b_cost_components_are_all_present(self):
        costs = CostModel(spread_price=0.3, entry_slip_price=0.05,
                          stop_slip_price=0.07, commission_per_oz_side=0.04)
        d = mk(dir_=1, stop_dist=20.0, costs=costs)
        # long: entry pays spread+slip+comm, exit pays stop_slip+comm, on 1 oz
        self.assertAlmostEqual(d.estimated_entry_cost, 0.3 + 0.05 + 0.04)
        self.assertAlmostEqual(d.estimated_exit_cost, 0.07 + 0.04)
        self.assertAlmostEqual(d.estimated_costs,
                               d.estimated_entry_cost + d.estimated_exit_cost)
        self.assertAlmostEqual(d.actual_stop_risk,
                               d.price_stop_loss + d.estimated_costs)


class TestOpenRiskAggregation(unittest.TestCase):
    """Cases 10-11: gross, never netted, never negative."""

    def test_10_profitable_stop_contributes_zero_not_a_negative(self):
        # long entered at 2000, stop moved to 2050 (above price 2020): locked in
        winner = open_sleeve("slow", 1, 0.01, 2050.0)
        r = sleeve_open_risk(winner.dir, winner.lots, winner.stop_px, 2020.0,
                             100.0, FREE, 100.0)
        self.assertEqual(r, 0.0)
        self.assertGreaterEqual(r, 0.0)

    def test_10b_a_winner_cannot_finance_a_new_trade(self):
        winner = open_sleeve("slow", 1, 0.01, 2050.0)
        loser = open_sleeve("medium", 1, 0.01, 1930.0)         # $70 at risk
        tot = total_open_risk([winner, loser], 2020.0, 100.0, FREE, 100.0)
        self.assertAlmostEqual(tot, (2020.0 - 1930.0) * 1.0)
        self.assertGreater(tot, 0.0)

    def test_11_opposing_sleeves_do_not_net_away(self):
        """A long and a short of equal size leave the BROKER flat, but both can
        still lose at their own stop. Gross risk must show both."""
        lng = open_sleeve("fast", 1, 0.01, 1980.0)             # $20 at risk
        sht = open_sleeve("medium", -1, 0.01, 2030.0)          # $30 at risk
        tot = total_open_risk([lng, sht], 2000.0, 100.0, FREE, 100.0)
        self.assertAlmostEqual(tot, 50.0)
        net_lots = lng.dir * lng.lots + sht.dir * sht.lots
        self.assertAlmostEqual(net_lots, 0.0, msg="fixture should net to flat")

    def test_11b_gate_sees_the_gross_figure(self):
        lng = open_sleeve("medium", 1, 0.01, 1955.0)           # $45
        sht = open_sleeve("slow", -1, 0.01, 2045.0)            # $45
        d = mk(stop_dist=20.0, sleeves=[lng, sht], price=2000.0)
        self.assertAlmostEqual(d.total_open_risk_before, 90.0)
        self.assertAlmostEqual(d.total_open_risk_pct_after, 1.10)
        self.assertEqual(d.reason, REASON_PORTFOLIO_RISK)

    def test_excludes_flat_and_stopless_sleeves(self):
        flat = Sleeve(name="fast", entry_len=20, exit_len=10)
        self.assertEqual(sleeve_open_risk(flat.dir, flat.lots, flat.stop_px,
                                          2000.0, 100.0, FREE, 100.0), 0.0)


class TestBackwardCompatibility(unittest.TestCase):
    """Case 6: with the override off nothing at all may change."""

    @classmethod
    def setUpClass(cls):
        cls.off = build_and_run(RunConfig(), verbose=False)

    def test_6_disabled_reproduces_the_published_baseline(self):
        with open(os.path.join(ROOT, "results", "v2",
                               "baseline_summary.json"), encoding="utf-8") as fh:
            published = json.load(fh)["statistics"]["risk"]
        self.assertAlmostEqual(self.off.diagnostics["net_profit"],
                               published["net_profit"], places=6)

    def test_6b_disabled_reproduces_the_frozen_v1_fixture(self):
        with open(os.path.join(ROOT, "tests", "golden",
                               "v1_baseline_stats.json"), encoding="utf-8") as fh:
            g = json.load(fh)
        r = build_and_run(RunConfig(v1_compat=True), verbose=False)
        self.assertAlmostEqual(r.diagnostics["net_profit"], g["net_profit"], places=6)
        self.assertEqual(len(r.trades), g["sleeve_trades"])

    def test_6c_enabling_the_override_changes_nothing_on_a_large_account(self):
        """At $100,000 almost every size clears the minimum on its own, so the
        override may only add trades that were previously skipped - it must
        never alter one that was already being taken."""
        on = build_and_run(RunConfig(enable_min_lot_override=True), verbose=False)
        a = self.off.trades.set_index(["sleeve", "entry_time"])["lots"]
        b = on.trades.set_index(["sleeve", "entry_time"])["lots"]
        common = a.index.intersection(b.index)
        self.assertGreater(len(common), 250)
        np.testing.assert_allclose(a.loc[common].to_numpy(float),
                                   b.loc[common].to_numpy(float))

    def test_6d_override_only_ever_adds_minimum_size_entries(self):
        on = build_and_run(RunConfig(capital=10000.0,
                                     enable_min_lot_override=True), verbose=False)
        used = on.sizing[on.sizing["override_used"]]
        self.assertGreater(len(used), 0)
        self.assertTrue((used["final_lots"] == on.config.minimum_lot).all(),
                        "override produced a size other than the minimum lot")


class TestProfiles(unittest.TestCase):
    def test_profiles_exist_and_validate(self):
        for name in ("baseline_strict", "small_account_override",
                     "small_account_override_stress"):
            cfg = apply_profile(RunConfig(), name)
            cfg.validate()
            self.assertIn("PROFILE__%s" % name.upper(), cfg.labels())

    def test_baseline_strict_leaves_the_override_off(self):
        cfg = apply_profile(RunConfig(), "baseline_strict")
        self.assertFalse(cfg.enable_min_lot_override)
        self.assertAlmostEqual(cfg.effective_target_risk_pct(), 0.10)

    def test_small_account_profile_settings(self):
        cfg = apply_profile(RunConfig(), "small_account_override")
        self.assertTrue(cfg.enable_min_lot_override)
        self.assertAlmostEqual(cfg.capital, 10000.0)
        self.assertAlmostEqual(cfg.effective_target_risk_pct(), 0.10)
        self.assertAlmostEqual(cfg.override_max_risk_pct_per_sleeve, 0.50)
        self.assertAlmostEqual(cfg.max_total_open_risk_pct, 1.00)

    def test_stress_profile_is_labelled_diagnostic(self):
        cfg = apply_profile(RunConfig(), "small_account_override_stress")
        self.assertIn("DIAGNOSTIC_ONLY__NOT_RECOMMENDED", cfg.labels())
        self.assertAlmostEqual(cfg.override_max_risk_pct_per_sleeve, 1.00)
        self.assertAlmostEqual(cfg.max_total_open_risk_pct, 2.00)

    def test_the_target_is_never_the_cap(self):
        for name in PROFILES:
            cfg = apply_profile(RunConfig(), name)
            self.assertAlmostEqual(cfg.effective_target_risk_pct(), 0.10,
                                   msg="%s moved the sizing target" % name)

    def test_rejects_a_portfolio_cap_below_the_sleeve_cap(self):
        with self.assertRaises(ValueError):
            RunConfig(override_max_risk_pct_per_sleeve=1.0,
                      max_total_open_risk_pct=0.5).validate()


class TestMql5Parity(unittest.TestCase):
    """Case 12: identical inputs must produce identical decisions in both.

    The EA computes the same table in `InpSelfTestSizing` mode and writes it to
    Common\\Files. If that file is absent the comparison is SKIPPED with the
    command that produces it - a silent pass here would be worthless.
    """
    EXPORT = os.path.join(ROOT, "results", "v2", "mql5_sizing_selftest.csv")

    def test_12_python_and_mql5_agree(self):
        if not os.path.isfile(self.EXPORT):
            self.skipTest(
                "no MQL5 self-test export at %s - produce it with:\n"
                "    python run_mt5_msvsd.py --sizing-selftest" % self.EXPORT)
        mq = pd.read_csv(self.EXPORT)
        self.assertEqual(len(mq), len(PARITY_CASES),
                         "EA case table is out of step with tests/parity_cases.py")
        bad = []
        for i, case in enumerate(PARITY_CASES):
            py = run_case(case)
            row = mq.iloc[i]
            if row["reason"] != py.reason:
                bad.append("%s: reason py=%s mq5=%s"
                           % (case["id"], py.reason, row["reason"]))
            if abs(float(row["final_lots"]) - py.final_lots) > 1e-9:
                bad.append("%s: lots py=%.6f mq5=%.6f"
                           % (case["id"], py.final_lots, row["final_lots"]))
            if abs(float(row["actual_stop_risk"]) - py.actual_stop_risk) > 1e-6:
                bad.append("%s: risk py=%.6f mq5=%.6f"
                           % (case["id"], py.actual_stop_risk, row["actual_stop_risk"]))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_python_side_of_the_parity_table_is_deterministic(self):
        """Runs regardless of MT5, so the shared table cannot rot unnoticed."""
        a = [run_case(c) for c in PARITY_CASES]
        b = [run_case(c) for c in PARITY_CASES]
        self.assertEqual([x.reason for x in a], [x.reason for x in b])
        self.assertEqual([round(x.final_lots, 9) for x in a],
                         [round(x.final_lots, 9) for x in b])
        # the table must exercise every outcome, or parity proves little
        seen = {x.reason for x in a}
        for r in (REASON_ACCEPT_NORMAL, REASON_ACCEPT_OVERRIDE,
                  REASON_SLEEVE_RISK, REASON_PORTFOLIO_RISK,
                  REASON_OVERRIDE_DISABLED):
            self.assertIn(r, seen, "parity table never produces %s" % r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
