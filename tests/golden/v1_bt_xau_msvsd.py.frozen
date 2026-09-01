# -*- coding: utf-8 -*-
"""Backtest for XAU_MultiSpeed_VolScaled_Donchian.pine (Pine v6).

WHY THIS IS A STANDALONE RUNNER AND NOT A core.spec Strategy
    core.spec models ONE position with one entry expression, one ATR stop and
    one trail. This strategy is three independent Donchian state machines whose
    signed sizes are netted into a single broker position. That cannot be
    written as `entry_long=...`, so the sleeve engine is reimplemented here
    bar-for-bar against the same GOLD H4 cache and the same FxPro cost
    calibration the rest of the repo uses.

WHAT IS BIT-FAITHFUL TO THE PINE
    Donchian levels    ta.highest(high[1], n) / ta.lowest(low[1], n)  - the
                       current bar is excluded everywhere.
    ATR                ta.atr(20) == RMA(TrueRange, 20), seeded with the SMA of
                       the first 20 true ranges. This is Pine's seeding, NOT
                       core.indicators.ATR(method='wilder') which uses a fixed
                       200-bar window to match MT5's iATR. Backtesting the Pine
                       means matching Pine.
    Sleeve state       identical block order: register fill at this bar's open
                       -> protective stop -> channel exit -> entry.
    Sizing             lots = (equity * risk%) / (2.5 * ATR_at_entry * 100),
                       floored to 0.01. ATR frozen at the entry signal.
    Netting            signed sum -> notional cap -> floor to lot step -> only
                       the delta vs the live position is traded.
    Execution          signal on the completed bar's close, fill at the NEXT
                       bar's open. Never same-bar.
    Stop modelling     breach detected on the completed bar's low/high, exit
                       filled at the next bar's open. Same conservative
                       approximation the Pine documents - gap risk beyond the
                       stop is fully absorbed.
    Friday filter      no new sleeve from 13:00 America/New_York on Friday.
                       Server time -> UTC via core.timeutil (FxPro EET/EEST),
                       UTC -> New York via the real tz database.

WHAT THE PINE CANNOT DO AND THIS RUNNER ADDS
    Swap. TradingView models no overnight carry. FxPro GOLD pays -52.40 USD per
    lot per night long and +23.58 short. For a strategy whose median hold is
    measured in weeks this is not a rounding error, so it is booked per night
    on the live net position, triple on Wednesday.

COST MODEL (matches core.backtest's convention: bars are BID)
    long fills at open + spread, short fills at open, plus a slippage
    allowance on both sides; commission 7.85 USD per lot round turn charged
    per side on every ounce that changes hands.

Usage
    python bt_xau_msvsd.py                       headline run
    python bt_xau_msvsd.py --capital 1000000     removes lot-rounding drag
    python bt_xau_msvsd.py --no-swap             isolate the carry
    python bt_xau_msvsd.py --sleeves fast        one sleeve only
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config, data as datamod          # noqa: E402
from core.backtest import count_rollovers         # noqa: E402
from core.timeutil import server_to_utc           # noqa: E402

SYMBOL = "GOLD"
TIMEFRAME = "H4"
DATE_FROM = "2022-01-01"
DATE_TO = "2026-08-31"
NAME = "XauMsvsd"

# ---- Pine input defaults, mirrored exactly -------------------------------
SLEEVE_DEFS = [
    ("fast", 20, 10),
    ("medium", 55, 20),
    ("slow", 120, 40),
]
RISK_PCT = 0.10          # per sleeve, % of equity
ATR_LEN = 20
ATR_MULT = 2.5
CONTRACT_OZ = 100.0
LOT_STEP = 0.01
MAX_NOTIONAL_X = 1.5
FRIDAY_HOUR = 13         # New York
ALLOW_REVERSAL = True

# ---- FxPro GOLD calibration used elsewhere in this repo ------------------
COMMISSION_PER_LOT_RT = 7.85        # USD, round turn
SWAP_LONG_PER_LOT_NIGHT = -52.40
SWAP_SHORT_PER_LOT_NIGHT = 23.58
TRIPLE_SWAP_WEEKDAY = 2             # Wednesday
SLIPPAGE_POINTS = 5.0               # 0.05 USD/oz per side, on top of spread
POINT = 0.01

WARMUP_BARS = 364


# =========================================================================
# Indicators - Pine semantics
# =========================================================================
def pine_atr(high, low, close, length):
    """ta.atr(length) == ta.rma(ta.tr, length), seeded with the SMA of the
    first `length` true ranges. NaN before that."""
    n = len(close)
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    tr[0] = high[0] - low[0]                       # Pine's tr on bar 0
    out = np.full(n, np.nan)
    if n < length:
        return out
    seed = tr[:length].mean()
    out[length - 1] = seed
    a = 1.0 / length
    for i in range(length, n):
        out[i] = a * tr[i] + (1.0 - a) * out[i - 1]
    return out


def rolling_extreme(arr, length, kind):
    """ta.highest/ta.lowest over `length` bars, NaN until the window is full."""
    s = pd.Series(arr)
    r = s.rolling(length, min_periods=length)
    return (r.max() if kind == "max" else r.min()).to_numpy()


def donchian(high, low, length, include_current=False):
    """Levels that EXCLUDE the current bar: ta.highest(high[1], length).

    `include_current=True` is the AUDIT variant only - it is the lookahead bug
    this strategy is written to avoid, kept so the guard can be shown to matter.
    """
    if include_current:
        return (rolling_extreme(high, length, "max"),
                rolling_extreme(low, length, "min"))
    hi_prev = np.concatenate(([np.nan], high[:-1]))
    lo_prev = np.concatenate(([np.nan], low[:-1]))
    return (rolling_extreme(hi_prev, length, "max"),
            rolling_extreme(lo_prev, length, "min"))


def floor_step(x, step):
    return max(0.0, np.floor(x / step + 1e-9) * step)


# =========================================================================
# Sleeve state machine - one instance per sleeve, mirroring the Pine function
# =========================================================================
class Sleeve(object):
    __slots__ = ("name", "entry_len", "exit_len", "dir", "lots", "entry_px",
                 "stop_px", "atr_ent", "pending", "entry_time", "entry_bar")

    def __init__(self, name, entry_len, exit_len):
        self.name = name
        self.entry_len = entry_len
        self.exit_len = exit_len
        self.reset()

    def reset(self):
        self.pending = 0
        self.dir = 0
        self.lots = 0.0
        self.entry_px = np.nan
        self.stop_px = np.nan
        self.atr_ent = np.nan
        self.entry_time = None
        self.entry_bar = -1


def step(sl, bar, lv, atr_now, risk_cash, entries_blocked, allow_rev):
    """One bar of the Pine `sleeveStep()` function.

    Returns (exit_ev, ent_ev): exit_ev 0/3(channel)/4(stop),
                               ent_ev  0/1(long)/2(short).
    """
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    ent_hi, ent_lo, ex_hi, ex_lo = lv
    exit_ev = 0
    ent_ev = 0

    # (A) register the fill of an entry submitted on the previous bar's close.
    # Pine models the fill at the raw chart open; spread/slippage are booked in
    # the cash ledger instead, so the sleeve logic stays comparable to Pine.
    if sl.pending == 1:
        sl.entry_px = o
        sl.stop_px = (o - sl.atr_ent * ATR_MULT if sl.dir == 1
                      else o + sl.atr_ent * ATR_MULT)
        sl.entry_time = bar["time"]
        sl.pending = 0

    # (B) protective stop - highest priority
    if sl.dir != 0 and np.isfinite(sl.stop_px):
        breached = (l <= sl.stop_px) if sl.dir == 1 else (h >= sl.stop_px)
        if breached:
            exit_ev = 4

    # (C) Donchian channel exit
    if exit_ev == 0 and sl.dir != 0 and np.isfinite(ex_hi) and np.isfinite(ex_lo):
        if (c < ex_lo) if sl.dir == 1 else (c > ex_hi):
            exit_ev = 3

    # (D) entry - flat sleeves only, never adds to an existing position
    can_enter = (sl.dir == 0 or exit_ev != 0) and not entries_blocked \
        and np.isfinite(ent_hi) and np.isfinite(ent_lo) \
        and np.isfinite(atr_now) and atr_now > 0 \
        and (exit_ev == 0 or allow_rev)
    new_dir = 0
    if can_enter:
        if c > ent_hi:
            new_dir = 1
        elif c < ent_lo:
            new_dir = -1

    if new_dir != 0:
        stop_dist = atr_now * ATR_MULT
        risk_per_lot = stop_dist * CONTRACT_OZ
        lots = floor_step(risk_cash / risk_per_lot, LOT_STEP) if risk_per_lot > 0 else 0.0
        if lots <= 0:
            new_dir = 0
        else:
            sl.reset()
            sl.dir = new_dir
            sl.lots = lots
            sl.atr_ent = atr_now
            sl.pending = 1
            ent_ev = 1 if new_dir == 1 else 2
    elif exit_ev != 0:
        sl.reset()

    return exit_ev, ent_ev


# =========================================================================
# Engine
# =========================================================================
def run(df, capital, risk_pct, use_swap, use_costs, active_sleeves,
        max_notional_x, lot_step, friday_filter,
        lookahead=False, same_bar_fill=False):
    """`lookahead` and `same_bar_fill` are AUDIT switches. Both are cheating;
    they exist so the cost of the honest choices can be measured."""
    t = df["time"].to_numpy()
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    spread_pts = df["spread"].to_numpy(float)
    n = len(df)

    atr = pine_atr(h, l, c, ATR_LEN)

    sleeves = [Sleeve(nm, e, x) for nm, e, x in SLEEVE_DEFS if nm in active_sleeves]
    levels = {}
    for sl in sleeves:
        levels[(sl.name, "ent")] = donchian(h, l, sl.entry_len, lookahead)
        levels[(sl.name, "exit")] = donchian(h, l, sl.exit_len, lookahead)

    # Friday 13:00 New York, derived from broker server time.
    utc = pd.DatetimeIndex(server_to_utc(df["time"], winter_offset_hours=2, dst="eu"))
    ny = utc.tz_localize("UTC").tz_convert("America/New_York")
    friday_block = ((ny.dayofweek == 4) & (ny.hour >= FRIDAY_HOUR)) if friday_filter \
        else np.zeros(n, dtype=bool)

    # -- account state -----------------------------------------------------
    realized = 0.0
    pos_oz = 0.0
    avg_px = 0.0
    pending_delta = 0.0          # ounces to trade at the next bar's open
    cost_comm = cost_spread = cost_swap = 0.0

    trades = []                  # sleeve-level virtual trades
    open_rec = {}                # sleeve name -> dict
    equity_curve = np.full(n, np.nan)
    pos_curve = np.zeros(n)
    cap_hits = 0

    warm = max([sl.entry_len for sl in sleeves] + [ATR_LEN]) + 2
    comm_per_oz_side = (COMMISSION_PER_LOT_RT / 2.0) / CONTRACT_OZ

    for i in range(n):
        # ---- 1. execute the order queued on the previous bar's close ------
        if pending_delta != 0.0:
            spr = spread_pts[i] * POINT
            slip = SLIPPAGE_POINTS * POINT
            # bars are BID: buying lifts the ask, selling hits the bid
            fill = o[i] + spr + slip if pending_delta > 0 else o[i] - slip
            qty = abs(pending_delta)
            if use_costs:
                cost_spread += (fill - o[i]) * pending_delta   # always >= 0
                cost_comm += comm_per_oz_side * qty
                realized -= comm_per_oz_side * qty
            eff = o[i] if not use_costs else fill

            new_pos = pos_oz + pending_delta
            if pos_oz == 0.0 or np.sign(pos_oz) == np.sign(pending_delta):
                avg_px = (avg_px * abs(pos_oz) + eff * qty) / (abs(pos_oz) + qty)
            else:
                # opposing delta: book P&L on the ounces that get closed, and
                # re-base the average if the order flips the position outright
                closed = min(abs(pos_oz), qty)
                realized += closed * (eff - avg_px) * np.sign(pos_oz)
                if qty > abs(pos_oz):
                    avg_px = eff
            pos_oz = new_pos
            if abs(pos_oz) < 1e-9:
                pos_oz = 0.0
                avg_px = 0.0
            pending_delta = 0.0

        # ---- 2. overnight carry on the position actually held -------------
        if i > 0 and pos_oz != 0.0 and use_swap and use_costs:
            nights = count_rollovers(t[i - 1], t[i], TRIPLE_SWAP_WEEKDAY)
            if nights:
                rate = SWAP_LONG_PER_LOT_NIGHT if pos_oz > 0 else SWAP_SHORT_PER_LOT_NIGHT
                sw = rate * (abs(pos_oz) / CONTRACT_OZ) * nights
                realized += sw
                cost_swap += sw

        equity = capital + realized + pos_oz * (c[i] - avg_px)
        equity_curve[i] = equity
        pos_curve[i] = pos_oz / CONTRACT_OZ

        if i < warm or not np.isfinite(atr[i]):
            continue

        # ---- 3. sleeve state machines on the COMPLETED bar ----------------
        bar = {"time": t[i], "open": o[i], "high": h[i], "low": l[i], "close": c[i]}
        risk_cash = equity * risk_pct / 100.0
        blocked = bool(friday_block[i])

        for sl in sleeves:
            eh, el = levels[(sl.name, "ent")]
            xh, xl = levels[(sl.name, "exit")]
            lv = (eh[i], el[i], xh[i], xl[i])
            prev = dict(dir=sl.dir, lots=sl.lots, entry_px=sl.entry_px,
                        stop_px=sl.stop_px, atr_ent=sl.atr_ent,
                        entry_time=sl.entry_time)
            xev, nev = step(sl, bar, lv, atr[i], risk_cash, blocked, ALLOW_REVERSAL)

            if xev != 0 and prev["dir"] != 0 and np.isfinite(prev["entry_px"]):
                # exit fills at the NEXT bar's open, like the entry did
                px_out = o[i + 1] if i + 1 < n else c[i]
                stop_dist = prev["atr_ent"] * ATR_MULT
                pts = (px_out - prev["entry_px"]) * prev["dir"]
                trades.append(dict(
                    sleeve=sl.name, direction="long" if prev["dir"] == 1 else "short",
                    entry_time=prev["entry_time"], exit_time=t[i + 1] if i + 1 < n else t[i],
                    entry_price=prev["entry_px"], exit_price=px_out,
                    lots=prev["lots"], atr_at_entry=prev["atr_ent"],
                    stop_price=prev["stop_px"], stop_dist=stop_dist,
                    reason="stop" if xev == 4 else "channel",
                    points=pts, gross=pts * prev["lots"] * CONTRACT_OZ,
                    r_multiple=pts / stop_dist if stop_dist > 0 else np.nan))
            if nev != 0:
                open_rec[sl.name] = True

        # ---- 4. netting -> one broker target -----------------------------
        net_lots_raw = sum(sl.dir * sl.lots for sl in sleeves)
        cap_lots = (equity * max_notional_x) / (CONTRACT_OZ * c[i]) if c[i] > 0 else 0.0
        clipped = min(abs(net_lots_raw), cap_lots)
        net_lots = np.sign(net_lots_raw) * floor_step(clipped, lot_step)
        if abs(net_lots_raw) - abs(net_lots) > lot_step / 2:
            cap_hits += 1

        target_oz = net_lots * CONTRACT_OZ
        delta = target_oz - pos_oz
        if abs(delta) >= CONTRACT_OZ * lot_step * 0.5:
            if same_bar_fill:
                # AUDIT ONLY: the optimistic fill this strategy refuses to use -
                # the order executes at the close of the very bar that fired it.
                spr = spread_pts[i] * POINT
                slip = SLIPPAGE_POINTS * POINT
                fill = c[i] + spr + slip if delta > 0 else c[i] - slip
                qty = abs(delta)
                if use_costs:
                    cost_comm += comm_per_oz_side * qty
                    realized -= comm_per_oz_side * qty
                eff = c[i] if not use_costs else fill
                if pos_oz == 0.0 or np.sign(pos_oz) == np.sign(delta):
                    avg_px = (avg_px * abs(pos_oz) + eff * qty) / (abs(pos_oz) + qty)
                else:
                    closed = min(abs(pos_oz), qty)
                    realized += closed * (eff - avg_px) * np.sign(pos_oz)
                    if qty > abs(pos_oz):
                        avg_px = eff
                pos_oz += delta
                if abs(pos_oz) < 1e-9:
                    pos_oz, avg_px = 0.0, 0.0
            else:
                pending_delta = delta

    # close whatever is still open at the final bar, at that bar's close
    if pos_oz != 0.0:
        realized += pos_oz * (c[-1] - avg_px)
        equity_curve[-1] = capital + realized
        pos_oz = 0.0

    return dict(
        equity=equity_curve, position_lots=pos_curve, time=t,
        trades=pd.DataFrame(trades), realized=realized,
        cost_comm=cost_comm, cost_spread=cost_spread, cost_swap=cost_swap,
        cap_hits=cap_hits, atr=atr, capital=capital)


# =========================================================================
# Reporting
# =========================================================================
def stats(res, df):
    eq = pd.Series(res["equity"], index=pd.DatetimeIndex(res["time"])).ffill().dropna()
    cap = res["capital"]
    tr = res["trades"]
    peak = eq.cummax()
    dd = (peak - eq) / peak * 100.0
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    bars_per_year = 6 * 252.0

    net = eq.iloc[-1] - cap
    out = {
        "period": "%s .. %s" % (eq.index[0].date(), eq.index[-1].date()),
        "years": round(years, 2),
        "initial_capital": cap,
        "final_equity": float(eq.iloc[-1]),
        "net_profit": float(net),
        "return_pct": float(net / cap * 100.0),
        "cagr_pct": float(((eq.iloc[-1] / cap) ** (1.0 / years) - 1.0) * 100.0) if years > 0 else 0.0,
        "max_dd_pct": float(dd.max()),
        "max_dd_money": float((peak - eq).max()),
        "sharpe": float(ret.mean() / ret.std() * np.sqrt(bars_per_year)) if ret.std() else 0.0,
        "cost_commission": float(res["cost_comm"]),
        "cost_spread_slip": float(res["cost_spread"]),
        "cost_swap": float(res["cost_swap"]),
        "notional_cap_hits": int(res["cap_hits"]),
        "max_abs_lots": float(np.nanmax(np.abs(res["position_lots"]))),
        "pct_bars_in_market": float(100.0 * np.mean(np.abs(res["position_lots"]) > 0)),
    }
    if years > 0 and out["max_dd_pct"] > 0:
        out["calmar"] = out["cagr_pct"] / out["max_dd_pct"]

    if len(tr):
        g = tr["gross"]
        wins, losses = g[g > 0], g[g < 0]
        out.update({
            "sleeve_trades": int(len(tr)),
            "sleeve_win_rate": float(100.0 * len(wins) / len(g)),
            "sleeve_profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) else float("inf"),
            "sleeve_gross_sum": float(g.sum()),
            "avg_R": float(tr["r_multiple"].mean()),
            "best_R": float(tr["r_multiple"].max()),
            "worst_R": float(tr["r_multiple"].min()),
            "stop_exits": int((tr["reason"] == "stop").sum()),
            "channel_exits": int((tr["reason"] == "channel").sum()),
        })
    return out, eq


def per_year(eq, tr):
    rows = []
    for y, grp in eq.groupby(eq.index.year):
        start = eq.loc[:grp.index[0]].iloc[-2] if grp.index[0] != eq.index[0] else grp.iloc[0]
        pk = grp.cummax()
        rows.append(dict(year=int(y), start_eq=float(start), end_eq=float(grp.iloc[-1]),
                         pnl=float(grp.iloc[-1] - start),
                         ret_pct=float((grp.iloc[-1] / start - 1) * 100.0),
                         max_dd_pct=float(((pk - grp) / pk * 100.0).max()),
                         trades=int((pd.DatetimeIndex(tr["exit_time"]).year == y).sum()) if len(tr) else 0))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--risk", type=float, default=RISK_PCT)
    ap.add_argument("--no-swap", action="store_true")
    ap.add_argument("--no-costs", action="store_true")
    ap.add_argument("--no-friday", action="store_true")
    ap.add_argument("--sleeves", default="fast,medium,slow")
    ap.add_argument("--max-notional", type=float, default=MAX_NOTIONAL_X)
    ap.add_argument("--lot-step", type=float, default=LOT_STEP)
    ap.add_argument("--tag", default="")
    ap.add_argument("--lookahead", action="store_true",
                    help="AUDIT: let the Donchian see the current bar")
    ap.add_argument("--same-bar-fill", action="store_true",
                    help="AUDIT: fill at the signal bar's close")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    df = datamod.load_rates(SYMBOL, TIMEFRAME, DATE_FROM, DATE_TO,
                            warmup_bars=WARMUP_BARS, refresh=False,
                            terminal_path=config.MT5_EXE)
    res = run(df, a.capital, a.risk, not a.no_swap, not a.no_costs,
              set(s.strip() for s in a.sleeves.split(",")),
              a.max_notional, a.lot_step, not a.no_friday,
              a.lookahead, a.same_bar_fill)
    st, eq = stats(res, df)

    tag = a.tag or "base"
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    base = os.path.join(config.RESULTS_DIR, "%s_%s" % (NAME, tag))
    res["trades"].to_csv(base + "_trades.csv", index=False, encoding="utf-8")
    eq.to_frame("equity").to_csv(base + "_equity.csv", encoding="utf-8")
    with open(base + "_stats.json", "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, default=float)

    if not a.quiet:
        print("== XAU Multi-Speed Volatility-Scaled Donchian  [%s] ==" % tag)
        print("   sleeves=%s risk=%.2f%%/sleeve capital=%s swap=%s costs=%s"
              % (a.sleeves, a.risk, int(a.capital),
                 "off" if a.no_swap else "on", "off" if a.no_costs else "on"))
        for k, v in st.items():
            print("   %-22s %s" % (k, ("%.4f" % v) if isinstance(v, float) else v))
        if len(res["trades"]):
            print("\n   -- per sleeve --")
            g = res["trades"].groupby("sleeve")
            print(g.agg(trades=("gross", "size"), gross=("gross", "sum"),
                        win_rate=("gross", lambda s: 100.0 * (s > 0).mean()),
                        avg_R=("r_multiple", "mean")).round(2).to_string())
            print("\n   -- per year --")
            print(per_year(eq, res["trades"]).round(2).to_string(index=False))
        print("\n   saved: %s_{trades,equity,stats}.*" % base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
