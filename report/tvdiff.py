# -*- coding: utf-8 -*-
"""Trade-by-trade diff between the Python engine and a TradingView export.

TradingView's "List of Trades" export stamps its rows in the chart's timezone
(UTC for this account) while the Python engine and the EA both run on broker
server time, so the two are joined through the same EET/EEST rule that
core/timeutil.py applies everywhere else - a naive fixed offset silently
mismatches half the year.

What is being looked for is not "do the numbers differ" - two different price
feeds guarantee that - but WHICH trades differ enough to matter and why.  The
classification is deliberately about causes the two engines can disagree on:

    SPREAD      entry differs by about the spread; Python fills a long at
                open+spread, Pine at the raw open
    TRAIL       one side's trailing stop armed and the other's did not, so
                the position was closed at a completely different level
    EXIT_BAR    both trailed, but the stop sat at a different level and the
                bar that touched it differs
    FEED        prices agree in kind but differ by feed noise
    PY_ONLY /   the trade exists on one side only
    TV_ONLY

Run:  python -m report.tvdiff <tv_export.csv> [strategy_name]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from core import config
from core.timeutil import eu_dst_active


def utc_to_server(utc_times, winter_offset_hours=2):
    """Inverse of core.timeutil.server_to_utc, same EU summer-time rule.

    The forward function decides DST from ``server - winter_offset``; here the
    UTC stamp itself is used. The two disagree only inside the one ambiguous
    hour a year that module already documents.

    Returns a NumPy array, never a Series: the caller assigns this onto a frame
    whose index is TradingView's trade number, and a Series carrying its own
    RangeIndex would be aligned against that index rather than positionally -
    silently handing every row the NEXT trade's timestamp.
    """
    ts = pd.to_datetime(pd.Series(np.asarray(utc_times))).reset_index(drop=True)
    extra = eu_dst_active(ts).astype(int)
    out = (ts + pd.Timedelta(hours=int(winter_offset_hours))
           + pd.to_timedelta(extra, unit="h"))

    # The offset is 2h or 3h and nothing else. Anything past that means the
    # values got shuffled, which is not detectable downstream - the join still
    # succeeds, just against the wrong trades.
    delta = (out - ts).dt.total_seconds() / 3600.0
    bad = ~delta.isin([2.0, 3.0])
    if bad.any():
        raise ValueError("utc_to_server produced %d offsets outside {2h, 3h}: %s"
                         % (int(bad.sum()), sorted(delta[bad].unique())[:5]))
    return out.to_numpy()


def load_tv(path):
    """Fold TradingView's two-row-per-trade export into one row per trade."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["Date and time"] = pd.to_datetime(df["Date and time"])
    ent = df[df["Type"].str.startswith("Entry")].set_index("Trade number")
    ext = df[df["Type"].str.startswith("Exit")].set_index("Trade number")
    tv = ent[["Date and time", "Price USD", "Size (qty)", "Type"]].join(
        ext[["Date and time", "Price USD", "Signal"]], rsuffix="_x", how="inner")
    tv.columns = ["t_in", "p_in", "oz", "side", "t_out", "p_out", "reason"]
    # Drop the trade-number index before deriving anything: leaving it in place
    # is what makes a positional assignment silently align by trade number.
    tv = tv.reset_index(drop=True)
    tv["dir"] = np.where(tv["side"].str.contains("long"), "long", "short")
    tv["t_in_s"] = utc_to_server(tv["t_in"])
    tv["t_out_s"] = utc_to_server(tv["t_out"])
    tv["pts"] = np.where(tv["dir"] == "long",
                         tv["p_out"] - tv["p_in"], tv["p_in"] - tv["p_out"])
    tv["usd"] = tv["pts"] * tv["oz"]
    return tv


def classify(r, spread_price):
    """Why this pair differs, most specific cause first."""
    if r["_merge"] == "left_only":
        return "PY_ONLY"
    if r["_merge"] == "right_only":
        return "TV_ONLY"
    py_tr, tv_tr = bool(r["trailed"]), (r["reason"] == "TSL")
    if py_tr != tv_tr:
        return "TRAIL"
    if r["d_exit_bars"] != 0:
        return "EXIT_BAR"
    if abs(r["d_pts"]) <= 2.5 * spread_price:
        return "SPREAD" if abs(r["d_in"]) > abs(r["d_out"]) else "FEED"
    return "FEED"


def run(tv_path, name="ScalpGoldM1", top=25, spread_price=0.15):
    py = pd.read_csv(os.path.join(config.RESULTS_DIR, "%s_py_trades.csv" % name),
                     parse_dates=["entry_time", "exit_time"])
    tv = load_tv(tv_path)

    # Compare only where both runs actually cover the same calendar.
    lo = max(py["entry_time"].min(), tv["t_in_s"].min())
    hi = min(py["entry_time"].max(), tv["t_in_s"].max())
    py = py[(py["entry_time"] >= lo) & (py["entry_time"] <= hi)].copy()
    tv = tv[(tv["t_in_s"] >= lo) & (tv["t_in_s"] <= hi)].copy()

    m = py.merge(tv, left_on="entry_time", right_on="t_in_s",
                 how="outer", indicator=True)
    m["oz_py"] = (m["lots"] * 100).round()
    m["d_in"] = m["entry_price"] - m["p_in"]
    m["d_out"] = m["exit_price"] - m["p_out"]
    m["d_pts"] = m["pts"] - m["points"]                 # TV minus Python
    m["d_usd"] = m["usd"] - m["pnl_money"]
    m["d_exit_bars"] = (m["t_out_s"] - m["exit_time"]).dt.total_seconds() / 60.0
    m["cause"] = m.apply(lambda r: classify(r, spread_price), axis=1)
    return m, lo, hi


def _fmt(m, rows):
    out = []
    for _, r in rows.iterrows():
        out.append(
            "  %s  %-5s %2.0f oz   py %8.2f -> %8.2f  %-4s%s\n"
            "  %s               tv %8.2f -> %8.2f  %-4s\n"
            "                            exit  py %s   tv %s  (%+.0f min)\n"
            "                            move  py %+7.2f   tv %+7.2f   diff %+7.2f USD/oz  [%s]"
            % (r["entry_time"].strftime("%Y-%m-%d %H:%M"), r["direction"], r["oz_py"],
               r["entry_price"], r["exit_price"], r["exit_reason"],
               " (trailed)" if r["trailed"] else "",
               " " * 16, r["p_in"], r["p_out"], r["reason"],
               r["exit_time"].strftime("%m-%d %H:%M"), r["t_out_s"].strftime("%m-%d %H:%M"),
               r["d_exit_bars"], r["points"], r["pts"], r["d_pts"], r["cause"]))
    return "\n\n".join(out)


def main(argv):
    tv_path = argv[0]
    name = argv[1] if len(argv) > 1 else "ScalpGoldM1"
    m, lo, hi = run(tv_path, name)

    both = m[m["_merge"] == "both"]
    print("=" * 74)
    print("  PYTHON vs TRADINGVIEW   %s" % name)
    print("  overlap %s .. %s" % (lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")))
    print("=" * 74)
    print("  python trades %4d      tv trades %4d      matched entry bar %4d (%.1f%%)"
          % ((m["_merge"] != "right_only").sum(), (m["_merge"] != "left_only").sum(),
             len(both), 100.0 * len(both) / max(1, (m["_merge"] != "right_only").sum())))
    print()
    print("-- why they differ ---------------------------------------------------")
    tot = m["d_usd"].fillna(0).abs().sum()
    for cause, grp in sorted(m.groupby("cause"), key=lambda kv: -kv[1]["d_usd"].abs().sum()):
        share = 100.0 * grp["d_usd"].abs().sum() / tot if tot else 0.0
        print("  %-9s n=%4d   |money diff| %8.2f USD  (%5.1f%% of all divergence)"
              % (cause, len(grp), grp["d_usd"].abs().sum(), share))
    print()
    print("-- totals ------------------------------------------------------------")
    print("  price movement   python %+9.2f      tv %+9.2f      diff %+9.2f USD/oz"
          % (both["points"].sum(), both["pts"].sum(),
             both["pts"].sum() - both["points"].sum()))
    print("  gross money      python %+9.2f      tv %+9.2f      diff %+9.2f USD"
          % (both["pnl_money"].sum(), both["usd"].sum(),
             both["usd"].sum() - both["pnl_money"].sum()))
    print()
    print("-- %d largest divergences (TV minus Python, in USD) -------------------"
          % 25)
    big = both.reindex(both["d_usd"].abs().sort_values(ascending=False).index).head(25)
    print(_fmt(m, big))

    out = os.path.join(config.RESULTS_DIR, "%s_tvdiff.csv" % name)
    cols = ["entry_time", "direction", "oz_py", "oz", "entry_price", "p_in", "d_in",
            "exit_time", "t_out_s", "d_exit_bars", "exit_price", "p_out", "d_out",
            "exit_reason", "reason", "trailed", "points", "pts", "d_pts",
            "pnl_money", "usd", "d_usd", "cause", "_merge"]
    m.sort_values("entry_time")[cols].to_csv(out, index=False, encoding="utf-8")
    print("\n\nfull comparison saved: %s" % out)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main(sys.argv[1:]))
