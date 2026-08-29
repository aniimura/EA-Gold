# -*- coding: utf-8 -*-
"""VolDip - port of DevEA/VolDip/VolDip.mq5 onto the framework.

Long when Bollinger width is extreme AND the 50-SMA is falling steeply.
Thresholds are the originals; note that 0.0158 sits near the 99.7th percentile
of H1 GBPUSD BB width, so this strategy trades rarely by design.
"""
from core.indicators import ATR, SMA, StdDev, Expr
from core.spec import Costs, Exits, Strategy

STRATEGY = Strategy(
    name="VolDip",
    symbol="GBPUSD",
    timeframe="H1",

    indicators={
        "atr":      ATR(14, method="sma"),
        "sma50":    SMA("close", 50),
        "std50":    StdDev("close", 50, ddof=1),
        # Bollinger width = (upper - lower) / middle = 4*sigma / mean
        "bb_width": Expr("4.0 * std50 / sma50"),
        # slope of the 50-SMA over 5 bars, normalised by ATR
        "slope":    Expr("(sma50 - sma50[5]) / atr"),
    },

    entry_long="bb_width > 0.0158 and slope < -0.79",

    exits=Exits(
        sl_atr=3.0,
        tp_atr=5.0,
        atr_name="atr",
        max_hold_bars=48,
    ),

    # Calibrated from a reconciliation run (see results/VolDip_reconcile.txt).
    costs=Costs(
        commission_per_lot=8.00,
        swap_long_per_lot_night=-1.91,
        swap_short_per_lot_night=-1.91,
    ),

    min_bars_between=24,
    lot=0.01,
    magic=20260801,

    date_from="2024-06-01",
    date_to="2026-03-01",

    comment="original thresholds from DevEA reverse_search / phase1",
)
