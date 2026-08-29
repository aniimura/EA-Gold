# -*- coding: utf-8 -*-
"""RsiRevert - an intentionally ACTIVE demo strategy.

Its job is to exercise the framework, not to make money: it fires often
enough that the Python/MT5 reconciliation has plenty of trades to compare,
and it touches every indicator family (Wilder RSI, SMA-ATR, EMA, Highest)
so a translation bug in any of them shows up.
"""
from core.indicators import ATR, EMA, RSI, Highest, Expr
from core.spec import Costs, Exits, Strategy

STRATEGY = Strategy(
    name="RsiRevert",
    symbol="GBPUSD",
    timeframe="H1",

    indicators={
        "atr":     ATR(14, method="sma"),
        "rsi":     RSI("close", 14, method="wilder"),
        "ema100":  EMA("close", 100),
        "hi20":    Highest("high", 20),
        # how far below the slow EMA price has stretched, in ATR units
        "stretch": Expr("(ema100 - close) / atr"),
    },

    # oversold, below the slow trend, and stretched at least half an ATR
    entry_long="rsi < 30.0 and close < ema100 and stretch > 0.5",

    # leave early if RSI has recovered
    exits=Exits(
        sl_atr=2.0,
        tp_atr=3.0,
        atr_name="atr",
        max_hold_bars=72,
        exit_long="rsi > 60.0",
    ),

    # Calibrated from a reconciliation run against FxPro-MT5 Demo (GBPUSD).
    # Without these the Python P/L is +10.42 while MT5 books -1.41 - the costs
    # are larger than the edge, so the sign of the result flips.
    costs=Costs(
        commission_per_lot=7.93,
        swap_long_per_lot_night=-2.55,
        swap_short_per_lot_night=-2.55,
    ),

    min_bars_between=12,
    lot=0.01,
    magic=20260802,

    date_from="2024-06-01",
    date_to="2026-03-01",
)
