# -*- coding: utf-8 -*-
"""One row per M1 bar, carrying both engines' bars, indicators and trades.

The reconciler answers "do they agree"; this answers "what was each engine
holding at 19:43 on the 14th". Everything is laid on the bar grid so a
divergence can be read forwards from the bar it started on rather than
inferred from two trade lists.

Columns come in five blocks:

    bar         time (broker server time), time_utc, OHLC, spread
    indicator   whatever the spec computed, plus the two signal columns
    py_*        the Python engine's entries, exits and position state
    mt5_*       the same, read from the CSV the generated EA wrote
    tv_*        the same from a TradingView "List of Trades" export

The MT5 block is absent when the tester has not been run for this spec, so the
frame is still usable from a Python-only iteration.

An entry and an exit can land on the same bar - a stop-out on the fill bar is
normal here - so entries and exits get separate columns rather than one event
column that would have to drop one of them.

``*_in_pos`` marks every bar a position was open, not just its ends, which is
what makes "they disagreed about being in the market at all" visible as a
contiguous run instead of two unmatched trade rows.

Run:  python -m report.timeline <tv_export.csv> [strategy_name]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from core import config
from core.timeutil import server_to_utc
from report.tvdiff import load_tv


def _mark_positions(index, opens, closes):
    """1 on every bar between each entry and its exit, inclusive."""
    held = np.zeros(len(index), dtype=np.int8)
    pos = pd.Series(np.arange(len(index)), index=index)
    for t0, t1 in zip(opens, closes):
        if pd.isna(t0) or pd.isna(t1):
            continue
        i0 = pos.get(t0, None)
        i1 = pos.get(t1, None)
        if i0 is None or i1 is None:
            continue
        held[int(i0):int(i1) + 1] = 1
    return held


def build(tv_path, name="ScalpGoldM1TV"):
    bars = pd.read_pickle(os.path.join(config.RESULTS_DIR, "%s_py_bars.pkl" % name))
    bars["time"] = pd.to_datetime(bars["time"])
    bars = bars.sort_values("time").drop_duplicates("time").reset_index(drop=True)

    df = bars.copy()
    df.insert(1, "time_utc", server_to_utc(df["time"]).to_numpy())

    py = pd.read_csv(os.path.join(config.RESULTS_DIR, "%s_py_trades.csv" % name),
                     parse_dates=["entry_time", "exit_time"])
    tv = load_tv(tv_path)

    # ---- Python side -----------------------------------------------------
    ent = py.set_index("entry_time")
    ext = py.set_index("exit_time")
    df["py_trade"] = df["time"].map(ent["idx"]).astype("Int64")
    df["py_entry"] = df["time"].map(ent["direction"])
    df["py_entry_price"] = df["time"].map(ent["entry_price"])
    df["py_sl"] = df["time"].map(ent["sl"])
    df["py_lots"] = df["time"].map(ent["lots"])
    df["py_exit_of"] = df["time"].map(ext["idx"]).astype("Int64")
    df["py_exit"] = df["time"].map(ext["exit_reason"])
    df["py_exit_price"] = df["time"].map(ext["exit_price"])
    df["py_trailed"] = df["time"].map(ext["trailed"])
    df["py_points"] = df["time"].map(ext["points"])
    df["py_net"] = df["time"].map(ext["net_money"])
    df["py_in_pos"] = _mark_positions(df["time"], py["entry_time"], py["exit_time"])

    # ---- MT5 side, when the tester has been run --------------------------
    mt5_path = os.path.join(config.RESULTS_DIR, "%s_mt5_trades.csv" % name)
    if os.path.isfile(mt5_path):
        m5 = pd.read_csv(mt5_path)
        for c in ("entry_time", "exit_time"):
            m5[c] = pd.to_datetime(m5[c], format="%Y.%m.%d %H:%M:%S", errors="coerce")
        # The EA stamps an exit with the tick's own time, which need not be a
        # bar boundary; floor it so it lands on the M1 grid the frame is on.
        m5["exit_bar_time"] = m5["exit_time"].dt.floor("min")
        m5["points"] = np.where(m5["direction"] == "long",
                                m5["exit_price"] - m5["entry_price"],
                                m5["entry_price"] - m5["exit_price"])
        me = m5.set_index("entry_time")
        mx = m5.set_index("exit_bar_time")
        df["mt5_trade"] = df["time"].map(me["idx"]).astype("Int64")
        df["mt5_entry"] = df["time"].map(me["direction"])
        df["mt5_entry_price"] = df["time"].map(me["entry_price"])
        df["mt5_sl"] = df["time"].map(me["sl"])
        df["mt5_lots"] = df["time"].map(me["lots"])
        df["mt5_exit_of"] = df["time"].map(mx["idx"]).astype("Int64")
        df["mt5_exit"] = df["time"].map(mx["exit_reason"])
        df["mt5_exit_price"] = df["time"].map(mx["exit_price"])
        df["mt5_exit_time"] = df["time"].map(mx["exit_time"])
        df["mt5_points"] = df["time"].map(mx["points"])
        df["mt5_in_pos"] = _mark_positions(df["time"], m5["entry_time"],
                                           m5["exit_bar_time"])

    # ---- TradingView side ------------------------------------------------
    tv = tv.reset_index(drop=True)
    tv["idx"] = np.arange(1, len(tv) + 1)
    tent = tv.set_index("t_in_s")
    text = tv.set_index("t_out_s")
    df["tv_trade"] = df["time"].map(tent["idx"]).astype("Int64")
    df["tv_entry"] = df["time"].map(tent["dir"])
    df["tv_entry_price"] = df["time"].map(tent["p_in"])
    df["tv_qty"] = df["time"].map(tent["oz"])
    df["tv_exit_of"] = df["time"].map(text["idx"]).astype("Int64")
    df["tv_exit"] = df["time"].map(text["reason"])
    df["tv_exit_price"] = df["time"].map(text["p_out"])
    df["tv_points"] = df["time"].map(text["pts"])
    df["tv_in_pos"] = _mark_positions(df["time"], tv["t_in_s"], tv["t_out_s"])

    # ---- side by side ----------------------------------------------------
    df["d_entry_price"] = df["py_entry_price"] - df["tv_entry_price"]
    df["d_exit_price"] = df["py_exit_price"] - df["tv_exit_price"]
    df["d_points"] = df["tv_points"] - df["py_points"]
    df["both_entry"] = df["py_entry"].notna() & df["tv_entry"].notna()
    df["both_exit"] = df["py_exit"].notna() & df["tv_exit"].notna()
    df["pos_disagree"] = (df["py_in_pos"] != df["tv_in_pos"]).astype(np.int8)
    if "mt5_entry" in df.columns:
        df["d_entry_price_mt5"] = df["py_entry_price"] - df["mt5_entry_price"]
        df["d_exit_price_mt5"] = df["py_exit_price"] - df["mt5_exit_price"]
        df["all3_entry"] = df["both_entry"] & df["mt5_entry"].notna()
    return df


def main(argv):
    tv_path = argv[0]
    name = argv[1] if len(argv) > 1 else "ScalpGoldM1TV"
    df = build(tv_path, name)

    # Clip to the window both runs actually cover, so leading warmup bars and
    # trailing padding do not read as "TradingView took no trades here".
    lo = min(df.loc[df["py_entry"].notna(), "time"].min(),
             df.loc[df["tv_entry"].notna(), "time"].min())
    hi = max(df.loc[df["py_exit"].notna(), "time"].max(),
             df.loc[df["tv_exit"].notna(), "time"].max())
    win = df[(df["time"] >= lo) & (df["time"] <= hi)].copy()

    full = os.path.join(config.RESULTS_DIR, "%s_timeline.csv" % name)
    win.to_csv(full, index=False, encoding="utf-8", float_format="%.5f")

    mask = (win["py_entry"].notna() | win["py_exit"].notna()
            | win["tv_entry"].notna() | win["tv_exit"].notna())
    if "mt5_entry" in win.columns:
        mask |= win["mt5_entry"].notna() | win["mt5_exit"].notna()
    ev = win[mask]
    small = os.path.join(config.RESULTS_DIR, "%s_timeline_events.csv" % name)
    ev.to_csv(small, index=False, encoding="utf-8", float_format="%.5f")

    print("== timeline ==")
    print("  window      : %s .. %s" % (lo, hi))
    print("  bars        : %d" % len(win))
    print("  py trades   : %d entries / %d exits"
          % (win["py_entry"].notna().sum(), win["py_exit"].notna().sum()))
    if "mt5_entry" in win.columns:
        print("  mt5 trades  : %d entries / %d exits"
              % (win["mt5_entry"].notna().sum(), win["mt5_exit"].notna().sum()))
    print("  tv trades   : %d entries / %d exits"
          % (win["tv_entry"].notna().sum(), win["tv_exit"].notna().sum()))
    print("  py+tv same entry bar : %d      same exit bar : %d"
          % (win["both_entry"].sum(), win["both_exit"].sum()))
    if "mt5_entry" in win.columns:
        print("  py+mt5 same entry bar: %d"
              % (win["py_entry"].notna() & win["mt5_entry"].notna()).sum())
        print("  all three same entry : %d" % win["all3_entry"].sum())
    print("  bars where only one side held a position : %d of %d (%.1f%%)"
          % (win["pos_disagree"].sum(), len(win),
             100.0 * win["pos_disagree"].mean()))
    print()
    print("  full   : %s  (%d rows, %.1f MB)"
          % (full, len(win), os.path.getsize(full) / 1048576.0))
    print("  events : %s  (%d rows, %.1f MB)"
          % (small, len(ev), os.path.getsize(small) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main(sys.argv[1:]))
