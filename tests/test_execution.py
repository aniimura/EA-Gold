# -*- coding: utf-8 -*-
"""Execution: intrabar stop replay, gap fills, and filters that must never
block an exit."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd import REASON_CODES                        # noqa: E402
from msvsd.config import RunConfig                    # noqa: E402
from msvsd.engine import run_engine                   # noqa: E402
from msvsd.financing import FinancingModel            # noqa: E402
from msvsd.run import build_and_run                   # noqa: E402
from tests import synth                               # noqa: E402

NO_SWAP = lambda: FinancingModel("none", -52.40, 23.58)   # noqa: E731


def _breakout_fixture(n_warm=200, band=1.0, level=2000.0, jump=60.0, tail=12):
    """Quiet warmup, one decisive up-bar, then a flat drift. All three sleeves
    enter on the breakout bar and fill at the next bar's open."""
    path = [(level, level + jump, level - band, level + jump)]
    path += [(level + jump, level + jump + 1, level + jump - 1, level + jump)
             for _ in range(tail)]
    return synth.flat_then(path, level=level, n_warm=n_warm, band=band)


class TestIntrabarStopReplay(unittest.TestCase):
    def setUp(self):
        self.df = _breakout_fixture()
        self.entry_bar = 201          # signal on 200, fill at 201's open

    def _stop_of(self, res, bar, sleeve="fast"):
        return float(res.bars.loc[bar, "stop_%s" % sleeve])

    def test_stop_filled_at_stop_price_not_next_open(self):
        """LTF replay must exit AT the stop, with adverse slippage, and never
        wait for the next H4 open."""
        h4 = self.df.copy()
        base = run_engine(h4, RunConfig(stop_mode="h4"), NO_SWAP())
        stop = self._stop_of(base, self.entry_bar)
        self.assertTrue(np.isfinite(stop))

        # bar 203 dips through the stop intrabar and recovers to close high
        dip = stop - 3.0
        top = float(h4.loc[203, "open"])
        h4.loc[203, ["high", "low", "close"]] = [top + 1.0, dip - 0.5, top]
        paths = {203: [(top, top + 1.0, top - 0.2, top - 0.2),
                       (top - 0.2, top - 0.2, dip - 0.5, dip),
                       (dip, top, dip, top)]}
        ltf = synth.ltf_from_paths(h4, paths)

        res = run_engine(h4, RunConfig(stop_mode="ltf",
                                       ltf_stop_slippage_points=5.0),
                         NO_SWAP(), ltf=ltf)
        stops = res.trades[res.trades["reason"] == "EXIT_PROTECTIVE_STOP"]
        self.assertTrue(len(stops) > 0, "no stop exit was produced")
        px = float(stops.iloc[0]["exit_price"])
        self.assertAlmostEqual(px, stop, places=6,
                               msg="stop did not fill at the stop price")

    def test_h4_approximation_and_ltf_replay_disagree_on_recovery(self):
        """A dip-and-recover bar is exactly where the two models must differ:
        LTF exits at the stop, H4 waits for the next open which has recovered."""
        h4 = self.df.copy()
        base = run_engine(h4, RunConfig(stop_mode="h4"), NO_SWAP())
        stop = self._stop_of(base, self.entry_bar)
        dip = stop - 3.0
        top = float(h4.loc[203, "open"])
        h4.loc[203, ["high", "low", "close"]] = [top + 1.0, dip - 0.5, top]
        paths = {203: [(top, top + 1.0, dip - 0.5, top)]}
        ltf = synth.ltf_from_paths(h4, paths)

        r_h4 = run_engine(h4, RunConfig(stop_mode="h4"), NO_SWAP())
        r_lt = run_engine(h4, RunConfig(stop_mode="ltf"), NO_SWAP(), ltf=ltf)
        s_h4 = r_h4.trades[r_h4.trades["reason"] == "EXIT_PROTECTIVE_STOP"]
        s_lt = r_lt.trades[r_lt.trades["reason"].isin(
            ["EXIT_PROTECTIVE_STOP", "EXIT_STOP_GAP"])]
        self.assertTrue(len(s_h4) and len(s_lt))
        self.assertNotAlmostEqual(float(s_h4.iloc[0]["exit_price"]),
                                  float(s_lt.iloc[0]["exit_price"]), places=3)

    def test_gap_through_stop_fills_at_the_open_never_at_the_stop(self):
        h4 = self.df.copy()
        base = run_engine(h4, RunConfig(stop_mode="h4"), NO_SWAP())
        stop = self._stop_of(base, self.entry_bar)
        gap_open = stop - 25.0            # opens far through the stop
        h4.loc[203, ["open", "high", "low", "close"]] = [
            gap_open, gap_open + 1.0, gap_open - 1.0, gap_open]
        paths = {203: [(gap_open, gap_open + 1.0, gap_open - 1.0, gap_open)]}
        ltf = synth.ltf_from_paths(h4, paths)

        res = run_engine(h4, RunConfig(stop_mode="ltf"), NO_SWAP(), ltf=ltf)
        gaps = res.trades[res.trades["reason"] == "EXIT_STOP_GAP"]
        self.assertTrue(len(gaps) > 0, "gap-through-stop was not detected")
        px = float(gaps.iloc[0]["exit_price"])
        self.assertAlmostEqual(px, gap_open, places=6)
        self.assertLess(px, stop, "a gapped stop must never fill better than the stop")
        self.assertGreater(res.diagnostics["stops_ltf_gap"], 0)

    def test_no_price_improvement_on_a_protective_stop(self):
        """Whatever the path, a long's stop exit never fills above its stop."""
        h4 = self.df.copy()
        base = run_engine(h4, RunConfig(stop_mode="h4"), NO_SWAP())
        stop = self._stop_of(base, self.entry_bar)
        dip = stop - 1.0
        top = float(h4.loc[203, "open"])
        h4.loc[203, ["high", "low", "close"]] = [top + 5.0, dip, top + 4.0]
        ltf = synth.ltf_from_paths(h4, {203: [(top, top + 5.0, dip, top + 4.0)]})
        res = run_engine(h4, RunConfig(stop_mode="ltf"), NO_SWAP(), ltf=ltf)
        st = res.trades[res.trades["reason"].isin(
            ["EXIT_PROTECTIVE_STOP", "EXIT_STOP_GAP"])]
        self.assertTrue(len(st) > 0)
        self.assertLessEqual(float(st.iloc[0]["exit_price"]), stop + 1e-9)

    def test_missing_ltf_coverage_is_counted_not_hidden(self):
        h4 = self.df.copy()
        ltf = synth.constant_ltf(h4)
        ltf = ltf[ltf["time"] < h4["time"].iloc[150]]      # truncate coverage
        res = run_engine(h4, RunConfig(stop_mode="ltf"), NO_SWAP(), ltf=ltf)
        self.assertGreater(res.diagnostics["ltf_missing_h4_bars"], 0)
        cov = res.diagnostics["ltf_coverage"]
        self.assertGreater(cov["uncovered"], 0)
        self.assertLess(cov["coverage_pct"], 100.0)


class TestFiltersNeverBlockExits(unittest.TestCase):
    def test_news_blackout_blocks_entries_but_not_exits(self):
        events = pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-06-01T00:00:00"]),
            "event_name": ["BLANKET"],
            "blackout_before_minutes": [0], "blackout_after_minutes": [0]})
        events["start_utc"] = pd.Timestamp("2000-01-01")
        events["end_utc"] = pd.Timestamp("2027-01-01")

        cfg_r = RunConfig(event_mode="report-only")
        cfg_b = RunConfig(event_mode="block-new-entries")
        from msvsd.run import build_inputs
        inp = build_inputs(cfg_r, verbose=False)
        h4 = inp["h4"]
        fin = FinancingModel("none", -52.40, 23.58)

        r_open = run_engine(h4, cfg_r, fin, events=events)
        r_block = run_engine(h4, cfg_b, FinancingModel("none", -52.4, 23.58),
                             events=events)
        self.assertGreater(len(r_open.trades), 0)
        self.assertEqual(len(r_block.trades), 0,
                         "a blanket blackout should stop every new entry")
        self.assertEqual(float(np.nanmax(np.abs(r_block.bars["position_oz"]))), 0.0)

    def test_exits_still_fire_inside_a_blackout(self):
        """Open positions before the blackout, then blanket it: they must close."""
        cfg = RunConfig(event_mode="block-new-entries")
        from msvsd.run import build_inputs
        h4 = build_inputs(cfg, verbose=False)["h4"]
        cut = h4["time_utc"].iloc[3000]
        events = pd.DataFrame([{
            "timestamp": cut, "event_name": "SECOND_HALF",
            "blackout_before_minutes": 0, "blackout_after_minutes": 0,
            "start_utc": cut, "end_utc": pd.Timestamp("2030-01-01")}])
        res = run_engine(h4, cfg, FinancingModel("none", -52.4, 23.58),
                         events=events)
        exits_in_blackout = res.trades[
            pd.DatetimeIndex(res.trades["exit_time"]) >= cut]
        self.assertGreater(len(exits_in_blackout), 0,
                           "no exit executed during the blackout - exits were blocked")
        # and nothing NEW opened after the cut
        entries_after = res.trades[pd.DatetimeIndex(res.trades["entry_time"]) > cut]
        self.assertEqual(len(entries_after), 0)

    def test_friday_filter_blocks_entries_only(self):
        cfg = RunConfig()
        from msvsd.run import build_inputs
        h4 = build_inputs(cfg, verbose=False)["h4"]
        res = run_engine(h4, cfg, FinancingModel("none", -52.4, 23.58))
        b = res.bars
        blocked = b["friday_block"].to_numpy(bool)
        self.assertGreater(blocked.sum(), 0, "fixture has no blocked Friday bars")
        # no entry reason code on a blocked bar
        for nm in ("fast", "medium", "slow"):
            codes = b["reason_" + nm].to_numpy(float)[blocked]
            self.assertEqual(int(((codes == 1) | (codes == 2)).sum()), 0,
                             "%s opened on a Friday-blocked bar" % nm)
        # but exits DO occur on blocked bars
        any_exit = 0
        for nm in ("fast", "medium", "slow"):
            codes = b["reason_" + nm].to_numpy(float)[blocked]
            any_exit += int(((codes == 3) | (codes == 4)).sum())
        self.assertGreater(any_exit, 0, "the Friday filter suppressed exits too")


if __name__ == "__main__":
    unittest.main(verbosity=2)
