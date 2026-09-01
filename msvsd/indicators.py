# -*- coding: utf-8 -*-
"""Pine-semantics indicators.

These deliberately match TradingView, not MT5. `core.indicators.ATR` uses a
fixed 200-bar seeding window because it has to agree with MQL5's iATR; the
object under test here is a Pine script, so `pine_atr` seeds the way
`ta.rma` does - a simple average of the first `length` true ranges.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(high, low, close):
    """Pine's ta.tr. Bar 0 has no previous close, so it degrades to high-low."""
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    tr[0] = high[0] - low[0]
    return tr


def pine_atr(high, low, close, length):
    """ta.atr(length) == ta.rma(ta.tr, length), SMA-seeded. NaN before that."""
    n = len(close)
    tr = true_range(high, low, close)
    out = np.full(n, np.nan)
    if n < length:
        return out
    out[length - 1] = tr[:length].mean()
    a = 1.0 / length
    for i in range(length, n):
        out[i] = a * tr[i] + (1.0 - a) * out[i - 1]
    return out


def rolling_extreme(arr, length, kind):
    """ta.highest / ta.lowest. NaN until the window is full."""
    r = pd.Series(arr).rolling(length, min_periods=length)
    return (r.max() if kind == "max" else r.min()).to_numpy()


def donchian(high, low, length, include_current=False):
    """Donchian levels that EXCLUDE the current bar: ta.highest(high[1], n).

    `include_current=True` is the audit variant only. It is the lookahead bug
    the strategy is written to avoid, kept so the guard can be shown to bite:
    with the current bar in the window, `close > highest(high, n)` can never be
    true, and the strategy takes zero trades.
    """
    if include_current:
        return (rolling_extreme(high, length, "max"),
                rolling_extreme(low, length, "min"))
    hi_prev = np.concatenate(([np.nan], high[:-1]))
    lo_prev = np.concatenate(([np.nan], low[:-1]))
    return (rolling_extreme(hi_prev, length, "max"),
            rolling_extreme(lo_prev, length, "min"))


def floor_step(x, step):
    """Round DOWN to a lot increment. Never rounds a position up into more risk."""
    if step <= 0:
        return max(0.0, float(x))
    return max(0.0, np.floor(x / step + 1e-9) * step)
