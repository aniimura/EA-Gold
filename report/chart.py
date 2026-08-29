# -*- coding: utf-8 -*-
"""The Strategy Tester "Performance" panel, drawn from the Python backtest.

TradingView shows four things stacked on one time axis: the cumulative P/L
curve, each trade's run-up and drawdown, and a win/loss strip.  The same four
are drawn here from ``results/<name>_py_trades.csv`` so a Python-only iteration
can be read the same way as a TradingView run.

Run-up and drawdown are not in the trade CSV - they are the best and worst
prices the position ever saw, which only the bar data knows.  They are
recomputed here from ``results/<name>_py_bars.pkl`` (written by ``pybt
--bars``).  Without that file the panel still renders, minus those two rows.

Two curves are drawn, not one.  ``net`` is what the account would show;
``gross`` prices movement alone.  Keeping both visible is the same discipline
the reconciler applies in report section 2b - when the two diverge sharply the
edge is being eaten by costs, and that is worth seeing on every chart rather
than discovering once at reconciliation time.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")                                    # noqa: E402
import matplotlib.dates as mdates                        # noqa: E402
import matplotlib.pyplot as plt                          # noqa: E402

from core import config

# TradingView's palette, so the two panels can be read side by side.
GREEN = "#089981"
RED = "#f23645"
GREY = "#787b86"
GRID = "#e0e3eb"


def _excursions(trades, bars, contract_size):
    """Best and worst open P/L of each trade, in account currency.

    Indexed by time rather than by bar number: the trade CSV counts bars from
    the start of the loaded history (warmup included) while the bar frame may
    have been sliced, and a silent off-by-N here would misattribute every
    excursion.
    """
    if bars is None:
        return None, None
    b = bars.set_index(pd.to_datetime(bars["time"]))
    run_up, draw = [], []
    for t in trades.itertuples():
        w = b.loc[t.entry_time:t.exit_time]
        if len(w) == 0:
            run_up.append(np.nan)
            draw.append(np.nan)
            continue
        mpp = contract_size * t.lots                     # money per price unit
        hi, lo = float(w["high"].max()), float(w["low"].min())
        if t.direction == "long":
            run_up.append((hi - t.entry_price) * mpp)
            draw.append((lo - t.entry_price) * mpp)
        else:
            run_up.append((t.entry_price - lo) * mpp)
            draw.append((t.entry_price - hi) * mpp)
    return np.array(run_up), np.array(draw)


def _subtitle(name, trades, net, gross):
    """One line of headline figures.

    Read from ``<name>_py_stats.json`` when it is there, so the chart can never
    print a different number than the ``pybt`` run that produced it; recomputed
    only as a fallback.
    """
    stats = {}
    p = os.path.join(config.RESULTS_DIR, "%s_py_stats.json" % name)
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            stats = json.load(fh)

    def pick(key, fallback):
        v = stats.get(key)
        return float(v) if isinstance(v, (int, float)) else fallback

    wins, losses = net[net > 0], net[net < 0]
    eq = np.concatenate(([0.0], np.cumsum(net)))
    return "  ·  ".join([
        "%d trades" % len(trades),
        "win %.2f%%" % pick("win_rate", 100.0 * (net > 0).mean() if len(net) else 0.0),
        "PF %.2f" % pick("profit_factor",
                         wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")),
        "net %+.2f" % pick("net_profit", net.sum()),
        "gross %+.2f" % pick("gross_profit", gross.sum()),
        "max DD %.2f" % pick("max_dd", (np.maximum.accumulate(eq) - eq).max()),
    ])


def render(strategy, out_path=None):
    """Draw the panel for ``strategy`` and return the path written."""
    name = strategy.name
    tr_path = os.path.join(config.RESULTS_DIR, "%s_py_trades.csv" % name)
    if not os.path.isfile(tr_path):
        raise IOError("no Python result yet - run `pybt` first (%s)" % tr_path)
    trades = pd.read_csv(tr_path, parse_dates=["entry_time", "exit_time"])
    if trades.empty:
        raise ValueError("%s produced no trades - nothing to plot" % name)

    bars_path = os.path.join(config.RESULTS_DIR, "%s_py_bars.pkl" % name)
    bars = pd.read_pickle(bars_path) if os.path.isfile(bars_path) else None

    sym_path = os.path.join(config.DATA_DIR, "%s_syminfo.json" % name)
    contract = 100.0
    if os.path.isfile(sym_path):
        with open(sym_path, "r", encoding="utf-8") as fh:
            contract = float(json.load(fh)["contract_size"])

    # Plotted against exit time, so the curve steps where the money is booked.
    trades = trades.sort_values("exit_time").reset_index(drop=True)
    t = trades["exit_time"]
    net = trades["net_money"].to_numpy(dtype=float)
    gross = trades["pnl_money"].to_numpy(dtype=float)
    cum_net, cum_gross = np.cumsum(net), np.cumsum(gross)
    run_up, draw = _excursions(trades, bars, contract)

    rows = 3 if run_up is not None else 2
    heights = [4.0, 1.3, 0.30] if rows == 3 else [4.0, 0.30]
    fig, axes = plt.subplots(
        rows, 1, figsize=(13.5, 7.2), sharex=True,
        gridspec_kw={"height_ratios": heights, "hspace": 0.12})
    fig.patch.set_facecolor("white")

    # ---- cumulative P/L -------------------------------------------------
    ax = axes[0]
    ax.axhline(0.0, color=GREY, lw=0.8, alpha=0.6)
    ax.plot(t, cum_gross, color=GREY, lw=1.0, ls="--", alpha=0.75,
            label="Cumulative P/L (gross, price only)")
    ax.plot(t, cum_net, color=GREEN, lw=1.6, label="Cumulative P/L (net)")
    ax.fill_between(t, 0.0, cum_net, color=GREEN, alpha=0.12)
    ax.set_ylabel("P/L (%s)" % strategy.currency)
    ax.set_title("%s   %s %s   %s .. %s\n%s"
                 % (name, strategy.symbol, strategy.timeframe,
                    strategy.date_from, strategy.date_to,
                    _subtitle(name, trades, net, gross)),
                 loc="left", fontsize=11, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)

    # The closing value, badged on the axis the way the tester badges it.
    final = float(cum_net[-1])
    ax.annotate(" {:+,.2f} ".format(final), xy=(t.iloc[-1], final),
                xytext=(6, 0), textcoords="offset points",
                va="center", fontsize=9, color="white", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc=GREEN if final >= 0 else RED,
                          ec="none"))

    # ---- run-ups and drawdowns -----------------------------------------
    if run_up is not None:
        ax = axes[1]
        ax.axhline(0.0, color=GREY, lw=0.8, alpha=0.6)
        ax.vlines(t, 0.0, run_up, color=GREEN, lw=1.0, alpha=0.85)
        ax.vlines(t, draw, 0.0, color=RED, lw=1.0, alpha=0.85)
        ax.set_ylabel("run-up /\ndrawdown", fontsize=9)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)

    # ---- win / loss strip ----------------------------------------------
    ax = axes[-1]
    ax.bar(t, np.ones(len(t)), width=0.0016 * max(1.0, (t.iloc[-1] - t.iloc[0]).days),
           color=np.where(net > 0, GREEN, RED), align="center")
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_ylabel("W/L", fontsize=9)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

    out = out_path or os.path.join(config.RESULTS_DIR, "%s_performance.png" % name)
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    fig.savefig(out, dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out
