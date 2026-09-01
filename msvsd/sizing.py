# -*- coding: utf-8 -*-
"""Position sizing, and the minimum-lot override for small accounts.

THE PROBLEM THIS SOLVES
    Size is `risk_cash / (stop_distance x value_per_price_unit_per_lot)`, floored
    to the broker's lot step. On a $10,000 account at 0.10 % per sleeve that
    lands between 0.003 and 0.008 lots for every entry in the 2022-2026 record -
    never once reaching the 0.01 minimum. The account takes ZERO trades.

WHAT THE OVERRIDE DOES, AND WHAT IT REFUSES TO DO
    When the normal size rounds below the broker minimum, the override tests
    EXACTLY ONE minimum-size position and asks what it would actually lose at
    its stop, costs included. If that is inside both the per-sleeve and the
    portfolio cap, the trade is allowed at the minimum lot. It is never made
    larger than the minimum, and the caps are permission limits - NOT a new
    sizing target. The normal target stays 0.10 %.

    A minimum lot on a small account carries more risk than the target asks
    for; that is the whole point of gating it. On this instrument 0.01 lot is
    one ounce, so a $40 stop is $40 - 0.40 % of a $10,000 account, four times
    the nominal target.

APPLIES ONLY TO THE OVERRIDE PATH
    A normally-sized position is placed exactly as before, without consulting
    either cap. That is deliberate: it keeps the disabled-override behaviour
    bit-identical to the published baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import numpy as np

from .indicators import floor_step

# ---- stable labels. A wire format shared with the EA and the Pine: append,
#      never rename.
REASON_ACCEPT_NORMAL = "ORDER_ACCEPTED_NORMAL_SIZE"
REASON_ACCEPT_OVERRIDE = "ORDER_ACCEPTED_MINIMUM_OVERRIDE"
REASON_OVERRIDE_DISABLED = "OVERRIDE_DISABLED"
REASON_SLEEVE_RISK = "OVERRIDE_SLEEVE_RISK_EXCEEDED"
REASON_PORTFOLIO_RISK = "PORTFOLIO_OPEN_RISK_EXCEEDED"
CONDITION_BELOW_MIN = "NORMAL_SIZE_BELOW_MINIMUM"
CONDITION_NORMAL = "NORMAL_SIZE_OK"

ALL_REASONS = (REASON_ACCEPT_NORMAL, REASON_ACCEPT_OVERRIDE,
               REASON_OVERRIDE_DISABLED, REASON_SLEEVE_RISK,
               REASON_PORTFOLIO_RISK, CONDITION_BELOW_MIN)


# --------------------------------------------------------------------------
@dataclass
class CostModel:
    """Modelled transaction costs, in price and per-ounce terms.

    Mirrors what the engine actually books: bars are BID, so a long pays the
    spread on entry and a short pays it on exit. A round trip therefore costs
    the spread once and slippage twice whichever way it is facing, which is why
    the gate is direction-symmetric without special-casing either side.
    """
    spread_price: float = 0.0
    entry_slip_price: float = 0.0
    stop_slip_price: float = 0.0
    commission_per_oz_side: float = 0.0

    def entry_cost(self, oz: float, direction: int) -> float:
        spread = self.spread_price if direction > 0 else 0.0
        return (spread + self.entry_slip_price) * oz + self.commission_per_oz_side * oz

    def exit_cost(self, oz: float, direction: int) -> float:
        spread = self.spread_price if direction < 0 else 0.0
        return (spread + self.stop_slip_price) * oz + self.commission_per_oz_side * oz

    def round_trip(self, oz: float, direction: int) -> float:
        return self.entry_cost(oz, direction) + self.exit_cost(oz, direction)


def money_per_price_per_lot(contract_oz: float, tick_size: float = 0.0,
                            tick_value: float = 0.0) -> float:
    """USD gained or lost per 1.0 of price movement, per lot.

    Prefers the instrument's tick metadata (`tick_value / tick_size`), which is
    what a platform-native calculation uses and what generalises off XAUUSD.
    Falls back to the contract size when tick metadata is absent. For a 100 oz
    gold contract the two agree exactly: 1.0 / 0.01 == 100.
    """
    if tick_size and tick_value and tick_size > 0:
        return float(tick_value) / float(tick_size)
    return float(contract_oz)


# --------------------------------------------------------------------------
@dataclass
class SizingDecision:
    """One sizing verdict, with everything needed to audit it after the fact."""
    time: object = None
    sleeve: str = ""
    direction: str = ""
    equity: float = 0.0
    atr: float = 0.0
    entry_price: float = np.nan          # modelled: the fill is the next open
    stop_price: float = np.nan
    stop_distance: float = 0.0
    raw_lots: float = 0.0
    rounded_lots: float = 0.0
    final_lots: float = 0.0
    minimum_lot: float = 0.0
    lot_step: float = 0.0
    override_considered: bool = False
    override_used: bool = False
    price_stop_loss: float = 0.0
    estimated_entry_cost: float = 0.0
    estimated_exit_cost: float = 0.0
    estimated_costs: float = 0.0
    actual_stop_risk: float = 0.0
    actual_stop_risk_pct: float = 0.0
    total_open_risk_before: float = 0.0
    total_open_risk_after: float = 0.0
    total_open_risk_pct_before: float = 0.0
    total_open_risk_pct_after: float = 0.0
    condition: str = CONDITION_NORMAL
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.final_lots > 0.0

    def to_dict(self) -> Dict:
        return asdict(self)


# --------------------------------------------------------------------------
def sleeve_open_risk(direction: int, lots: float, stop_px: float, price: float,
                     mpp_per_lot: float, costs: CostModel,
                     contract_oz: float) -> float:
    """Capital still at risk in one open sleeve, from here to its stop.

    GROSS, per sleeve. Opposing sleeves are never allowed to net each other out
    - the broker position may be flat while two sleeves each still stand to
    lose at their own stop, and hiding that behind the net is exactly the
    mistake this function exists to avoid.

    A stop that has moved past breakeven contributes ZERO rather than a
    negative number, so a winning position can never finance a new one.
    """
    if direction == 0 or lots <= 0 or not np.isfinite(stop_px):
        return 0.0
    adverse = (price - stop_px) if direction > 0 else (stop_px - price)
    if adverse <= 0.0:
        return 0.0                      # stop locks in a profit: nothing at risk
    oz = lots * contract_oz
    return adverse * mpp_per_lot * lots + costs.exit_cost(oz, direction)


def total_open_risk(sleeves, price: float, mpp_per_lot: float, costs: CostModel,
                    contract_oz: float, exclude: Optional[str] = None) -> float:
    tot = 0.0
    for s in sleeves:
        if exclude is not None and s.name == exclude:
            continue
        tot += sleeve_open_risk(s.dir, s.lots, s.stop_px, price, mpp_per_lot,
                                costs, contract_oz)
    return tot


# --------------------------------------------------------------------------
def decide(sleeve_name: str, direction: int, atr: float, atr_mult: float,
           equity: float, risk_cash: float, price: float,
           contract_oz: float, lot_step: float, minimum_lot: float,
           costs: CostModel, sleeves=None,
           enable_override: bool = False,
           override_max_risk_pct: float = 0.50,
           max_total_open_risk_pct: float = 1.00,
           tick_size: float = 0.0, tick_value: float = 0.0,
           when=None) -> SizingDecision:
    """Size one prospective sleeve entry and decide whether it may be placed."""
    mpp = money_per_price_per_lot(contract_oz, tick_size, tick_value)
    stop_dist = atr * atr_mult
    d = SizingDecision(
        time=when, sleeve=sleeve_name,
        direction="long" if direction > 0 else "short",
        equity=float(equity), atr=float(atr), stop_distance=float(stop_dist),
        minimum_lot=float(minimum_lot), lot_step=float(lot_step),
        entry_price=float(price),
        stop_price=float(price - stop_dist * direction))

    risk_per_lot = stop_dist * mpp
    d.raw_lots = (risk_cash / risk_per_lot) if risk_per_lot > 0 else 0.0
    d.rounded_lots = floor_step(d.raw_lots, lot_step)

    before = (total_open_risk(sleeves, price, mpp, costs, contract_oz)
              if sleeves else 0.0)
    d.total_open_risk_before = before
    d.total_open_risk_pct_before = (100.0 * before / equity) if equity > 0 else 0.0

    def _price_risk(lots):
        oz = lots * contract_oz
        pl = stop_dist * mpp * lots
        ec = costs.entry_cost(oz, direction)
        xc = costs.exit_cost(oz, direction)
        return pl, ec, xc

    # ---- normal path: at or above the broker minimum ----------------------
    if d.rounded_lots >= minimum_lot - 1e-12 and d.rounded_lots > 0:
        d.condition = CONDITION_NORMAL
        d.final_lots = d.rounded_lots
        d.reason = REASON_ACCEPT_NORMAL
        pl, ec, xc = _price_risk(d.final_lots)
        d.price_stop_loss, d.estimated_entry_cost, d.estimated_exit_cost = pl, ec, xc
        d.estimated_costs = ec + xc
        d.actual_stop_risk = pl + ec + xc
        d.actual_stop_risk_pct = (100.0 * d.actual_stop_risk / equity) if equity > 0 else 0.0
        after = before + d.actual_stop_risk
        d.total_open_risk_after = after
        d.total_open_risk_pct_after = (100.0 * after / equity) if equity > 0 else 0.0
        return d

    # ---- below the minimum ------------------------------------------------
    d.condition = CONDITION_BELOW_MIN
    if not enable_override:
        d.reason = REASON_OVERRIDE_DISABLED
        d.total_open_risk_after = before
        d.total_open_risk_pct_after = d.total_open_risk_pct_before
        return d

    # exactly one minimum-size position is tested; never more
    d.override_considered = True
    test_lots = float(minimum_lot)
    pl, ec, xc = _price_risk(test_lots)
    d.price_stop_loss, d.estimated_entry_cost, d.estimated_exit_cost = pl, ec, xc
    d.estimated_costs = ec + xc
    d.actual_stop_risk = pl + ec + xc
    d.actual_stop_risk_pct = (100.0 * d.actual_stop_risk / equity) if equity > 0 else 0.0
    after = before + d.actual_stop_risk
    d.total_open_risk_after = after
    d.total_open_risk_pct_after = (100.0 * after / equity) if equity > 0 else 0.0

    # tolerance so a value sitting exactly on the cap is admitted rather than
    # rejected by floating-point noise
    EPS = 1e-9
    if d.actual_stop_risk_pct > override_max_risk_pct + EPS:
        d.reason = REASON_SLEEVE_RISK
        return d
    if d.total_open_risk_pct_after > max_total_open_risk_pct + EPS:
        d.reason = REASON_PORTFOLIO_RISK
        return d

    d.final_lots = test_lots
    d.override_used = True
    d.reason = REASON_ACCEPT_OVERRIDE
    return d
