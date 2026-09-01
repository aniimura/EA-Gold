# -*- coding: utf-8 -*-
"""Core mechanics: channels, execution timing, sizing, exposure, independence."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd.config import RunConfig                              # noqa: E402
from msvsd.engine import net_target, run_engine                 # noqa: E402
from msvsd.financing import FinancingModel                      # noqa: E402
from msvsd.indicators import donchian, floor_step, pine_atr     # noqa: E402
from msvsd.sleeves import (EV_EXIT_STOP, Sleeve, phase_exit_entry,
                           phase_fill, phase_stop)              # noqa: E402
from tests import synth                                         # noqa: E402


def no_swap(cfg=None):
    return FinancingModel("none", -52.40, 23.58)


def flat_swap():
    return FinancingModel("flat", -52.40, 23.58)


class TestChannelsExcludeCurrentBar(unittest.TestCase):
    def test_donchian_excludes_current_bar(self):
        high = np.array([10.0, 11.0, 12.0, 50.0, 13.0])
        low = np.array([9.0, 8.0, 7.0, 1.0, 6.0])
        hi, lo = donchian(high, low, 3)
        # bar 3's own 50/1 must NOT appear in bar 3's levels
        self.assertEqual(hi[3], 12.0)
        self.assertEqual(lo[3], 7.0)
        # it appears from bar 4 onward
        self.assertEqual(hi[4], 50.0)
        self.assertEqual(lo[4], 1.0)

    def test_first_full_window_only(self):
        high = np.arange(1.0, 8.0)
        low = high - 1
        hi, _ = donchian(high, low, 3)
        # needs 3 prior bars, so index 0..2 are NaN
        self.assertTrue(np.all(np.isnan(hi[:3])))
        self.assertFalse(np.isnan(hi[3]))

    def test_lookahead_variant_is_degenerate(self):
        """With the current bar included, close > highest(high, n) can never fire."""
        df = synth.breakout_up()
        cfg = RunConfig(lookahead_audit=True, sleeve_mode="all")
        res = run_engine(df, cfg, no_swap())
        self.assertEqual(len(res.trades), 0)
        self.assertEqual(float(np.nanmax(np.abs(res.bars["position_oz"]))), 0.0)


class TestNextBarOpenExecution(unittest.TestCase):
    def test_fill_price_is_next_bar_open(self):
        df = synth.breakout_up()
        cfg = RunConfig(sleeve_mode="all")
        res = run_engine(df, cfg, no_swap())
        self.assertTrue(len(res.trades) > 0 or res.bars["position_oz"].abs().max() > 0)
        fills = res.fills[res.fills["kind"] == "open_order"]
        self.assertTrue(len(fills) > 0)
        first = fills.iloc[0]
        bar = df[df["time"] == first["time"]].iloc[0]
        # reference price for an open_order fill is that bar's OPEN
        self.assertAlmostEqual(float(first["ref_price"]), float(bar["open"]), places=9)

    def test_signal_bar_close_never_executes(self):
        """The bar that produces the signal must not also transact."""
        df = synth.breakout_up()
        res = run_engine(df, RunConfig(), no_swap())
        b = res.bars
        first_target = b.index[(b["net_target_lots"].abs() > 0)][0]
        # position is still zero on the signal bar; it changes on the NEXT bar
        self.assertEqual(float(b.loc[first_target, "position_oz"]), 0.0)
        self.assertGreater(abs(float(b.loc[first_target + 1, "position_oz"])), 0.0)


class TestAtrFreezeAndStop(unittest.TestCase):
    def test_atr_frozen_at_entry_and_stop_derived_from_it(self):
        s = Sleeve("fast", 20, 10)
        s.dir, s.lots, s.atr_ent, s.pending = 1, 0.05, 4.0, 1
        phase_fill(s, 100.0, pd.Timestamp("2024-01-01"), 5, 2.5)
        self.assertAlmostEqual(s.entry_px, 100.0)
        self.assertAlmostEqual(s.stop_px, 100.0 - 2.5 * 4.0)
        self.assertAlmostEqual(s.atr_ent, 4.0)

    def test_stop_never_widens_even_as_atr_explodes(self):
        s = Sleeve("fast", 20, 10)
        s.dir, s.lots, s.atr_ent, s.pending = 1, 0.05, 4.0, 1
        phase_fill(s, 100.0, pd.Timestamp("2024-01-01"), 5, 2.5)
        original = s.stop_px
        for atr_now in (8.0, 20.0, 100.0):
            phase_exit_entry(s, 105.0, 200.0, 50.0, 200.0, 50.0, atr_now,
                             100.0, 100.0, 0.01, 2.5, False, True,
                             "symmetric", False, 0)
            self.assertAlmostEqual(s.stop_px, original,
                                   msg="stop moved when ATR changed")

    def test_stop_triggers_on_completed_bar_range(self):
        s = Sleeve("fast", 20, 10)
        s.dir, s.stop_px, s.lots = 1, 90.0, 0.05
        self.assertEqual(phase_stop(s, low=90.5, high=110.0), 0)
        self.assertEqual(phase_stop(s, low=89.9, high=110.0), EV_EXIT_STOP)
        s.dir, s.stop_px = -1, 110.0
        self.assertEqual(phase_stop(s, low=90.0, high=109.5), 0)
        self.assertEqual(phase_stop(s, low=90.0, high=110.1), EV_EXIT_STOP)


class TestSizingAndRounding(unittest.TestCase):
    def test_lot_rounding_always_down(self):
        self.assertAlmostEqual(floor_step(0.0199, 0.01), 0.01)
        self.assertAlmostEqual(floor_step(0.0299999, 0.01), 0.02)
        self.assertAlmostEqual(floor_step(0.009, 0.01), 0.0)
        self.assertAlmostEqual(floor_step(0.0199, 0.001), 0.019)
        # a value already on the grid must not fall a step through float noise
        self.assertAlmostEqual(floor_step(0.03, 0.01), 0.03)
        self.assertAlmostEqual(floor_step(0.07, 0.01), 0.07)

    def test_size_is_risk_over_stop_distance(self):
        s = Sleeve("fast", 20, 10)
        # risk 100 USD, ATR 4.0, mult 2.5 -> stop distance 10 -> 1000 USD/lot
        # -> 0.1 lots
        phase_exit_entry(s, close=150.0, ent_hi=100.0, ent_lo=50.0,
                         ex_hi=100.0, ex_lo=50.0, atr_now=4.0, risk_cash=100.0,
                         contract_oz=100.0, lot_step=0.01, atr_mult=2.5,
                         entries_blocked=False, allow_rev=True,
                         direction_mode="symmetric", slow_short_confirmed=False,
                         prior_exit_ev=0)
        self.assertEqual(s.dir, 1)
        self.assertAlmostEqual(s.lots, 0.10)

    def test_unsizable_entry_is_refused_not_rounded_up(self):
        s = Sleeve("fast", 20, 10)
        phase_exit_entry(s, close=150.0, ent_hi=100.0, ent_lo=50.0,
                         ex_hi=100.0, ex_lo=50.0, atr_now=400.0, risk_cash=1.0,
                         contract_oz=100.0, lot_step=0.01, atr_mult=2.5,
                         entries_blocked=False, allow_rev=True,
                         direction_mode="symmetric", slow_short_confirmed=False,
                         prior_exit_ev=0)
        self.assertEqual(s.dir, 0)
        self.assertEqual(s.lots, 0.0)

    def test_exit_is_not_swallowed_by_an_unsizable_reversal(self):
        """DEFECT-V1-EXIT-SWALLOW regression guard."""
        s = Sleeve("fast", 20, 10)
        s.dir, s.lots, s.entry_px, s.stop_px, s.atr_ent = 1, 0.05, 100.0, 90.0, 4.0
        # close breaks the exit channel AND the opposite entry channel, but ATR
        # is huge so the reversal cannot be sized
        xev, nev = phase_exit_entry(
            s, close=40.0, ent_hi=200.0, ent_lo=50.0, ex_hi=200.0, ex_lo=60.0,
            atr_now=400.0, risk_cash=1.0, contract_oz=100.0, lot_step=0.01,
            atr_mult=2.5, entries_blocked=False, allow_rev=True,
            direction_mode="symmetric", slow_short_confirmed=False, prior_exit_ev=0)
        self.assertNotEqual(xev, 0)
        self.assertEqual(nev, 0)
        self.assertEqual(s.dir, 0, "sleeve kept a position it was told to close")

    def test_v1_compat_reproduces_the_defect(self):
        s = Sleeve("fast", 20, 10)
        s.dir, s.lots, s.entry_px, s.stop_px, s.atr_ent = 1, 0.05, 100.0, 90.0, 4.0
        phase_exit_entry(
            s, close=40.0, ent_hi=200.0, ent_lo=50.0, ex_hi=200.0, ex_lo=60.0,
            atr_now=400.0, risk_cash=1.0, contract_oz=100.0, lot_step=0.01,
            atr_mult=2.5, entries_blocked=False, allow_rev=True,
            direction_mode="symmetric", slow_short_confirmed=False,
            prior_exit_ev=0, v1_compat=True)
        self.assertEqual(s.dir, 1, "v1_compat should reproduce the swallowed exit")


class TestNotionalCap(unittest.TestCase):
    def _sleeves(self, lots):
        out = []
        for nm, l in zip(("fast", "medium", "slow"), lots):
            s = Sleeve(nm, 20, 10)
            s.dir, s.lots = (1 if l else 0), abs(l)
            out.append(s)
        return out

    def test_cap_limits_exposure(self):
        cfg = RunConfig(max_notional_x=1.5, lot_step=0.01, contract_oz=100.0)
        # equity 10_000, price 2000 -> cap = 1.5*10000/(100*2000) = 0.075 lots
        net, raw, cap, capped = net_target(self._sleeves([0.05, 0.05, 0.05]),
                                           10000.0, 2000.0, cfg)
        self.assertAlmostEqual(raw, 0.15)
        self.assertAlmostEqual(cap, 0.075)
        self.assertAlmostEqual(net, 0.07)      # floored to the lot step
        self.assertTrue(capped)

    def test_cap_inactive_when_exposure_is_small(self):
        cfg = RunConfig(max_notional_x=1.5)
        net, raw, cap, capped = net_target(self._sleeves([0.01, 0.02, 0.02]),
                                           100000.0, 2000.0, cfg)
        self.assertAlmostEqual(net, 0.05)
        self.assertFalse(capped)

    def test_cap_preserves_sign(self):
        cfg = RunConfig(max_notional_x=1.5)
        sl = self._sleeves([0.05, 0.05, 0.05])
        for s in sl:
            s.dir = -1
        net, raw, cap, capped = net_target(sl, 10000.0, 2000.0, cfg)
        self.assertLess(net, 0)
        self.assertAlmostEqual(abs(net), 0.07)


class TestSleeveIndependence(unittest.TestCase):
    def test_closing_one_sleeve_leaves_the_others_alone(self):
        a = Sleeve("fast", 20, 10)
        b = Sleeve("medium", 55, 20)
        for s, lots in ((a, 0.01), (b, 0.02)):
            s.dir, s.lots, s.entry_px, s.stop_px, s.atr_ent = 1, lots, 100.0, 90.0, 4.0
        # a is stopped, b is not
        self.assertEqual(phase_stop(a, low=89.0, high=101.0), EV_EXIT_STOP)
        a.reset()
        self.assertEqual(a.dir, 0)
        self.assertEqual(b.dir, 1)
        self.assertAlmostEqual(b.lots, 0.02)
        self.assertAlmostEqual(b.stop_px, 90.0)

    def test_each_sleeve_keeps_its_own_windows_and_stop(self):
        """A rising warmup, so the 20-bar and 120-bar highs genuinely differ.

        (On a flat warmup every window returns the same level, which would make
        this assertion vacuous rather than passing for the right reason.)
        """
        # an oscillation with a period longer than the fast window: the 120-bar
        # high sees a peak the 20-bar high has already rolled past
        import math
        rows = []
        for i in range(260):
            p = 2000.0 + 50.0 * math.sin(i / 19.0)
            rows.append((p, p + 1.0, p - 1.0, p))
        df = synth.make_h4(rows)
        res = run_engine(df, RunConfig(), no_swap())
        b = res.bars.dropna(subset=["ent_hi_fast", "ent_hi_slow"])
        self.assertTrue(len(b) > 0)
        self.assertFalse(np.allclose(b["ent_hi_fast"].to_numpy(float),
                                     b["ent_hi_slow"].to_numpy(float)),
                         "fast and slow sleeves resolved identical entry channels")
        # and their exit channels differ too (10 vs 40 bars)
        self.assertFalse(np.allclose(b["exit_lo_fast"].to_numpy(float),
                                     b["exit_lo_slow"].to_numpy(float)))
        # sanity: the wider window is never tighter than the narrow one
        self.assertTrue((b["ent_hi_slow"].to_numpy(float)
                         >= b["ent_hi_fast"].to_numpy(float) - 1e-9).all())


class TestNetPositionReconciliation(unittest.TestCase):
    def test_virtual_book_reconciles_with_the_account(self):
        """sum(sleeve gross) - costs must equal the account's net profit."""
        from msvsd.run import build_and_run
        cfg = RunConfig(log_open_sleeves_at_end=True)
        res = build_and_run(cfg, verbose=False)
        d = res.diagnostics
        gross = float(res.trades["gross"].sum())
        implied = (gross - d["cost_spread_slip"] - d["cost_commission"]
                   + d["cost_swap"])
        self.assertAlmostEqual(implied, d["net_profit"], places=6,
                               msg="virtual sleeves and the netted account disagree")

    def test_position_never_exceeds_the_net_target(self):
        from msvsd.run import build_and_run
        res = build_and_run(RunConfig(), verbose=False)
        b = res.bars.dropna(subset=["net_target_lots"])
        pos = b["position_oz"].to_numpy(float) / 100.0
        tgt = b["net_target_lots"].to_numpy(float)
        # the position tracks the PREVIOUS bar's target (next-bar execution)
        self.assertLessEqual(float(np.abs(pos[1:] - tgt[:-1]).max()), 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
