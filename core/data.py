# -*- coding: utf-8 -*-
"""Bar data and symbol properties, sourced from the running MT5 terminal.

Both sides of the comparison must see the SAME bars.  Pulling them from the
same terminal that will later run the Strategy Tester removes timezone and
broker-history mismatches: the returned ``time`` column is already server
time, which is exactly what the EA sees.
"""
from __future__ import annotations

import datetime as dt
import os
import pickle

import numpy as np
import pandas as pd

from .types import SymbolInfo, TIMEFRAMES, tf_seconds

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_MT5_TERMINAL = r"C:\Program Files\FxPro - MetaTrader 5\terminal64.exe"


class DataError(RuntimeError):
    pass


# --------------------------------------------------------------------------
def _mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise DataError("the MetaTrader5 python package is not installed") from exc
    return mt5


def _initialize(mt5, terminal_path=None):
    ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not ok:
        raise DataError("mt5.initialize() failed: %s" % (mt5.last_error(),))
    return True


# --------------------------------------------------------------------------
def get_symbol_info(symbol, terminal_path=None):
    """Read the broker constants that affect fills."""
    mt5 = _mt5()
    _initialize(mt5, terminal_path or _MT5_TERMINAL)
    try:
        if not mt5.symbol_select(symbol, True):
            raise DataError("symbol_select(%s) failed: %s" % (symbol, mt5.last_error()))
        si = mt5.symbol_info(symbol)
        if si is None:
            raise DataError("symbol_info(%s) returned None" % symbol)
        return SymbolInfo(
            name=si.name,
            digits=int(si.digits),
            point=float(si.point),
            trade_stops_level=int(si.trade_stops_level),
            trade_freeze_level=int(si.trade_freeze_level),
            contract_size=float(si.trade_contract_size),
            volume_min=float(si.volume_min),
            volume_step=float(si.volume_step),
            tick_size=float(si.trade_tick_size),
            tick_value=float(si.trade_tick_value),
        )
    finally:
        mt5.shutdown()


def fetch_rates(symbol, timeframe, date_from, date_to, warmup_bars=0,
                terminal_path=None):
    """Download bars covering ``date_from``..``date_to`` plus warmup history."""
    mt5 = _mt5()
    if timeframe not in TIMEFRAMES:
        raise DataError("unknown timeframe %r" % timeframe)
    tf_const = getattr(mt5, TIMEFRAMES[timeframe][0])

    d_from = pd.Timestamp(date_from).to_pydatetime()
    d_to = pd.Timestamp(date_to).to_pydatetime()
    # Pad generously: weekends and holidays mean calendar time > bar count.
    pad_s = int(warmup_bars * tf_seconds(timeframe) * 2.5) + 7 * 86400
    d_start = d_from - dt.timedelta(seconds=pad_s)
    # Pad the END too.  copy_rates_range() interprets its bounds against the
    # terminal's own clock and quietly stops short of the requested instant -
    # asking for "up to 2026-08-01" returned bars only to 2026-07-31 15:00,
    # which silently truncated the last day of the backtest while the MT5
    # tester happily used it.  Over-request, then clip by the window mask.
    d_end = d_to + dt.timedelta(days=3)

    _initialize(mt5, terminal_path or _MT5_TERMINAL)
    try:
        if not mt5.symbol_select(symbol, True):
            raise DataError("symbol_select(%s) failed: %s" % (symbol, mt5.last_error()))
        rates = mt5.copy_rates_range(symbol, tf_const, d_start, d_end)
        if rates is None or len(rates) == 0:
            raise DataError("no rates for %s %s in %s..%s (%s)"
                            % (symbol, timeframe, d_start, d_end, mt5.last_error()))
    finally:
        mt5.shutdown()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    if "spread" not in df.columns:
        df["spread"] = np.nan
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    df.attrs["symbol"] = symbol
    df.attrs["timeframe"] = timeframe
    df.attrs["test_start"] = pd.Timestamp(d_from)
    df.attrs["test_end"] = pd.Timestamp(d_to)
    return df


# --------------------------------------------------------------------------
def _cache_path(symbol, timeframe, date_from, date_to, warmup_bars):
    key = "%s_%s_%s_%s_w%d.pkl" % (
        symbol.replace(".", "_").replace("#", "_"), timeframe,
        pd.Timestamp(date_from).strftime("%Y%m%d"),
        pd.Timestamp(date_to).strftime("%Y%m%d"), warmup_bars)
    return os.path.join(DATA_DIR, key)


def load_rates(symbol, timeframe, date_from, date_to, warmup_bars=0,
               refresh=False, terminal_path=None, verbose=True):
    """``fetch_rates`` with an on-disk cache."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _cache_path(symbol, timeframe, date_from, date_to, warmup_bars)
    if os.path.exists(path) and not refresh:
        with open(path, "rb") as fh:
            df = pickle.load(fh)
        if verbose:
            print("  data   : cache %s (%d bars)" % (os.path.basename(path), len(df)))
        return df
    df = fetch_rates(symbol, timeframe, date_from, date_to, warmup_bars, terminal_path)
    with open(path, "wb") as fh:
        pickle.dump(df, fh)
    if verbose:
        print("  data   : fetched %d bars %s .. %s"
              % (len(df), df["time"].iloc[0], df["time"].iloc[-1]))
    return df


def median_spread_points(df):
    """Median historical spread in broker points, or NaN when unavailable."""
    if "spread" not in df.columns:
        return float("nan")
    s = pd.to_numeric(df["spread"], errors="coerce").dropna()
    s = s[s > 0]
    return float(s.median()) if len(s) else float("nan")


def test_window_mask(df, date_from, date_to):
    """Boolean mask selecting the bars inside the backtest window."""
    t = df["time"]
    return (t >= pd.Timestamp(date_from)) & (t < pd.Timestamp(date_to))
