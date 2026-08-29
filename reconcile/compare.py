# -*- coding: utf-8 -*-
"""Reconcile the Python backtest against the MT5 Strategy Tester run.

Two levels, because they fail for different reasons:

  bars   - every indicator value, bar by bar.  A mismatch here means the
           formula was translated wrongly (or a look-ahead crept in) and no
           amount of trade-level tweaking will fix it.  Check this FIRST.
  trades - entry bar, entry price, SL/TP, exit reason and exit price.

Differences are classified rather than merely counted, because the honest
answer is not always "the code is wrong":

  LOGIC      the two engines disagreed about whether to trade at all
  AMBIGUOUS  SL and TP were both inside one bar; OHLC cannot say which came
             first, so a different choice is expected, not a bug
  SPREAD     entry/exit prices differ by roughly the spread
  ROUNDING   sub-point differences from NormalizeDouble / tick size
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from core import config
from core.types import tf_seconds

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


# --------------------------------------------------------------------------
def _floor_to_bar(series, tf):
    s = pd.to_datetime(series)
    secs = tf_seconds(tf)
    return (s.astype("int64") // 10 ** 9 // secs * secs).astype("int64")


def _count_rollovers(trades, triple_weekday=2, weight=None):
    """Weighted overnight rollovers, so an implied swap rate can be derived.

    ``triple_weekday`` is pandas' Wednesday (=2); brokers usually charge three
    days of swap on the Wednesday rollover.  Pass ``weight`` (per-trade lots)
    to get LOT-nights rather than plain nights.
    """
    try:
        ent = pd.to_datetime(trades["entry_time"])
        ext = pd.to_datetime(trades["exit_time"])
    except Exception:
        return 0
    w = (weight.to_numpy(dtype=float) if weight is not None
         else np.ones(len(ent), dtype=float))
    total = 0.0
    for k, (a, b) in enumerate(zip(ent, ext)):
        if pd.isna(a) or pd.isna(b):
            continue
        d = a.normalize()
        end = b.normalize()
        n = 0
        while d < end:
            d = d + pd.Timedelta(days=1)
            n += 3 if d.dayofweek == triple_weekday else 1
        total += n * (w[k] if np.isfinite(w[k]) else 1.0)
    return total


def reconcile_bars(py_bars, mt5_bars, indicator_names, rel_tol=1e-6,
                   abs_tol=1e-9):
    # abs_tol defaults to 1e-9 because the EA writes indicator values with
    # DoubleToString(v, 10) - anything below that is CSV quantisation, not a
    # translation error.  A value that hovers around zero (a difference or a
    # ratio) otherwise shows a huge RELATIVE error from a meaningless absolute
    # one, which is why both tests must fail before a bar counts as bad.
    """Compare indicator values bar by bar.  Returns (summary_df, stats)."""
    if py_bars is None or mt5_bars is None or len(mt5_bars) == 0:
        return None, {"status": "skipped", "reason": "no bar CSV"}

    p = py_bars.copy()
    m = mt5_bars.copy()
    p["_k"] = pd.to_datetime(p["time"]).astype("int64")
    m["_k"] = pd.to_datetime(m["time"]).astype("int64")
    merged = p.merge(m, on="_k", suffixes=("_py", "_mt5"))
    if merged.empty:
        return None, {"status": "no overlap",
                      "py_range": (str(p["time"].min()), str(p["time"].max())),
                      "mt5_range": (str(m["time"].min()), str(m["time"].max()))}

    rows = []
    worst = 0.0
    # Compare the entry signals too, not only the indicator values: a filter
    # that reads the clock, or a higher-timeframe value, can be identical
    # bar-for-bar yet still fire on different bars.
    for nm in list(indicator_names) + ["sig_long", "sig_short"]:
        a, b = nm + "_py", nm + "_mt5"
        if a not in merged.columns or b not in merged.columns:
            continue
        va = pd.to_numeric(merged[a], errors="coerce").to_numpy(dtype=float)
        vb = pd.to_numeric(merged[b], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(va) & np.isfinite(vb)
        if ok.sum() == 0:
            rows.append({"indicator": nm, "n": 0, "max_abs_diff": np.nan,
                         "max_rel_diff": np.nan, "n_bad": 0, "verdict": "NO DATA"})
            continue
        d = np.abs(va[ok] - vb[ok])
        scale = np.maximum(np.abs(va[ok]), np.abs(vb[ok]))
        rel = np.where(scale > 0, d / scale, 0.0)
        bad = int(((rel > rel_tol) & (d > abs_tol)).sum())
        worst = max(worst, float(rel.max()))
        rows.append({
            "indicator": nm,
            "n": int(ok.sum()),
            "max_abs_diff": float(d.max()),
            "max_rel_diff": float(rel.max()),
            "n_bad": bad,
            "verdict": "MATCH" if bad == 0 else "DIVERGES",
        })

    summary = pd.DataFrame(rows)
    n_bad_ind = int((summary["verdict"] == "DIVERGES").sum()) if len(summary) else 0
    stats = {
        "status": PASS if n_bad_ind == 0 else FAIL,
        "bars_compared": int(len(merged)),
        "indicators": int(len(summary)),
        "indicators_diverging": n_bad_ind,
        "worst_rel_diff": worst,
    }
    return summary, stats


# --------------------------------------------------------------------------
def reconcile_trades(py_trades, mt5_trades, timeframe, point,
                     price_tol_points=1.0, spread_points=None,
                     contract_size=100000.0, lot=0.01):
    """Match trades one to one and classify every difference."""
    p = py_trades.copy()
    m = mt5_trades.copy()
    if len(p) == 0 and len(m) == 0:
        return pd.DataFrame(), {"status": WARN, "reason": "no trades on either side",
                                "py_trades": 0, "mt5_trades": 0}

    p["_k"] = _floor_to_bar(p["entry_time"], timeframe)
    m["_k"] = _floor_to_bar(m["entry_time"], timeframe)

    p = p.drop_duplicates("_k", keep="first")
    m = m.drop_duplicates("_k", keep="first")

    merged = p.merge(m, on="_k", how="outer", suffixes=("_py", "_mt5"),
                     indicator=True)
    merged = merged.sort_values("_k").reset_index(drop=True)

    tol = price_tol_points * point
    out = []
    for _, r in merged.iterrows():
        side = r["_merge"]
        rec = {
            "bar_time": pd.to_datetime(r["_k"], unit="s"),
            "in_py": side in ("both", "left_only"),
            "in_mt5": side in ("both", "right_only"),
        }
        if side == "left_only":
            rec.update({"class": "LOGIC", "detail": "python-only trade",
                        "direction": r.get("direction_py", r.get("direction"))})
            out.append(rec)
            continue
        if side == "right_only":
            rec.update({"class": "LOGIC", "detail": "MT5-only trade",
                        "direction": r.get("direction_mt5", r.get("direction"))})
            out.append(rec)
            continue

        d_entry = float(r["entry_price_mt5"]) - float(r["entry_price_py"])
        d_sl = float(r["sl_mt5"]) - float(r["sl_py"]) if r["sl_py"] else 0.0
        d_tp = float(r["tp_mt5"]) - float(r["tp_py"]) if r["tp_py"] else 0.0
        d_exit = float(r["exit_price_mt5"]) - float(r["exit_price_py"])
        same_reason = str(r["exit_reason_py"]) == str(r["exit_reason_mt5"])
        ambiguous = bool(r.get("ambiguous", False))

        if same_reason and str(r["exit_reason_py"]) == "EOD":
            # Both sides closed the last open position when the run ended;
            # the closing price is whatever each side had last, not a decision.
            klass = "EOD"
            detail = "forced close at end of run (%+.1fp)" % (d_exit / point)
        elif not same_reason:
            klass = "AMBIGUOUS" if ambiguous else "LOGIC"
            detail = "exit %s (py) vs %s (mt5)" % (r["exit_reason_py"], r["exit_reason_mt5"])
        elif abs(d_entry) > tol or abs(d_exit) > tol:
            spr = (spread_points or 0.0) * point
            near_spread = spr > 0 and abs(abs(d_entry) - spr) < max(tol, 0.5 * spr)
            klass = "SPREAD" if near_spread else "ROUNDING"
            detail = "entry %+.1fp exit %+.1fp" % (d_entry / point, d_exit / point)
        elif abs(d_sl) > tol or abs(d_tp) > tol:
            on_edge = str(r.get("note", "")) == "boundary"
            within_one = max(abs(d_sl), abs(d_tp)) <= 1.5 * point
            klass = "BOUNDARY" if (on_edge and within_one) else "ROUNDING"
            detail = "SL %+.1fp TP %+.1fp" % (d_sl / point, d_tp / point)
            if klass == "BOUNDARY":
                detail += "  (stop sat on a .5-tick boundary)"
        else:
            klass = "MATCH"
            detail = ""

        rec.update({
            "class": klass, "detail": detail,
            "direction": r["direction_py"],
            "entry_py": float(r["entry_price_py"]), "entry_mt5": float(r["entry_price_mt5"]),
            "d_entry_pts": d_entry / point,
            "d_sl_pts": d_sl / point, "d_tp_pts": d_tp / point,
            "exit_py": r["exit_reason_py"], "exit_mt5": r["exit_reason_mt5"],
            "d_exit_pts": d_exit / point,
            "points_py": float(r["points_py"]) if "points_py" in r else np.nan,
            "points_mt5": float(r["points_mt5"]) if "points_mt5" in r else np.nan,
            "ambiguous": ambiguous,
        })
        out.append(rec)

    detail = pd.DataFrame(out)
    counts = detail["class"].value_counts().to_dict() if len(detail) else {}
    n_logic = int(counts.get("LOGIC", 0))
    n_match = int(counts.get("MATCH", 0))
    n_total = int(len(detail))

    pts_py = float(pd.to_numeric(p.get("points"), errors="coerce").sum()) if "points" in p else np.nan
    pts_mt5 = float(pd.to_numeric(m.get("points"), errors="coerce").sum()) if "points" in m else np.nan

    if n_logic == 0 and n_total:
        status = PASS
    elif n_total and n_logic / float(n_total) <= 0.02:
        status = WARN
    else:
        status = FAIL

    costs = None
    if "profit" in m.columns and m["profit"].notna().any():
        # MT5's booked profit already handles currency conversion, so prefer
        # it; the price-derived value only fills rows the EA could not book
        # (the end-of-run forced close).
        prof = pd.to_numeric(m["profit"], errors="coerce")
        derived = pd.to_numeric(m.get("points"), errors="coerce") * contract_size * lot
        gross = float(prof.fillna(derived).sum())
        swap = float(pd.to_numeric(m.get("swap"), errors="coerce").sum())
        comm = float(pd.to_numeric(m.get("commission"), errors="coerce").sum())
        nights = _count_rollovers(m)
        n_tr = max(len(m), 1)
        # Swap is charged per lot-night and its SIGN usually differs between
        # long and short (gold on this broker: -67.9 vs +27.0), so a single
        # blended figure would mis-price whichever side is rarer.
        per_night_dir = {}
        for side in ("long", "short"):
            sub = m[m["direction"] == side]
            if not len(sub):
                continue
            nl = _count_rollovers(sub)
            lots = pd.to_numeric(sub.get("lots"), errors="coerce")
            lot_nights = _count_rollovers(sub, weight=lots) if lots.notna().any() else nl
            sw = float(pd.to_numeric(sub.get("swap"), errors="coerce").sum())
            per_night_dir[side] = (sw / lot_nights) if lot_nights else None
        costs = {"gross": gross, "swap": swap, "commission": comm,
                 "net": gross + swap + comm,
                 "nights": nights,
                 "per_night": (swap / nights) if nights else None,
                 "per_night_dir": per_night_dir,
                 "comm_per_trade": comm / n_tr,
                 # per LOT, not per trade - sizes vary under risk sizing
                 "total_lots": float(pd.to_numeric(m.get("lots"),
                                                   errors="coerce").sum())
                 if "lots" in m.columns else float(n_tr) * lot}

    stats = {
        "status": status,
        "costs": costs,
        "py_trades": int(len(p)),
        "mt5_trades": int(len(m)),
        "matched": n_total - int(counts.get("LOGIC", 0)),
        "exact_match": n_match,
        "classes": counts,
        "match_rate": (100.0 * n_match / n_total) if n_total else 0.0,
        "points_py": pts_py,
        "points_mt5": pts_mt5,
        "points_diff": (pts_mt5 - pts_py) if np.isfinite(pts_py) and np.isfinite(pts_mt5) else np.nan,
        "ambiguous_py": int(pd.to_numeric(p.get("ambiguous"), errors="coerce").fillna(0).sum())
                        if "ambiguous" in p else 0,
    }
    return detail, stats


# --------------------------------------------------------------------------
def render_report(strategy_name, bar_summary, bar_stats, trade_detail,
                  trade_stats, py_stats=None, mt5_stats=None, lot=0.01):
    """Human-readable reconciliation report."""
    L = []
    W = 74
    L.append("=" * W)
    L.append("  RECONCILIATION  %s" % strategy_name)
    L.append("=" * W)

    L.append("")
    L.append("-- 1. indicators (bar by bar) " + "-" * (W - 30))
    if bar_summary is None:
        L.append("   skipped: %s" % bar_stats.get("reason", bar_stats.get("status")))
        L.append("   run with --bars to enable (the EA then writes a per-bar CSV)")
    else:
        L.append("   bars compared : %d" % bar_stats["bars_compared"])
        L.append("   %-16s %8s %14s %14s %8s  %s"
                 % ("indicator", "n", "max_abs_diff", "max_rel_diff", "n_bad", "verdict"))
        for _, r in bar_summary.iterrows():
            L.append("   %-16s %8d %14.3e %14.3e %8d  %s"
                     % (r["indicator"], r["n"], r["max_abs_diff"],
                        r["max_rel_diff"], r["n_bad"], r["verdict"]))
        L.append("   => %s (%d/%d indicators diverge)"
                 % (bar_stats["status"], bar_stats["indicators_diverging"],
                    bar_stats["indicators"]))

    L.append("")
    L.append("-- 2. trades " + "-" * (W - 13))
    L.append("   python : %d trades" % trade_stats["py_trades"])
    L.append("   mt5    : %d trades" % trade_stats["mt5_trades"])
    if trade_stats.get("classes"):
        for k in ("MATCH", "EOD", "BOUNDARY", "SPREAD", "ROUNDING",
                  "AMBIGUOUS", "LOGIC"):
            if k in trade_stats["classes"]:
                L.append("   %-10s %4d" % (k, trade_stats["classes"][k]))
    L.append("   exact match rate : %.1f%%" % trade_stats["match_rate"])
    if np.isfinite(trade_stats.get("points_diff", np.nan)):
        L.append("   net move python  : %+.5f" % trade_stats["points_py"])
        L.append("   net move mt5     : %+.5f" % trade_stats["points_mt5"])
        L.append("   difference       : %+.5f" % trade_stats["points_diff"])

    cost = trade_stats.get("costs")
    if cost:
        L.append("")
        L.append("-- 2b. where the money went " + "-" * (W - 28))
        L.append("   The Python engine prices movement only; MT5 also books")
        L.append("   swap and commission.  Gross must match, costs explain the rest.")
        L.append("   gross P/L from price (mt5) : %+10.2f" % cost["gross"])
        L.append("   swap                       : %+10.2f" % cost["swap"])
        L.append("   commission                 : %+10.2f" % cost["commission"])
        L.append("   ------------------------------------------")
        L.append("   MT5 booked net             : %+10.2f" % cost["net"])
        if lot and cost.get("per_night") is not None:
            L.append("")
            L.append("   Calibrated cost model - paste into the strategy spec:")
            L.append("")
            pd_dir = cost.get("per_night_dir") or {}
            sl_rate = pd_dir.get("long")
            ss_rate = pd_dir.get("short")
            if sl_rate is None:
                sl_rate = cost["per_night"] / lot
            if ss_rate is None:
                ss_rate = cost["per_night"] / lot
            tot_lots = cost.get("total_lots") or (trade_stats["mt5_trades"] * lot)
            L.append("       costs=Costs(")
            L.append("           commission_per_lot=%.2f,"
                     % (-cost["commission"] / tot_lots if tot_lots else 0.0))
            L.append("           swap_long_per_lot_night=%.2f," % sl_rate)
            L.append("           swap_short_per_lot_night=%.2f," % ss_rate)
            L.append("       ),")
            L.append("")
            L.append("   (%d weighted rollovers over %d trades)"
                     % (cost["nights"], trade_stats["mt5_trades"]))

    if trade_detail is not None and len(trade_detail):
        bad = trade_detail[trade_detail["class"] == "LOGIC"]
        if len(bad):
            L.append("")
            L.append("   first %d logic divergences:" % min(15, len(bad)))
            for _, r in bad.head(15).iterrows():
                L.append("     %s  %s" % (r["bar_time"], r["detail"]))

    if py_stats or mt5_stats:
        L.append("")
        L.append("-- 3. headline figures " + "-" * (W - 24))
        L.append("   %-22s %14s %14s" % ("", "python", "mt5"))
        pairs = [("trades", "trades", "total_trades"),
                 ("net profit", "net_profit", "total_net_profit"),
                 ("win rate %", "win_rate", "win_rate"),
                 ("profit factor", "profit_factor", "profit_factor"),
                 ("sharpe", "sharpe", "sharpe_ratio"),
                 ("max DD %", "max_dd_pct", "max_dd_pct")]
        for label, pk, mk in pairs:
            pv = (py_stats or {}).get(pk)
            mv = (mt5_stats or {}).get(mk)
            fmt = lambda v: ("%14.4f" % v) if isinstance(v, (int, float)) and v is not None else "%14s" % "-"
            L.append("   %-22s %s %s" % (label, fmt(pv), fmt(mv)))

    L.append("")
    L.append("=" * W)
    overall = trade_stats["status"]
    if bar_summary is not None and bar_stats["status"] == FAIL:
        overall = FAIL
    L.append("  VERDICT: %s" % overall)
    if overall == PASS:
        L.append("  Python and MT5 agree - it is safe to iterate in Python alone.")
    elif overall == WARN:
        L.append("  Minor divergence only. Inspect the classes above before trusting")
        L.append("  Python-only results.")
    else:
        L.append("  Do NOT trust Python-only results yet. Fix indicators first,")
        L.append("  then re-check trades.")
    L.append("=" * W)
    return "\n".join(L), overall


def save_report(strategy_name, text, trade_detail=None, bar_summary=None):
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    base = os.path.join(config.RESULTS_DIR, strategy_name)
    with open(base + "_reconcile.txt", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    if trade_detail is not None and len(trade_detail):
        trade_detail.to_csv(base + "_reconcile_trades.csv", index=False,
                            encoding="utf-8")
    if bar_summary is not None and len(bar_summary):
        bar_summary.to_csv(base + "_reconcile_bars.csv", index=False,
                           encoding="utf-8")
    return base + "_reconcile.txt"
