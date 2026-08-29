# -*- coding: utf-8 -*-
"""ScalpGoldM1 - port of Scalp_Gold_M1_fixed.pine (TradingView Pine v5).

Bollinger mean-reversion scalper on XAUUSD M1, filtered by the H1 trend, a
trading session, and news blackout windows, sized so that a stop-out costs a
fixed amount, and managed with a profit-locking trailing stop.

WHAT THE PINE SCRIPT DOES
-------------------------
  signal   evaluated at the M1 close; the order fills at the next bar's open
  long     close crosses BELOW the lower Bollinger band  (mean reversion)
  short    close crosses ABOVE the upper band
  filters  H1 EMA(50) direction, ATR/price >= 0.015%, session 13:00-21:00 UTC,
           news blackouts 12:25-13:30 / 13:55-14:10 / 17:55-19:30 UTC
  stop     max(3 x ATR(14), 50 ticks), frozen from the SIGNAL bar but anchored
           to the ACTUAL fill price
  size     floor(5 USD / stop distance) oz, i.e. a stop-out targets -5 USD
  trail    once open profit reaches 3 USD, lock in (peak - 1 USD)
  exit     stop only - there is no take profit

MAPPING TO THIS BROKER
----------------------
  Pine `XAUUSD`, mintick 0.01, pointvalue 1 (1 USD per 1 USD move per oz)
  FxPro `GOLD`, digits 2, point 0.01, contract 100 oz, 1 lot = 100 oz.

  So Pine's quantity in ounces is this framework's lots x 100:
      Pine  qty_oz = floor(5 / sl_dist)          step 1 oz,  min 1 oz
      here  lots   = floor(5 / (sl_dist x 100) / 0.01) x 0.01
  which is the same number divided by 100.  `lot_step`/`lot_min` of 0.01
  reproduce Pine's `QtyStep`/`MinQty` of 1 oz exactly.

DELIBERATE DIFFERENCES
----------------------
  * Pine's `CooldownSec = 5` can never bind on an M1 chart - the next bar is
    60 s away - and this framework already forbids re-entry on the bar of an
    exit.  Modelled as `min_bars_between = 0`.
  * Pine allows `pyramiding = 20` but caps concurrent positions at
    `MaxPositions = 1`, so only one position is ever open.  Same here.
  * Session and blackout hours are UTC in the Pine script.  The `utc_*` fields
    below convert from broker server time using the EET/EEST rule in
    core/timeutil.py, so the windows stay correct across daylight saving.
"""
from core.indicators import ATR, EMA, SMA, StdDev, Expr, HTF
from core.spec import Costs, Exits, Sizing, Strategy, Trail

# ---------------------------------------------------------------------------
# Blackout windows, converted from HH:MM to minutes past midnight UTC.
#   12:25-13:30  ->  745-810
#   13:55-14:10  ->  835-850
#   17:55-19:30  -> 1075-1170
# ---------------------------------------------------------------------------
BLACKOUT = ("not (745 <= utc_minute_of_day < 810)"
            " and not (835 <= utc_minute_of_day < 850)"
            " and not (1075 <= utc_minute_of_day < 1170)")

SESSION = "13 <= utc_hour < 21"

# ATR must be at least 0.015% of price, and the ATR-derived stop must still
# leave room for at least one 0.01-lot (1 oz) position: floor(5/sl_dist) >= 1
# means sl_dist <= 5.0.  The framework's sizing already rejects the trade, but
# stating it here keeps the Python and MQL5 signal columns identical.
VOL_FILTER = "atr / close * 100.0 >= 0.015"

CROSS_DOWN_LOWER = "close < bb_lower and close[1] >= bb_lower[1]"
CROSS_UP_UPPER = "close > bb_upper and close[1] <= bb_upper[1]"

COMMON = "%s and %s and %s and %s" % (SESSION, BLACKOUT, VOL_FILTER, "atr > 0.0")

STRATEGY = Strategy(
    name="ScalpGoldM1",
    symbol="GOLD",
    timeframe="M1",

    indicators={
        # Bollinger(14, 1.0) on M1 - Pine's ta.stdev is a POPULATION stdev,
        # so ddof=0 here (pandas would default to 1 and drift by ~4%).
        "bb_basis": SMA("close", 14),
        "bb_dev":   StdDev("close", 14, ddof=0),
        "bb_upper": Expr("bb_basis + 1.0 * bb_dev"),
        "bb_lower": Expr("bb_basis - 1.0 * bb_dev"),

        # Pine's ta.atr uses RMA (Wilder) smoothing, not a simple mean.
        # RMA is recursive back to the first bar, which a tester starting
        # mid-history can never reproduce, so it is seeded 200 bars back:
        # (13/14)^186 ~ 1e-6, i.e. the seed is long forgotten by then.
        "atr": ATR(14, method="wilder", window=200),

        # H1 EMA(50) taken from the LAST CLOSED H1 bar, exactly what
        # request.security(..., ta.ema(close,50)[1], lookahead_on) yields.
        "h1_ema": HTF(EMA("close", 50), "H1"),
    },

    entry_long="%s and %s and close > h1_ema" % (CROSS_DOWN_LOWER, COMMON),
    entry_short="%s and %s and close < h1_ema" % (CROSS_UP_UPPER, COMMON),

    exits=Exits(
        sl_atr=3.0,             # AtrMultSL
        sl_min_points=50,       # MinSlTicks, 50 x 0.01 = 0.50
        tp_atr=None,            # the Pine script has no take profit
        atr_name="atr",
    ),

    # FixedLossUSD = 5.0 with QtyStep/MinQty of 1 oz == 0.01 lot here.
    sizing=Sizing(mode="risk", risk_money=5.0, lot_step=0.01,
                  lot_min=0.01, lot_max=5.0),

    # TrailStartUSD = 3.0, TrailStepUSD = 1.0
    trail=Trail(start_money=3.0, step_money=1.0),

    min_bars_between=0,
    lot=0.01,
    magic=20260803,

    broker_gmt_offset=2,        # FxPro runs EET/EEST
    broker_dst="eu",

    # 12 months, matching the window the TradingView run covers so the two can
    # be compared directly.  Reaching this far back needs the terminal's
    # [Charts] MaxBars above ~370k (the M1 bar count); at the default 100000
    # copy_rates_range silently returns nothing for a window this wide.
    date_from="2025-09-01",
    date_to="2026-08-29",

    deposit=10000.0,
    currency="USD",
    leverage=100,

    # Calibrated from a reconciliation run against FxPro-MT5 Demo.
    # Gold swaps are strongly asymmetric - holding long costs about 3x what
    # holding short earns - so a single blended rate would misprice the book.
    # See results/ScalpGoldM1_reconcile.txt to recalibrate.
    # Recalibrated for the 12-month window.  The 2-month calibration read
    # -77.71 on the long side; over a full year the average is a good deal
    # milder, which is exactly the drift the README warns about - a cost model
    # fitted to a short window does not survive a change of period.
    costs=Costs(
        commission_per_lot=7.85,
        swap_long_per_lot_night=-52.40,
        swap_short_per_lot_night=23.58,
    ),
)
