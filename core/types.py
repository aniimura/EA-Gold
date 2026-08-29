# -*- coding: utf-8 -*-
"""Shared data types for the FxTrade_202608 reproduction framework."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Timeframes
# --------------------------------------------------------------------------
# name -> (MetaTrader5 python constant name, MQL5 enum, seconds per bar)
TIMEFRAMES: Dict[str, Any] = {
    "M1":  ("TIMEFRAME_M1",  "PERIOD_M1",      60),
    "M5":  ("TIMEFRAME_M5",  "PERIOD_M5",     300),
    "M15": ("TIMEFRAME_M15", "PERIOD_M15",    900),
    "M30": ("TIMEFRAME_M30", "PERIOD_M30",   1800),
    "H1":  ("TIMEFRAME_H1",  "PERIOD_H1",    3600),
    "H4":  ("TIMEFRAME_H4",  "PERIOD_H4",   14400),
    "D1":  ("TIMEFRAME_D1",  "PERIOD_D1",   86400),
}


def tf_seconds(tf: str) -> int:
    return TIMEFRAMES[tf][2]


# --------------------------------------------------------------------------
# Rounding
# --------------------------------------------------------------------------
def mt5_round(value: float, digits: int) -> float:
    """Replicate MQL5 NormalizeDouble().

    Two traps, both of which show up as 1-point SL/TP differences that look
    like logic bugs:

      * Python's built-in round() is round-half-to-even; NormalizeDouble
        rounds half away from zero.
      * Rounding the *decimal* form (via Decimal(repr(x))) is not the same as
        rounding the *binary* double.  repr() snaps to the shortest string
        that round-trips, which can flip a value sitting just below a .5
        boundary onto the other side.

    So do exactly what NormalizeDouble does: scale, add half, truncate toward
    zero, unscale - in double arithmetic, in that order.
    """
    if value is None or not np.isfinite(value):
        return value
    scale = 10.0 ** digits
    scaled = float(value) * scale
    return (math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)) / scale


# --------------------------------------------------------------------------
# Symbol / execution parameters
# --------------------------------------------------------------------------
@dataclass
class SymbolInfo:
    """Broker-side constants that affect fills.  Fetched from MT5 or overridden."""
    name: str
    digits: int = 5
    point: float = 0.00001
    trade_stops_level: int = 0      # STOPLEVEL in points
    trade_freeze_level: int = 0
    contract_size: float = 100000.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    tick_size: float = 0.00001
    tick_value: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


EXIT_SL = "SL"
EXIT_TP = "TP"
EXIT_TIME = "TIME"
EXIT_SIGNAL = "SIGNAL"
EXIT_EOD = "EOD"          # forced close at end of backtest


@dataclass
class Trade:
    """One round-trip position, produced by both the Python engine and MT5."""
    idx: int = 0
    direction: str = "long"           # 'long' | 'short'

    entry_bar: int = -1
    entry_time: Any = None            # pandas.Timestamp (server time)
    entry_price: float = float("nan")
    sl: float = float("nan")
    tp: float = float("nan")
    entry_atr: float = float("nan")
    entry_spread_points: float = float("nan")
    lots: float = 0.0                 # 0 = fall back to the strategy's fixed lot

    exit_bar: int = -1
    exit_time: Any = None
    exit_price: float = float("nan")
    exit_reason: str = ""

    bars_held: int = 0
    ambiguous: bool = False           # SL and TP both reachable inside one bar
    trailed: bool = False             # the stop was moved by the trailing rule
    note: str = ""

    # booked costs (filled in by the engine from Strategy.costs)
    swap: float = 0.0                 # account currency, signed
    commission: float = 0.0           # account currency, positive = charged
    nights: int = 0                   # weighted overnight rollovers

    # ---------------------------------------------------------------- metrics
    @property
    def points(self) -> float:
        """Signed price move in price units (not broker points)."""
        if not np.isfinite(self.exit_price) or not np.isfinite(self.entry_price):
            return float("nan")
        d = self.exit_price - self.entry_price
        return d if self.direction == "long" else -d

    def pnl_atr(self) -> float:
        if not np.isfinite(self.entry_atr) or self.entry_atr <= 0:
            return float("nan")
        return self.points / self.entry_atr

    def size(self, lot: float) -> float:
        return self.lots if self.lots else lot

    def pnl_money(self, contract_size: float, lot: float) -> float:
        """Gross P/L from price movement only (no swap, no commission)."""
        return self.points * contract_size * self.size(lot)

    def net_money(self, contract_size: float, lot: float) -> float:
        """What the account actually sees: gross + swap - commission."""
        return self.pnl_money(contract_size, lot) + self.swap - self.commission

    def to_row(self, contract_size: float = 100000.0, lot: float = 0.01) -> Dict[str, Any]:
        return {
            "idx": self.idx,
            "direction": self.direction,
            "entry_bar": self.entry_bar,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "entry_atr": self.entry_atr,
            "entry_spread_points": self.entry_spread_points,
            "lots": self.size(lot),
            "exit_bar": self.exit_bar,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "ambiguous": self.ambiguous,
            "trailed": self.trailed,
            "points": self.points,
            "pnl_atr": self.pnl_atr(),
            "pnl_money": self.pnl_money(contract_size, lot),
            "swap": self.swap,
            "commission": self.commission,
            "nights": self.nights,
            "net_money": self.net_money(contract_size, lot),
            "note": self.note,
        }


def trades_to_frame(trades: List[Trade], contract_size: float = 100000.0,
                    lot: float = 0.01) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=list(Trade().to_row().keys()))
    return pd.DataFrame([t.to_row(contract_size, lot) for t in trades])


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    trades: List[Trade] = field(default_factory=list)
    bars: Optional[pd.DataFrame] = None       # per-bar diagnostics (optional)
    stats: Dict[str, Any] = field(default_factory=dict)

    def frame(self, contract_size: float = 100000.0, lot: float = 0.01) -> pd.DataFrame:
        return trades_to_frame(self.trades, contract_size, lot)
