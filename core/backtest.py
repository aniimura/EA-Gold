# -*- coding: utf-8 -*-
"""Python backtest engine that mirrors MT5 execution bar for bar.

The engine deliberately models only what the generated EA can also do:

  entry   : signal read on the last completed bar, filled at the next bar OPEN
            (long pays the spread: fill = open + spread; short fills at open)
  SL / TP : attached to the position, triggered intrabar from the bar's
            high/low (long is checked on bid, short on ask)
  time    : closed at a bar OPEN once ``bars_held >= max_hold_bars``
  signal  : closed at a bar OPEN when the exit expression is true
  re-entry: never on the same bar as an exit, and never before
            ``min_bars_between`` bars have passed since the last entry

When a single bar contains both the SL and the TP level, the order of the two
is unknowable from OHLC.  The engine takes the SL (the conservative choice)
and flags the trade ``ambiguous`` so the reconciler can tell a genuine logic
divergence apart from intrabar path uncertainty.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import math

from .expr import PRICE_FIELDS
from .spec import LONG, SHORT, Strategy
from .timeutil import time_fields
from .types import (BacktestResult, SymbolInfo, Trade, mt5_round,
                    EXIT_SL, EXIT_TP, EXIT_TIME, EXIT_SIGNAL, EXIT_EOD)

EXIT_TRAIL = "TRAIL"


# --------------------------------------------------------------------------
def _shift(arr, n):
    if n == 0:
        return arr
    out = np.full(len(arr), np.nan, dtype=float)
    out[n:] = arr[:-n]
    return out


def _shift_back(arr):
    """out[t] = arr[t + 1]; the last position is left False/NaN."""
    out = np.zeros(len(arr), dtype=arr.dtype)
    out[:-1] = arr[1:]
    return out


def _half_tick(value, digits, eps=5e-10):
    """True when ``value`` sits within ``eps`` of a .5 tick rounding boundary."""
    if value is None or not np.isfinite(value):
        return False
    frac = abs(value) * (10.0 ** digits)
    return abs(frac - np.floor(frac) - 0.5) < eps


def _price_env(strategy, frame):
    env = {}
    for field, col in PRICE_FIELDS.items():
        if col in frame.columns:
            env[field] = frame[col].to_numpy(dtype=float)
        elif field in frame.columns:
            env[field] = frame[field].to_numpy(dtype=float)
        else:
            env[field] = np.full(len(frame), np.nan)
    env["__time__"] = frame["time"].to_numpy()
    env.update(time_fields(frame["time"], strategy.broker_gmt_offset,
                           strategy.broker_dst))
    return env


def compute_indicators(strategy: Strategy, df: pd.DataFrame, htf_frames=None):
    """Evaluate every indicator over the full bar history."""
    env = _price_env(strategy, df)
    env["__htf__"] = {tf: _price_env(strategy, f)
                      for tf, f in (htf_frames or {}).items()}
    for name in strategy.order:
        env[name] = np.asarray(strategy.indicators[name].compute(name, env), dtype=float)
    return env


def evaluate_signal(compiled, env, n):
    """Signal array where ``out[i]`` decides the order filled at bar ``i``.

    Reference ``name[k]`` resolves to bar ``i-1-k``: the ``-1`` is the
    forming-bar offset that makes look-ahead impossible.
    """
    if compiled is None:
        return np.zeros(n, dtype=bool)
    E = {}
    for name, k in compiled.refs:
        E[(name, k)] = _shift(env[name], k + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = eval(compiled.py_code, {"__builtins__": {}}, {"E": E, "np": np})  # noqa: S307
    out = np.asarray(out)
    if out.ndim == 0:
        out = np.full(n, out)
    if out.dtype != bool:
        out = np.nan_to_num(out.astype(float), nan=0.0) != 0.0
    return out.astype(bool)


# --------------------------------------------------------------------------
class _Engine(object):

    def __init__(self, strategy, df, sym, spread_points, spread_series, start_idx):
        self.s = strategy
        self.df = df
        self.sym = sym
        self.start_idx = start_idx
        self.o = df["open"].to_numpy(dtype=float)
        self.h = df["high"].to_numpy(dtype=float)
        self.l = df["low"].to_numpy(dtype=float)
        self.c = df["close"].to_numpy(dtype=float)
        self.t = df["time"].to_numpy()
        self.n = len(df)
        if spread_series is not None:
            self.spread_pts = np.asarray(spread_series, dtype=float)
        else:
            self.spread_pts = np.full(self.n, float(spread_points or 0.0))
        self.trades = []
        # position state
        self.pos = None
        self.peak = float("nan")

    # ---------------------------------------------------------------- utils
    def spread_price(self, i):
        v = self.spread_pts[i]
        return 0.0 if not np.isfinite(v) else v * self.sym.point

    def _distances(self, i, atr_arr):
        """SL/TP distances in price units, computed on the signal bar."""
        e = self.s.exits
        sl = tp = None
        if e.sl_atr is not None or e.tp_atr is not None:
            a = atr_arr[i - 1]
            if not np.isfinite(a) or a <= 0:
                return None, None
            if e.sl_atr is not None:
                sl = e.sl_atr * a
            if e.tp_atr is not None:
                tp = e.tp_atr * a
        if e.sl_points is not None:
            sl = e.sl_points * self.sym.point
        if e.tp_points is not None:
            tp = e.tp_points * self.sym.point
        if sl is not None and e.sl_min_points:
            sl = max(sl, e.sl_min_points * self.sym.point)
        return sl, tp

    def _lots(self, sl_d):
        """Position size for this trade, or 0 when it is not tradeable."""
        z = self.s.sizing
        if not z.is_risk():
            return self.s.lot
        if not sl_d or sl_d <= 0:
            return 0.0
        money_per_price = self.sym.contract_size          # per 1.0 lot
        raw = z.risk_money / (sl_d * money_per_price)
        lots = math.floor(raw / z.lot_step) * z.lot_step
        if lots < z.lot_min:
            return 0.0
        return min(lots, z.lot_max)

    def update_trail(self, i):
        """Move the stop using the bar that has just CLOSED (bar i-1).

        The new level is therefore active for bar i onwards - never for the
        bar whose high produced it, which would be look-ahead.
        """
        t = self.s.trail
        p = self.pos
        if not t.active() or p is None or i <= p.entry_bar:
            return
        money_per_price = self.sym.contract_size * p.lots
        if money_per_price <= 0:
            return
        j = i - 1
        if p.direction == LONG:
            self.peak = max(self.peak, self.h[j])
            profit = (self.peak - p.entry_price) * money_per_price
        else:
            self.peak = min(self.peak, self.l[j])
            profit = (p.entry_price - self.peak) * money_per_price
        if profit < t.start_money:
            return
        lock = max(profit - t.step_money, 0.0)
        delta = lock / money_per_price
        new_sl = (p.entry_price + delta) if p.direction == LONG else (p.entry_price - delta)
        new_sl = mt5_round(new_sl, self.sym.digits)

        if p.direction == LONG:
            if not (new_sl > p.sl and new_sl > p.entry_price):
                return
        else:
            if not (new_sl < p.sl and new_sl < p.entry_price):
                return

        # A broker rejects a stop that is already on the wrong side of the
        # market - it would fire on the next tick.  Skipping this check is not
        # a small modelling nicety: on a fast reversal the Python engine would
        # "exit" at a level MT5 never accepted, then take a different next
        # trade, and the two runs diverge for the rest of the backtest.
        # The modification happens on the first tick of this bar, so bid is
        # the bar's open and ask is open + spread.
        bid = self.o[i]
        ask = bid + self.spread_price(i)
        stop_min = self.sym.trade_stops_level * self.sym.point
        if p.direction == LONG:
            if not new_sl < bid - stop_min:
                return
        else:
            if not new_sl > ask + stop_min:
                return

        p.sl = new_sl
        p.trailed = True

    # -------------------------------------------------------------- actions
    def open_position(self, i, direction, atr_arr):
        sl_d, tp_d = self._distances(i, atr_arr)
        if sl_d is None and tp_d is None and not self.s.exits.max_hold_bars \
                and not self.s.exits.exit_long and not self.s.exits.exit_short:
            return False
        lots = self._lots(sl_d)
        if lots <= 0:
            return False
        spr = self.spread_price(i)
        dg = self.sym.digits
        stop_min = self.sym.trade_stops_level * self.sym.point

        if direction == LONG:
            entry = mt5_round(self.o[i] + spr, dg)
            raw_sl, raw_tp = entry - (sl_d or 0.0), entry + (tp_d or 0.0)
        else:
            entry = mt5_round(self.o[i], dg)
            raw_sl, raw_tp = entry + (sl_d or 0.0), entry - (tp_d or 0.0)
        sl = mt5_round(raw_sl, dg) if sl_d else 0.0
        tp = mt5_round(raw_tp, dg) if tp_d else 0.0

        # A stop that lands exactly halfway between two ticks is decided by the
        # last bit of the ATR and of the broker's ask - neither of which can be
        # reproduced from bar data with certainty.  Flag it so a resulting
        # one-point difference is recognised as arithmetic, not logic.
        on_edge = ((sl_d and _half_tick(raw_sl, dg))
                   or (tp_d and _half_tick(raw_tp, dg)))

        # MT5 rejects stops closer than STOPLEVEL - skip on both sides alike.
        if stop_min > 0:
            if sl and abs(entry - sl) < stop_min:
                return False
            if tp and abs(entry - tp) < stop_min:
                return False

        a = atr_arr[i - 1] if atr_arr is not None else float("nan")
        self.pos = Trade(
            idx=len(self.trades) + 1, direction=direction, entry_bar=i,
            entry_time=pd.Timestamp(self.t[i]), entry_price=entry,
            sl=sl, tp=tp, entry_atr=float(a),
            entry_spread_points=float(self.spread_pts[i]),
            lots=float(lots),
            note="boundary" if on_edge else "",
        )
        self.peak = entry
        return True

    def close_position(self, i, price, reason, ambiguous=False):
        p = self.pos
        p.exit_bar = i
        p.exit_time = pd.Timestamp(self.t[i])
        p.exit_price = mt5_round(price, self.sym.digits)
        p.exit_reason = reason
        p.bars_held = i - p.entry_bar
        p.ambiguous = bool(ambiguous)
        self.trades.append(p)
        self.pos = None

    def check_intrabar(self, i):
        """Return (hit_price, reason, ambiguous) or (None, None, False)."""
        p = self.pos
        spr = self.spread_price(i)
        if p.direction == LONG:
            sl_hit = bool(p.sl) and self.l[i] <= p.sl
            tp_hit = bool(p.tp) and self.h[i] >= p.tp
        else:
            sl_hit = bool(p.sl) and (self.h[i] + spr) >= p.sl
            tp_hit = bool(p.tp) and (self.l[i] + spr) <= p.tp
        if sl_hit and tp_hit:
            return p.sl, EXIT_SL, True
        if sl_hit:
            return p.sl, EXIT_SL, False
        if tp_hit:
            return p.tp, EXIT_TP, False
        return None, None, False

    def bar_open_exit_price(self, i):
        return self.o[i] if self.pos.direction == LONG else self.o[i] + self.spread_price(i)


# --------------------------------------------------------------------------
def run_backtest(strategy: Strategy, df: pd.DataFrame, sym: SymbolInfo,
                 spread_points=None, spread_series=None,
                 start_time=None, end_time=None, collect_bars=False,
                 htf_frames=None):
    """Run the Python backtest and return a :class:`BacktestResult`."""
    n = len(df)
    env = compute_indicators(strategy, df, htf_frames)

    sig_long = evaluate_signal(strategy.compiled["entry_long"], env, n)
    sig_short = evaluate_signal(strategy.compiled["entry_short"], env, n)
    ex_long = evaluate_signal(strategy.compiled["exit_long"], env, n)
    ex_short = evaluate_signal(strategy.compiled["exit_short"], env, n)

    atr_arr = env.get(strategy.exits.atr_name) if strategy.exits.uses_atr() else None
    if atr_arr is None:
        atr_arr = np.full(n, np.nan)

    times = pd.to_datetime(pd.Series(df["time"].to_numpy()))
    warm = strategy.warmup_bars()
    in_window = np.ones(n, dtype=bool)
    if start_time is not None:
        in_window &= (times >= pd.Timestamp(start_time)).to_numpy()
    if end_time is not None:
        in_window &= (times < pd.Timestamp(end_time)).to_numpy()
    first = int(np.argmax(in_window)) if in_window.any() else n
    start_idx = max(first, warm + 1)

    eng = _Engine(strategy, df, sym, spread_points, spread_series, start_idx)
    max_hold = strategy.exits.max_hold_bars
    gap = strategy.min_bars_between
    last_entry_bar = -10 ** 9

    for i in range(start_idx, n):
        if not in_window[i]:
            if eng.pos is not None:
                eng.close_position(i, eng.bar_open_exit_price(i), EXIT_EOD)
            break

        closed_this_bar = False

        if eng.pos is not None:
            p = eng.pos
            bars_held = i - p.entry_bar
            # Trail first: the level derived from the bar that just closed is
            # what the broker holds while THIS bar trades.
            eng.update_trail(i)
            exit_sig = ex_long[i] if p.direction == LONG else ex_short[i]
            if max_hold and bars_held >= max_hold:
                eng.close_position(i, eng.bar_open_exit_price(i), EXIT_TIME)
                closed_this_bar = True
            elif exit_sig:
                eng.close_position(i, eng.bar_open_exit_price(i), EXIT_SIGNAL)
                closed_this_bar = True
            else:
                px, reason, amb = eng.check_intrabar(i)
                if px is not None:
                    eng.close_position(i, px, reason, amb)
                    closed_this_bar = True

        if eng.pos is None and not closed_this_bar:
            if (i - last_entry_bar) >= gap:
                direction = None
                if sig_long[i]:
                    direction = LONG
                elif sig_short[i]:
                    direction = SHORT
                if direction is not None and eng.open_position(i, direction, atr_arr):
                    last_entry_bar = i
                    px, reason, amb = eng.check_intrabar(i)
                    if px is not None:
                        eng.close_position(i, px, reason, amb)

    if eng.pos is not None:
        last = min(n - 1, i)
        eng.close_position(last, eng.c[last], EXIT_EOD)

    apply_costs(eng.trades, strategy)

    bars = None
    if collect_bars:
        cols = {"time": df["time"].to_numpy(),
                "open": eng.o, "high": eng.h, "low": eng.l, "close": eng.c}
        for name in strategy.order:
            cols[name] = env[name]
        # sig_long[i] is the decision for the order FILLED at bar i, i.e. it was
        # computed on bar i-1.  Label it with the bar it was computed on, which
        # is what the EA writes, so the two diagnostics line up.
        cols["sig_long"] = _shift_back(sig_long)
        cols["sig_short"] = _shift_back(sig_short)
        bars = pd.DataFrame(cols)

    res = BacktestResult(strategy=strategy.name, symbol=strategy.symbol,
                         timeframe=strategy.timeframe, trades=eng.trades, bars=bars)
    res.stats = summarize(eng.trades, sym.contract_size, strategy.lot,
                          deposit=strategy.deposit)
    return res


# --------------------------------------------------------------------------
def count_rollovers(entry_time, exit_time, triple_weekday=2):
    """Weighted overnight rollovers between two timestamps (server time)."""
    a = pd.Timestamp(entry_time).normalize()
    b = pd.Timestamp(exit_time).normalize()
    n = 0
    while a < b:
        a = a + pd.Timedelta(days=1)
        n += 3 if a.dayofweek == triple_weekday else 1
    return n


def apply_costs(trades, strategy):
    """Book swap and commission onto each trade, per the spec's cost model."""
    c = strategy.costs
    lot = strategy.lot
    for t in trades:
        if t.exit_time is None or t.entry_time is None:
            continue
        t.nights = count_rollovers(t.entry_time, t.exit_time, c.triple_swap_weekday)
        rate = (c.swap_long_per_lot_night if t.direction == LONG
                else c.swap_short_per_lot_night)
        size = t.size(lot)
        t.swap = rate * size * t.nights
        t.commission = c.commission_per_lot * size
    return trades


def summarize(trades, contract_size=100000.0, lot=0.01, deposit=10000.0):
    """Headline statistics, comparable with the MT5 report.

    ``net_profit`` is after costs, matching what MT5 reports; ``gross_profit``
    is price movement only, which is the figure the reconciler compares.
    """
    if not trades:
        return {"trades": 0, "net_profit": 0.0, "gross_profit": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0, "sharpe": 0.0,
                "max_dd": 0.0, "max_dd_pct": 0.0, "points": 0.0,
                "pnl_atr": 0.0, "swap": 0.0, "commission": 0.0, "ambiguous": 0}
    gross = np.array([t.pnl_money(contract_size, lot) for t in trades], dtype=float)
    money = np.array([t.net_money(contract_size, lot) for t in trades], dtype=float)
    pts = np.array([t.points for t in trades], dtype=float)
    atr = np.array([t.pnl_atr() for t in trades], dtype=float)
    wins = money[money > 0]
    losses = money[money < 0]
    eq = deposit + np.cumsum(money)
    peak = np.maximum.accumulate(np.concatenate(([deposit], eq)))
    dd = peak - np.concatenate(([deposit], eq))
    max_dd = float(dd.max())
    denom = money.std(ddof=1) if len(money) > 1 else 0.0
    return {
        "trades": int(len(trades)),
        "net_profit": float(money.sum()),
        "gross_profit": float(gross.sum()),
        "swap": float(sum(t.swap for t in trades)),
        "commission": float(sum(t.commission for t in trades)),
        "win_rate": float(100.0 * len(wins) / len(money)),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else float("inf"),
        "sharpe": float(money.mean() / denom * np.sqrt(len(money))) if denom else 0.0,
        "expected_payoff": float(money.mean()),
        "max_dd": max_dd,
        "max_dd_pct": float(100.0 * max_dd / peak.max()) if peak.max() else 0.0,
        "points": float(pts.sum()),
        "pnl_atr": float(np.nansum(atr)),
        "ambiguous": int(sum(1 for t in trades if t.ambiguous)),
        "boundary": int(sum(1 for t in trades if t.note == "boundary")),
    }
