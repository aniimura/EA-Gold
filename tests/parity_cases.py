# -*- coding: utf-8 -*-
"""The shared Python/MQL5 sizing-parity table.

This table is duplicated verbatim in build/XauMsvsd.mq5 (`SizingSelfTest`).
Both sides compute a decision for every row; `tests/test_min_lot_override.py`
compares them. Keep the two in step - the test asserts the row COUNT matches,
so adding a case here without adding it there fails loudly rather than silently
comparing fewer cases.

Every row states the stop DISTANCE directly; ATR is derived as distance / 2.5,
so the numbers stay readable and platform-independent.
"""
from __future__ import annotations

from typing import Dict, List

from msvsd.sizing import CostModel, decide

# open sleeves are described as (direction, lots, stop_price); the current
# price is always 2000.0 so the arithmetic is checkable by eye
PARITY_CASES: List[Dict] = [
    dict(id="normal_size_large_account", equity=100000.0, stop_dist=32.5,
         direction=1, enable=True, open=[]),
    dict(id="normal_size_override_off", equity=100000.0, stop_dist=32.5,
         direction=1, enable=False, open=[]),
    dict(id="below_min_override_off", equity=10000.0, stop_dist=20.0,
         direction=1, enable=False, open=[]),
    dict(id="override_accept_20usd", equity=10000.0, stop_dist=20.0,
         direction=1, enable=True, open=[]),
    dict(id="override_accept_on_cap_50usd", equity=10000.0, stop_dist=50.0,
         direction=1, enable=True, open=[]),
    dict(id="override_reject_sleeve_51usd", equity=10000.0, stop_dist=51.0,
         direction=1, enable=True, open=[]),
    dict(id="override_short_symmetric", equity=10000.0, stop_dist=20.0,
         direction=-1, enable=True, open=[]),
    dict(id="override_reject_portfolio", equity=10000.0, stop_dist=40.0,
         direction=1, enable=True, open=[(1, 0.01, 1930.0)]),
    dict(id="override_accept_on_portfolio_cap", equity=10000.0, stop_dist=40.0,
         direction=1, enable=True, open=[(1, 0.01, 1940.0)]),
    dict(id="opposing_sleeves_gross", equity=10000.0, stop_dist=20.0,
         direction=1, enable=True, open=[(1, 0.01, 1955.0), (-1, 0.01, 2045.0)]),
    dict(id="winning_stop_contributes_zero", equity=10000.0, stop_dist=20.0,
         direction=1, enable=True, open=[(1, 0.01, 2050.0)]),
    dict(id="with_costs", equity=10000.0, stop_dist=49.0, direction=1,
         enable=True, open=[],
         costs=dict(spread=0.5, entry_slip=0.5, stop_slip=0.5, comm_oz=0.25)),
    dict(id="micro_contract", equity=10000.0, stop_dist=20.0, direction=1,
         enable=True, open=[], contract_oz=10.0),
    dict(id="coarse_minimum_lot", equity=10000.0, stop_dist=20.0, direction=1,
         enable=True, open=[], minimum_lot=0.1, lot_step=0.1),
]

PRICE = 2000.0
SLEEVE_CAP = 0.50
TOTAL_CAP = 1.00


class _Open(object):
    """Minimal stand-in for a Sleeve - sizing only reads these four fields."""
    __slots__ = ("name", "dir", "lots", "stop_px")

    def __init__(self, name, direction, lots, stop_px):
        self.name, self.dir, self.lots, self.stop_px = name, direction, lots, stop_px


def run_case(case: Dict):
    c = case.get("costs") or {}
    costs = CostModel(spread_price=c.get("spread", 0.0),
                      entry_slip_price=c.get("entry_slip", 0.0),
                      stop_slip_price=c.get("stop_slip", 0.0),
                      commission_per_oz_side=c.get("comm_oz", 0.0))
    contract_oz = case.get("contract_oz", 100.0)
    sleeves = [_Open("open%d" % i, d, l, s)
               for i, (d, l, s) in enumerate(case.get("open", []))]
    return decide(
        sleeve_name="fast", direction=case["direction"],
        atr=case["stop_dist"] / 2.5, atr_mult=2.5,
        equity=case["equity"],
        risk_cash=case["equity"] * 0.10 / 100.0,
        price=PRICE, contract_oz=contract_oz,
        lot_step=case.get("lot_step", 0.01),
        minimum_lot=case.get("minimum_lot", 0.01),
        costs=costs, sleeves=sleeves,
        enable_override=case["enable"],
        override_max_risk_pct=SLEEVE_CAP,
        max_total_open_risk_pct=TOTAL_CAP)
