# -*- coding: utf-8 -*-
"""Overnight financing.

v1 applied one current FxPro rate (-52.40 long / +23.58 short per lot per
night) across 4.7 years. That is the single largest modelling assumption in
the whole backtest - carry was 38 % of gross P&L - and rates moved a long way
with policy rates over that period. This module replaces the assumption with
a file, and refuses by default to guess at any date the file does not cover.

THE FILE FORMAT ENCODES TRIPLE NIGHTS
    date,long_swap_usd_per_lot,short_swap_usd_per_lot
    Each row is the actual cash charged or credited per lot at THAT rollover.
    A Wednesday row therefore already carries the triple charge; nothing in
    this module multiplies by three when historical rates are in use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import SWAP_SCENARIO_MULT


class FinancingError(RuntimeError):
    """Raised when a rollover date has no rate and the policy is `error`."""


@dataclass
class ChargeEvent:
    date: pd.Timestamp
    direction: int          # +1 long, -1 short
    lots: float
    rate_per_lot: float     # cash per lot for this rollover, sign included
    amount: float           # rate_per_lot * lots  (signed; negative = cost)
    source: str             # historical | flat | scenario | zero | forward-fill | none


class FinancingModel:
    """Rollover calendar plus a rate lookup, with an explicit missing-data policy."""

    def __init__(self, model: str, long_flat: float, short_flat: float,
                 triple_weekday: int = 2, table: Optional[pd.DataFrame] = None,
                 missing_policy: str = "error", scenario: str = "base"):
        self.model = model
        self.long_flat = float(long_flat)
        self.short_flat = float(short_flat)
        self.triple_weekday = int(triple_weekday)
        self.missing_policy = missing_policy
        self.scenario = scenario
        self.scenario_mult = SWAP_SCENARIO_MULT[scenario]
        self._map: Dict[pd.Timestamp, Tuple[float, float]] = {}
        self._dates: np.ndarray = np.array([], dtype="datetime64[ns]")
        if table is not None and len(table):
            self._map = {r.date: (r.long_swap_usd_per_lot, r.short_swap_usd_per_lot)
                         for r in table.itertuples()}
            self._dates = table["date"].to_numpy("datetime64[ns]")
        # diagnostics
        self.missing_dates: List[pd.Timestamp] = []
        self.filled_dates: List[pd.Timestamp] = []
        self.used_historical = 0
        self.used_fallback = 0

    # ------------------------------------------------------------------
    def rollover_dates(self, t0, t1) -> List[pd.Timestamp]:
        """Broker rollover boundaries strictly after t0 and at or before t1.

        Rollover is 00:00 SERVER time, which is why the engine hands this
        method server timestamps rather than UTC ones. This reproduces
        `core.backtest.count_rollovers` exactly, but returns the dates so a
        per-date rate can be applied to each.
        """
        a = pd.Timestamp(t0).normalize()
        b = pd.Timestamp(t1).normalize()
        out = []
        while a < b:
            a = a + pd.Timedelta(days=1)
            out.append(a)
        return out

    def _flat_rate(self, date: pd.Timestamp, direction: int, mult: float) -> float:
        base = self.long_flat if direction > 0 else self.short_flat
        nights = 3.0 if date.dayofweek == self.triple_weekday else 1.0
        return base * nights * mult

    def rate_for(self, date: pd.Timestamp, direction: int,
                 count: bool = True) -> Tuple[float, str]:
        """Cash per lot for one rollover. Returns (rate, source).

        `count=False` suppresses the diagnostic counters, for the virtual
        sleeve attribution pass which re-prices rollovers that the actual
        position has already been charged for."""
        if self.model == "none":
            return 0.0, "none"
        if self.model == "flat":
            return self._flat_rate(date, direction, 1.0), "flat"
        if self.model == "scenario":
            return self._flat_rate(date, direction, self.scenario_mult), "scenario"

        # historical
        key = pd.Timestamp(date).normalize()
        hit = self._map.get(key)
        if hit is not None:
            if count:
                self.used_historical += 1
            return (hit[0] if direction > 0 else hit[1]), "historical"

        if count:
            self.missing_dates.append(key)
        pol = self.missing_policy
        if pol == "error":
            raise FinancingError(
                "no historical swap rate for %s (position was %s).\n"
                "The file covers %s .. %s. Extend it, restrict --date-from/--date-to, "
                "or choose an explicit --swap-missing-policy "
                "(zero | forward-fill | scenario-rate). Every one of those "
                "alternatives labels the result as non-historical."
                % (key.date(), "long" if direction > 0 else "short",
                   self._dates[0] if len(self._dates) else "n/a",
                   self._dates[-1] if len(self._dates) else "n/a"))
        if count:
            self.used_fallback += 1
            self.filled_dates.append(key)
        if pol == "zero":
            return 0.0, "zero"
        if pol == "scenario-rate":
            return self._flat_rate(key, direction, self.scenario_mult), "scenario-rate"
        if pol == "forward-fill":
            if not len(self._dates):
                raise FinancingError("forward-fill requested but the swap table is empty")
            idx = np.searchsorted(self._dates, np.datetime64(key), side="right") - 1
            if idx < 0:
                raise FinancingError(
                    "cannot forward-fill %s: it precedes the first row in the swap "
                    "file (%s). Forward-fill never invents a rate before the data "
                    "starts." % (key.date(), pd.Timestamp(self._dates[0]).date()))
            prev = self._map[pd.Timestamp(self._dates[idx])]
            return (prev[0] if direction > 0 else prev[1]), "forward-fill"
        raise FinancingError("unknown missing policy %r" % pol)

    # ------------------------------------------------------------------
    def charge(self, t0, t1, lots: float, direction: int) -> List[ChargeEvent]:
        """Financing events for holding `lots` in `direction` from t0 to t1."""
        if lots == 0.0 or direction == 0 or self.model == "none":
            return []
        out = []
        for d in self.rollover_dates(t0, t1):
            rate, src = self.rate_for(d, direction)
            if rate == 0.0 and src in ("none",):
                continue
            out.append(ChargeEvent(date=d, direction=direction, lots=abs(lots),
                                   rate_per_lot=rate, amount=rate * abs(lots),
                                   source=src))
        return out

    # ------------------------------------------------------------------
    def coverage_report(self) -> Dict:
        return {
            "model": self.model,
            "scenario": self.scenario if self.model == "scenario" else None,
            "missing_policy": self.missing_policy if self.model == "historical" else None,
            "rows_in_table": len(self._map),
            "table_first": str(pd.Timestamp(self._dates[0]).date()) if len(self._dates) else None,
            "table_last": str(pd.Timestamp(self._dates[-1]).date()) if len(self._dates) else None,
            "rollovers_priced_historically": self.used_historical,
            "rollovers_priced_by_fallback": self.used_fallback,
            "distinct_missing_dates": len(set(self.missing_dates)),
            "missing_sample": [str(d.date()) for d in sorted(set(self.missing_dates))[:10]],
        }


# --------------------------------------------------------------------------
def attribute(events: List[ChargeEvent]) -> pd.DataFrame:
    """Flatten charge events for grouping by year / direction."""
    if not events:
        return pd.DataFrame(columns=["date", "year", "direction", "lots",
                                     "rate_per_lot", "amount", "source", "lot_nights"])
    df = pd.DataFrame([e.__dict__ for e in events])
    df["year"] = pd.DatetimeIndex(df["date"]).year
    df["lot_nights"] = df["lots"]
    return df


def synthesize_scenario_table(dates: pd.DatetimeIndex, long_flat: float,
                              short_flat: float, mult: float,
                              triple_weekday: int = 2) -> pd.DataFrame:
    """Build a swap CSV in the documented schema from a flat rate assumption.

    Used to generate the sample file and the low/base/high scenario files. The
    output is explicitly synthetic - it is an assumption written down in the
    historical format, not observed data, and every run that consumes it is
    labelled SCENARIO_CARRY.
    """
    rows = []
    for d in dates:
        n = 3.0 if d.dayofweek == triple_weekday else 1.0
        rows.append({"date": d.strftime("%Y-%m-%d"),
                     "long_swap_usd_per_lot": round(long_flat * n * mult, 4),
                     "short_swap_usd_per_lot": round(short_flat * n * mult, 4)})
    return pd.DataFrame(rows)
