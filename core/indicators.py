# -*- coding: utf-8 -*-
"""Indicator library with paired NumPy / MQL5 implementations.

Every indicator here is defined ONCE and emits both

  * ``compute(name, env)``  -> a NumPy array aligned to the bar index, and
  * ``mq5_body(name)``      -> ``double Ind_<name>(int s)`` in MQL5,

computed with exactly the same formula from raw OHLC.

Why not iATR/iMA/iStdDev?
    Because MT5's built-ins do not agree with the obvious pandas equivalents.
    ``iATR`` uses Wilder smoothing while ``rolling().mean()`` is a simple
    average - in this project that single mismatch produced a 64.9% ATR
    difference and invalidated a whole backtest.  ``iStdDev`` is a population
    standard deviation while ``pandas.rolling().std()`` defaults to the sample
    one.  Re-deriving both sides from OHLC removes the entire class of bug.

Why the hand-rolled sums instead of pandas rolling()?
    Because floating-point addition is not associative.  pandas keeps a
    running sum (add the new bar, subtract the old one) while the MQL5loop
    re-adds the whole window each time, so the two disagree in the last bit.
    That is normally irrelevant - until a stop level lands exactly on a
    rounding boundary such as 1.342405, where one ulp flips NormalizeDouble
    and the stop moves a whole point.  ``_seq_sum`` therefore accumulates in
    the SAME ORDER as the emitted MQL5 loop, newest bar first.

Indexing rule (see core/expr.py): shift 0 == last COMPLETED bar,
MQL5 rates index == shift + 1.
"""
from __future__ import annotations

import contextlib

import numpy as np
import pandas as pd

from .expr import PRICE_FIELDS, TIME_FIELDS, compile_indicator_expr

__all__ = [
    "Indicator", "SMA", "EMA", "StdDev", "ATR", "RSI", "Highest", "Lowest",
    "Sum", "Expr", "HTF", "INDICATOR_TYPES",
]


# --------------------------------------------------------------------------
# order-preserving primitives
# --------------------------------------------------------------------------
def _shift(arr, n):
    """out[t] = arr[t - n]; leading positions filled with NaN."""
    arr = np.asarray(arr, dtype=float)
    if n == 0:
        return arr
    out = np.full(len(arr), np.nan, dtype=float)
    if n < len(arr):
        out[n:] = arr[:-n]
    return out


def _seq_sum(x, period, oldest_first=False):
    """sum over a rolling window, added in the same order as the MQL5 loop.

    ``oldest_first=False`` matches ``for(j=0;j<n;j++) sum += src(s+j)``.
    """
    x = np.asarray(x, dtype=float)
    acc = np.zeros(len(x), dtype=float)
    order = range(period - 1, -1, -1) if oldest_first else range(period)
    for j in order:
        acc = acc + _shift(x, j)
    return acc


def _seq_sum_offset(x, period, offset, oldest_first=True):
    """Window of ``period`` bars ending ``offset`` bars back.

    Matches ``for(j=0;j<period;j++) sum += src(s + offset + period - 1 - j)``,
    which is how the EMA/Wilder seed is accumulated (oldest bar first).
    """
    x = np.asarray(x, dtype=float)
    acc = np.zeros(len(x), dtype=float)
    shifts = [offset + period - 1 - j for j in range(period)]
    if not oldest_first:
        shifts.reverse()
    for sh in shifts:
        acc = acc + _shift(x, sh)
    return acc


def _seq_recursion(x, alpha, steps, seed):
    """``for(k=steps-1;k>=0;k--) e = a*x[k] + (1-a)*e`` - vectorised over bars."""
    e = np.array(seed, dtype=float, copy=True)
    for k in range(steps - 1, -1, -1):
        e = alpha * _shift(x, k) + (1.0 - alpha) * e
    return e


# Which rates array the emitted MQL5 reads, and the suffix its helper
# functions carry.  A higher-timeframe indicator re-emits the SAME body
# against g_rates_<TF>, so one definition serves both timeframes and they
# cannot drift apart.
_CTX = {"rates": "g_rates", "suffix": ""}


@contextlib.contextmanager
def emit_context(rates="g_rates", suffix=""):
    old = dict(_CTX)
    _CTX.update(rates=rates, suffix=suffix)
    try:
        yield
    finally:
        _CTX.update(old)


def mq5_source(source, idx_expr):
    """MQL5 accessor for an indicator input at our-shift ``idx_expr``."""
    if source in PRICE_FIELDS:
        return "%s[(%s) + 1].%s" % (_CTX["rates"], idx_expr, PRICE_FIELDS[source])
    if source in TIME_FIELDS:
        return "%s(%s[(%s) + 1].time)" % (
            TIME_FIELDS[source], _CTX["rates"], idx_expr)
    return "Ind_%s%s(%s)" % (source, _CTX["suffix"], idx_expr)


def _resolve(env, source):
    if source not in env:
        raise KeyError("unknown indicator input: %r" % (source,))
    return np.asarray(env[source], dtype=float)


def _blank_before(arr, n):
    """Mask the first ``n`` bars, where the MQL5 side would read past history."""
    if n > 0:
        arr[:min(n, len(arr))] = np.nan
    return arr


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------
class Indicator(object):
    """Base class.  Subclasses implement compute() and mq5_body()."""

    kind = "indicator"

    def deps(self):
        """Names this indicator reads (price fields or other indicators)."""
        return []

    def warmup(self):
        """Bars of history needed before the first valid value."""
        return 0

    def compute(self, name, env):
        raise NotImplementedError

    def mq5_body(self, name):
        raise NotImplementedError

    def describe(self):
        return "%s(%s)" % (type(self).__name__, getattr(self, "period", ""))


# --------------------------------------------------------------------------
# moving averages / dispersion
# --------------------------------------------------------------------------
class SMA(Indicator):
    """Simple moving average."""

    def __init__(self, source="close", period=14):
        self.source = source
        self.period = int(period)

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.period

    def compute(self, name, env):
        x = _resolve(env, self.source)
        return _seq_sum(x, self.period) / float(self.period)

    def mq5_body(self, name):
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double sum = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "      sum += %s;\n"
            "   return(sum / %d.0);\n"
            "  }\n" % (name, self.period, mq5_source(self.source, "s + j"), self.period)
        )


class Sum(Indicator):
    """Rolling sum."""

    def __init__(self, source="close", period=14):
        self.source = source
        self.period = int(period)

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.period

    def compute(self, name, env):
        return _seq_sum(_resolve(env, self.source), self.period)

    def mq5_body(self, name):
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double sum = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "      sum += %s;\n"
            "   return(sum);\n"
            "  }\n" % (name, self.period, mq5_source(self.source, "s + j"))
        )


class StdDev(Indicator):
    """Rolling standard deviation.

    ``ddof=1`` (the pandas default, a sample stdev) is used unless told
    otherwise.  MT5's iStdDev is ``ddof=0``; picking one explicitly and
    emitting the SAME choice into MQL5 is the entire point.
    """

    def __init__(self, source="close", period=20, ddof=1):
        self.source = source
        self.period = int(period)
        self.ddof = int(ddof)

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.period

    def compute(self, name, env):
        x = _resolve(env, self.source)
        mean = _seq_sum(x, self.period) / float(self.period)
        acc = np.zeros(len(x), dtype=float)
        for j in range(self.period):
            d = _shift(x, j) - mean
            acc = acc + d * d
        return np.sqrt(acc / float(self.period - self.ddof))

    def mq5_body(self, name):
        denom = self.period - self.ddof
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double mean = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "      mean += %s;\n"
            "   mean /= %d.0;\n"
            "   double acc = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "     {\n"
            "      double d = %s - mean;\n"
            "      acc += d * d;\n"
            "     }\n"
            "   return(MathSqrt(acc / %d.0));\n"
            "  }\n" % (name, self.period, mq5_source(self.source, "s + j"),
                       self.period, self.period,
                       mq5_source(self.source, "s + j"), denom)
        )


class EMA(Indicator):
    """Exponential moving average over a FIXED window.

    A textbook EMA is recursive back to the first bar of the series, which is
    unreproducible in a tester that starts mid-history.  This one is seeded
    with an SMA ``window`` bars back and then iterated forward, so Python and
    MQL5 see byte-identical inputs and produce identical output.
    """

    def __init__(self, source="close", period=20, window=None):
        self.source = source
        self.period = int(period)
        self.window = int(window) if window else int(period) * 6
        if self.window - self.period < 1:
            raise ValueError("EMA window must exceed period")

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.window

    def compute(self, name, env):
        x = _resolve(env, self.source)
        a = 2.0 / (self.period + 1.0)
        m = self.window - self.period
        seed = _seq_sum_offset(x, self.period, m) / float(self.period)
        out = _seq_recursion(x, a, m, seed)
        return _blank_before(out, self.window - 1)

    def mq5_body(self, name):
        m = self.window - self.period
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double a = 2.0 / (%d.0 + 1.0);\n"
            "   double seed = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "      seed += %s;\n"
            "   seed /= %d.0;\n"
            "   double e = seed;\n"
            "   for(int k = %d; k >= 0; k--)\n"
            "      e = a * (%s) + (1.0 - a) * e;\n"
            "   return(e);\n"
            "  }\n" % (name, self.period, self.period,
                       mq5_source(self.source, "s + %d - j" % (self.window - 1)),
                       self.period, m - 1,
                       mq5_source(self.source, "s + k"))
        )


# --------------------------------------------------------------------------
# volatility / oscillators
# --------------------------------------------------------------------------
class ATR(Indicator):
    """Average True Range.

    ``method='sma'`` (default) is a simple average of True Range.
    ``method='wilder'`` reproduces MT5's iATR smoothing, seeded over a fixed
    window so it stays reproducible.
    """

    def __init__(self, period=14, method="sma", window=None):
        self.period = int(period)
        self.method = str(method).lower()
        if self.method not in ("sma", "wilder"):
            raise ValueError("ATR method must be 'sma' or 'wilder'")
        self.window = int(window) if window else int(period) * 10

    def deps(self):
        return ["high", "low", "close"]

    def warmup(self):
        return (self.period + 1) if self.method == "sma" else (self.window + 1)

    @staticmethod
    def _true_range(env):
        h = _resolve(env, "high")
        l = _resolve(env, "low")
        pc = _shift(_resolve(env, "close"), 1)
        return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))

    def compute(self, name, env):
        tr = self._true_range(env)
        if self.method == "sma":
            return _seq_sum(tr, self.period) / float(self.period)
        a = 1.0 / self.period
        m = self.window - self.period
        seed = _seq_sum_offset(tr, self.period, m) / float(self.period)
        out = _seq_recursion(tr, a, m, seed)
        return _blank_before(out, self.window)

    @staticmethod
    def _tr_at(idx):
        r = _CTX["rates"]
        return ("MathMax(%s[(%s) + 1].high - %s[(%s) + 1].low,"
                " MathMax(MathAbs(%s[(%s) + 1].high - %s[(%s) + 2].close),"
                " MathAbs(%s[(%s) + 1].low - %s[(%s) + 2].close)))"
                % (r, idx, r, idx, r, idx, r, idx, r, idx, r, idx))

    def mq5_body(self, name):
        if self.method == "sma":
            return (
                "double Ind_%s(int s)\n"
                "  {\n"
                "   double sum = 0.0;\n"
                "   for(int j = 0; j < %d; j++)\n"
                "      sum += %s;\n"
                "   return(sum / %d.0);\n"
                "  }\n" % (name, self.period, self._tr_at("s + j"), self.period)
            )
        m = self.window - self.period
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double a = 1.0 / %d.0;\n"
            "   double seed = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "      seed += %s;\n"
            "   seed /= %d.0;\n"
            "   double e = seed;\n"
            "   for(int k = %d; k >= 0; k--)\n"
            "      e = a * (%s) + (1.0 - a) * e;\n"
            "   return(e);\n"
            "  }\n" % (name, self.period, self.period,
                       self._tr_at("s + %d - j" % (self.window - 1)),
                       self.period, m - 1, self._tr_at("s + k"))
        )


class RSI(Indicator):
    """Relative Strength Index.

    ``method='sma'`` averages gains/losses with a plain rolling mean;
    ``method='wilder'`` is the classic (and MT5 iRSI) smoothing, seeded over a
    fixed window for reproducibility.
    """

    def __init__(self, source="close", period=14, method="wilder", window=None):
        self.source = source
        self.period = int(period)
        self.method = str(method).lower()
        if self.method not in ("sma", "wilder"):
            raise ValueError("RSI method must be 'sma' or 'wilder'")
        self.window = int(window) if window else int(period) * 10

    def deps(self):
        return [self.source]

    def warmup(self):
        return (self.period + 1) if self.method == "sma" else (self.window + 1)

    def compute(self, name, env):
        x = _resolve(env, self.source)
        prev = _shift(x, 1)
        gain = np.maximum(x - prev, 0.0)
        loss = np.maximum(prev - x, 0.0)
        if self.method == "sma":
            g = _seq_sum(gain, self.period) / float(self.period)
            l = _seq_sum(loss, self.period) / float(self.period)
            blank = self.period
        else:
            a = 1.0 / self.period
            m = self.window - self.period
            g = _seq_recursion(
                gain, a, m,
                _seq_sum_offset(gain, self.period, m) / float(self.period))
            l = _seq_recursion(
                loss, a, m,
                _seq_sum_offset(loss, self.period, m) / float(self.period))
            blank = self.window
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi = np.where(l == 0.0, 100.0, 100.0 - 100.0 / (1.0 + g / l))
        rsi = np.where(np.isnan(g) | np.isnan(l), np.nan, rsi)
        return _blank_before(rsi, blank)

    def _gain_at(self, idx):
        return "MathMax(%s - %s, 0.0)" % (
            mq5_source(self.source, idx), mq5_source(self.source, "(%s) + 1" % idx))

    def _loss_at(self, idx):
        return "MathMax(%s - %s, 0.0)" % (
            mq5_source(self.source, "(%s) + 1" % idx), mq5_source(self.source, idx))

    def mq5_body(self, name):
        if self.method == "sma":
            return (
                "double Ind_%s(int s)\n"
                "  {\n"
                "   double g = 0.0, l = 0.0;\n"
                "   for(int j = 0; j < %d; j++)\n"
                "     {\n"
                "      g += %s;\n"
                "      l += %s;\n"
                "     }\n"
                "   g /= %d.0; l /= %d.0;\n"
                "   if(l == 0.0) return(100.0);\n"
                "   return(100.0 - 100.0 / (1.0 + g / l));\n"
                "  }\n" % (name, self.period, self._gain_at("s + j"),
                           self._loss_at("s + j"), self.period, self.period)
            )
        m = self.window - self.period
        back = "s + %d - j" % (self.window - 1)
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double a = 1.0 / %d.0;\n"
            "   double g = 0.0, l = 0.0;\n"
            "   for(int j = 0; j < %d; j++)\n"
            "     {\n"
            "      g += %s;\n"
            "      l += %s;\n"
            "     }\n"
            "   g /= %d.0; l /= %d.0;\n"
            "   for(int k = %d; k >= 0; k--)\n"
            "     {\n"
            "      g = a * (%s) + (1.0 - a) * g;\n"
            "      l = a * (%s) + (1.0 - a) * l;\n"
            "     }\n"
            "   if(l == 0.0) return(100.0);\n"
            "   return(100.0 - 100.0 / (1.0 + g / l));\n"
            "  }\n" % (name, self.period, self.period,
                       self._gain_at(back), self._loss_at(back),
                       self.period, self.period, m - 1,
                       self._gain_at("s + k"), self._loss_at("s + k"))
        )


# --------------------------------------------------------------------------
# extremes
# --------------------------------------------------------------------------
class Highest(Indicator):
    def __init__(self, source="high", period=20):
        self.source = source
        self.period = int(period)

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.period

    def compute(self, name, env):
        x = _resolve(env, self.source)
        out = _shift(x, 0).copy()
        for j in range(1, self.period):
            out = np.maximum(out, _shift(x, j))
        return out

    def mq5_body(self, name):
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double v = %s;\n"
            "   for(int j = 1; j < %d; j++)\n"
            "      v = MathMax(v, %s);\n"
            "   return(v);\n"
            "  }\n" % (name, mq5_source(self.source, "s"), self.period,
                       mq5_source(self.source, "s + j"))
        )


class Lowest(Indicator):
    def __init__(self, source="low", period=20):
        self.source = source
        self.period = int(period)

    def deps(self):
        return [self.source]

    def warmup(self):
        return self.period

    def compute(self, name, env):
        x = _resolve(env, self.source)
        out = _shift(x, 0).copy()
        for j in range(1, self.period):
            out = np.minimum(out, _shift(x, j))
        return out

    def mq5_body(self, name):
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   double v = %s;\n"
            "   for(int j = 1; j < %d; j++)\n"
            "      v = MathMin(v, %s);\n"
            "   return(v);\n"
            "  }\n" % (name, mq5_source(self.source, "s"), self.period,
                       mq5_source(self.source, "s + j"))
        )


# --------------------------------------------------------------------------
# derived expression
# --------------------------------------------------------------------------
class Expr(Indicator):
    """An indicator defined as an expression over other indicators/prices.

    Example::

        bb_width = Expr("4.0 * std50 / sma50")
        slope    = Expr("(sma50[1] - sma50[6]) / atr[1]")
    """

    kind = "expr"

    def __init__(self, source):
        self.source = str(source)
        self._c = compile_indicator_expr(self.source, shift_var="s")

    def deps(self):
        return sorted(self._c.names)

    def warmup(self):
        return self._c.max_shift

    def compute(self, name, env):
        n = None
        E = {}
        for ref_name, shift in self._c.refs:
            arr = _resolve(env, ref_name)
            n = len(arr) if n is None else n
            E[(ref_name, shift)] = _shift(arr, shift)
        with np.errstate(divide="ignore", invalid="ignore"):
            out = eval(self._c.py_code, {"__builtins__": {}},  # noqa: S307
                       {"E": E, "np": np})
        out = np.asarray(out, dtype=float)
        if out.ndim == 0:
            out = np.full(n, float(out))
        return out

    def mq5_body(self, name):
        c = compile_indicator_expr(self.source, "s", _CTX["rates"], _CTX["suffix"])
        return (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   return(%s);\n"
            "  }\n" % (name, c.mq5_code)
        )

    def describe(self):
        return "Expr(%r)" % (self.source,)


# --------------------------------------------------------------------------
# higher timeframe
# --------------------------------------------------------------------------
class HTF(Indicator):
    """Evaluate an indicator on a HIGHER timeframe, without look-ahead.

    The value exposed on each lower-timeframe bar comes from the last
    **fully closed** higher-timeframe bar - never the one still forming.
    That single rule is what a TradingView ``request.security(..., [1],
    lookahead_on)`` expresses, and it is the most common source of a backtest
    that cannot be reproduced live: using the current H1 bar's EMA on an M1
    bar means knowing how the hour ends before it does.

    Example::

        "h1_ema": HTF(EMA("close", 50), "H1")

    The inner indicator may only read price fields, since it is evaluated
    against the higher-timeframe bars rather than the chart ones.
    """

    kind = "htf"

    def __init__(self, inner, timeframe):
        from .types import TIMEFRAMES
        if timeframe not in TIMEFRAMES:
            raise ValueError("unknown HTF timeframe %r" % (timeframe,))
        bad = [d for d in inner.deps() if d not in PRICE_FIELDS]
        if bad:
            raise ValueError(
                "HTF inner indicator may only read price fields, got %s" % bad)
        self.inner = inner
        self.timeframe = timeframe

    def deps(self):
        return []

    def warmup(self):
        return 0        # measured in HTF bars, not chart bars

    def inner_warmup(self):
        return self.inner.warmup()

    def compute(self, name, env):
        htf = env.get("__htf__", {}).get(self.timeframe)
        if htf is None:
            raise KeyError("no %s data loaded for HTF indicator %r"
                           % (self.timeframe, name))
        inner_vals = np.asarray(
            self.inner.compute(name + "_htf", htf), dtype=float)

        chart_t = np.asarray(env["__time__"], dtype="datetime64[ns]")
        htf_t = np.asarray(htf["__time__"], dtype="datetime64[ns]")

        # index of the HTF bar CONTAINING each chart bar, then step back one
        # so only a closed bar is ever read
        j = np.searchsorted(htf_t, chart_t, side="right") - 2
        out = np.full(len(chart_t), np.nan)
        ok = j >= 0
        out[ok] = inner_vals[j[ok]]
        return out

    def mq5_body(self, name):
        tf_enum = _TF_ENUM[self.timeframe]
        arr = "g_rates_%s" % self.timeframe
        with emit_context(rates=arr, suffix="_%s" % self.timeframe):
            inner_body = self.inner.mq5_body("%s_%s" % (name, self.timeframe))
        wrapper = (
            "double Ind_%s(int s)\n"
            "  {\n"
            "   // index of the %s bar containing this bar; +1 inside the\n"
            "   // helper then reads the previous, fully CLOSED one.\n"
            "   int j = iBarShift(_Symbol, %s, %s[(s) + 1].time, false);\n"
            "   if(j < 0)\n"
            "      return(0.0);\n"
            "   return(Ind_%s_%s(j));\n"
            "  }\n" % (name, self.timeframe, tf_enum, _CTX["rates"],
                       name, self.timeframe)
        )
        return inner_body + "\n" + wrapper

    def describe(self):
        return "HTF(%s, %s)" % (self.inner.describe(), self.timeframe)


_TF_ENUM = {
    "M1": "PERIOD_M1", "M5": "PERIOD_M5", "M15": "PERIOD_M15",
    "M30": "PERIOD_M30", "H1": "PERIOD_H1", "H4": "PERIOD_H4", "D1": "PERIOD_D1",
}


INDICATOR_TYPES = {
    "SMA": SMA, "EMA": EMA, "StdDev": StdDev, "ATR": ATR, "RSI": RSI,
    "Highest": Highest, "Lowest": Lowest, "Sum": Sum, "Expr": Expr, "HTF": HTF,
}
