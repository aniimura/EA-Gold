# -*- coding: utf-8 -*-
"""Historical financing: per-date rates, triple nights, and missing-data policy."""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd.config import RunConfig                                  # noqa: E402
from msvsd.dataio import DataError, load_swap_table                 # noqa: E402
from msvsd.financing import (FinancingError, FinancingModel,
                             synthesize_scenario_table)             # noqa: E402
from msvsd.run import build_and_run                                 # noqa: E402
from tests import synth                                             # noqa: E402

LONG, SHORT = -52.40, 23.58


class TestRolloverCalendar(unittest.TestCase):
    def test_rollovers_are_day_boundaries_in_server_time(self):
        m = FinancingModel("flat", LONG, SHORT)
        # same day -> no rollover
        self.assertEqual(m.rollover_dates("2024-01-02 04:00", "2024-01-02 20:00"), [])
        # crossing midnight -> one
        d = m.rollover_dates("2024-01-02 20:00", "2024-01-03 00:00")
        self.assertEqual([str(x.date()) for x in d], ["2024-01-03"])
        # a weekend gap crosses three
        d = m.rollover_dates("2024-01-05 20:00", "2024-01-08 04:00")
        self.assertEqual([str(x.date()) for x in d],
                         ["2024-01-06", "2024-01-07", "2024-01-08"])


class TestFlatAndScenario(unittest.TestCase):
    def test_flat_model_triples_on_wednesday(self):
        m = FinancingModel("flat", LONG, SHORT, triple_weekday=2)
        wed = pd.Timestamp("2024-01-03")     # a Wednesday
        thu = pd.Timestamp("2024-01-04")
        self.assertAlmostEqual(m.rate_for(wed, 1)[0], LONG * 3)
        self.assertAlmostEqual(m.rate_for(thu, 1)[0], LONG)
        self.assertAlmostEqual(m.rate_for(thu, -1)[0], SHORT)

    def test_scenario_scales_without_touching_signals(self):
        lo = FinancingModel("scenario", LONG, SHORT, scenario="low")
        hi = FinancingModel("scenario", LONG, SHORT, scenario="high")
        thu = pd.Timestamp("2024-01-04")
        self.assertAlmostEqual(lo.rate_for(thu, 1)[0], LONG * 0.5)
        self.assertAlmostEqual(hi.rate_for(thu, 1)[0], LONG * 1.75)


class TestHistoricalRates(unittest.TestCase):
    def _model(self, policy="error"):
        tbl = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "long_swap_usd_per_lot": [-40.0, -120.0, -41.0],
            "short_swap_usd_per_lot": [18.0, 54.0, 19.0]})
        return FinancingModel("historical", LONG, SHORT, table=tbl,
                              missing_policy=policy)

    def test_uses_the_rate_for_that_exact_date(self):
        m = self._model()
        self.assertAlmostEqual(m.rate_for(pd.Timestamp("2024-01-02"), 1)[0], -40.0)
        self.assertAlmostEqual(m.rate_for(pd.Timestamp("2024-01-04"), -1)[0], 19.0)

    def test_triple_night_is_taken_from_the_file_not_multiplied(self):
        """2024-01-03 is a Wednesday. The file says -120; the engine must charge
        -120, not -120 x 3."""
        m = self._model()
        rate, src = m.rate_for(pd.Timestamp("2024-01-03"), 1)
        self.assertAlmostEqual(rate, -120.0)
        self.assertEqual(src, "historical")
        ev = m.charge("2024-01-02 20:00", "2024-01-03 04:00", lots=0.5, direction=1)
        self.assertEqual(len(ev), 1)
        self.assertAlmostEqual(ev[0].amount, -120.0 * 0.5)

    def test_charge_applies_per_rollover_and_scales_with_lots(self):
        m = self._model()
        ev = m.charge("2024-01-01 20:00", "2024-01-04 04:00", lots=0.25, direction=1)
        self.assertEqual(len(ev), 3)
        self.assertAlmostEqual(sum(e.amount for e in ev),
                               (-40.0 - 120.0 - 41.0) * 0.25)

    def test_missing_date_errors_by_default(self):
        m = self._model("error")
        with self.assertRaises(FinancingError) as ctx:
            m.rate_for(pd.Timestamp("2024-02-01"), 1)
        msg = str(ctx.exception)
        self.assertIn("2024-02-01", msg)
        self.assertIn("swap-missing-policy", msg)

    def test_missing_policy_zero(self):
        m = self._model("zero")
        rate, src = m.rate_for(pd.Timestamp("2024-02-01"), 1)
        self.assertEqual(rate, 0.0)
        self.assertEqual(src, "zero")

    def test_missing_policy_forward_fill_uses_the_last_known_row(self):
        m = self._model("forward-fill")
        rate, src = m.rate_for(pd.Timestamp("2024-02-01"), 1)
        self.assertAlmostEqual(rate, -41.0)
        self.assertEqual(src, "forward-fill")

    def test_forward_fill_refuses_to_extrapolate_backwards(self):
        m = self._model("forward-fill")
        with self.assertRaises(FinancingError):
            m.rate_for(pd.Timestamp("2020-01-01"), 1)

    def test_missing_policy_scenario_rate(self):
        m = self._model("scenario-rate")
        rate, src = m.rate_for(pd.Timestamp("2024-02-01"), 1)  # a Thursday
        self.assertAlmostEqual(rate, LONG)                      # base multiplier 1.0
        self.assertEqual(src, "scenario-rate")

    def test_fallback_usage_is_counted_and_reported(self):
        m = self._model("zero")
        for d in ("2024-02-01", "2024-02-02", "2024-02-01"):
            m.rate_for(pd.Timestamp(d), 1)
        rep = m.coverage_report()
        self.assertEqual(rep["rollovers_priced_by_fallback"], 3)
        self.assertEqual(rep["distinct_missing_dates"], 2)


class TestSchemaValidation(unittest.TestCase):
    def test_duplicate_dates_are_rejected(self):
        p = os.path.join(os.path.dirname(__file__), "_tmp_dup_swap.csv")
        pd.DataFrame({"date": ["2024-01-01", "2024-01-01"],
                      "long_swap_usd_per_lot": [-40, -40],
                      "short_swap_usd_per_lot": [18, 18]}).to_csv(p, index=False)
        try:
            with self.assertRaises(DataError):
                load_swap_table(p)
        finally:
            os.remove(p)

    def test_missing_columns_are_rejected(self):
        p = os.path.join(os.path.dirname(__file__), "_tmp_bad_swap.csv")
        pd.DataFrame({"date": ["2024-01-01"], "long_swap": [-40]}).to_csv(p, index=False)
        try:
            with self.assertRaises(DataError):
                load_swap_table(p)
        finally:
            os.remove(p)

    def test_sample_files_load(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("swap_rates_EXAMPLE.csv", "swap_rates_SYNTHETIC_base.csv"):
            t = load_swap_table(os.path.join(root, "schemas", f))
            self.assertGreater(len(t), 0)
            self.assertTrue(t["date"].is_monotonic_increasing)


class TestHistoricalEqualsFlatWhenTableEncodesFlat(unittest.TestCase):
    """The strongest available check on the historical path: a table that simply
    writes the flat assumption into the historical schema must reproduce the
    flat model to the cent. If it does not, the per-date lookup or the triple
    handling is wrong."""

    def test_synthetic_base_table_matches_the_flat_model(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        swap = os.path.join(root, "schemas", "swap_rates_SYNTHETIC_base.csv")
        flat = build_and_run(RunConfig(swap_model="flat"), verbose=False)
        hist = build_and_run(RunConfig(swap_model="historical", swap_file=swap),
                             verbose=False)
        self.assertAlmostEqual(flat.diagnostics["cost_swap"],
                               hist.diagnostics["cost_swap"], places=6)
        self.assertAlmostEqual(flat.diagnostics["net_profit"],
                               hist.diagnostics["net_profit"], places=6)
        self.assertEqual(hist.diagnostics["financing_coverage"]
                         ["rollovers_priced_by_fallback"], 0)

    def test_scenario_files_scale_carry_and_signals_stay_price_only(self):
        """Carry scales; the SIGNAL inputs are untouched.

        Note the one real coupling: position size is a fraction of live equity,
        and carry moves equity, so a heavier carry assumption can occasionally
        push a sleeve below one lot increment and drop the trade entirely. The
        breakout logic itself never sees the swap - the Donchian levels and ATR
        below are bit-identical - but the trade COUNT is not guaranteed to be,
        and pretending otherwise would hide a genuine feedback path.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = build_and_run(RunConfig(
            swap_model="historical",
            swap_file=os.path.join(root, "schemas", "swap_rates_SYNTHETIC_base.csv")),
            verbose=False)
        high = build_and_run(RunConfig(
            swap_model="historical",
            swap_file=os.path.join(root, "schemas", "swap_rates_SYNTHETIC_high.csv")),
            verbose=False)

        # every price-derived signal input is identical
        for col in ("atr", "ent_hi_fast", "ent_lo_fast", "exit_hi_slow",
                    "exit_lo_slow", "ent_hi_medium"):
            np.testing.assert_allclose(
                base.bars[col].to_numpy(float), high.bars[col].to_numpy(float),
                equal_nan=True, err_msg="%s changed with the carry assumption" % col)

        # Carry scales with the rate, but by slightly LESS than 1.75x: the
        # heavier charge lowers equity, which lowers position size, which lowers
        # the charge. The feedback is negative and therefore self-damping, and
        # that direction is the property worth asserting.
        ratio = high.diagnostics["cost_swap"] / base.diagnostics["cost_swap"]
        self.assertLess(ratio, 1.75)
        self.assertGreater(ratio, 1.70, "carry did not scale with the rate")
        # the feedback is a small perturbation, not a structural change
        self.assertLessEqual(abs(len(base.trades) - len(high.trades)), 3)

    def test_short_example_file_errors_out_of_coverage_by_default(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = RunConfig(swap_model="historical",
                        swap_file=os.path.join(root, "schemas",
                                               "swap_rates_EXAMPLE.csv"))
        with self.assertRaises(FinancingError):
            build_and_run(cfg, verbose=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
