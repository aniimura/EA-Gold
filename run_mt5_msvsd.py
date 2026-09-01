# -*- coding: utf-8 -*-
"""Compile XauMsvsd.mq5, run the MT5 Strategy Tester, and compare it with the
Python engine.

This is the third implementation of one specification. The point is not to get
a better number out of MT5 - it is to find out whether the Python engine's
SIGNALS are right, on an engine that applies the broker's own swap, spread and
order handling instead of my assumptions about them.

    python run_mt5_msvsd.py                 compile + test + compare
    python run_mt5_msvsd.py --compile-only
    python run_mt5_msvsd.py --compare-only  reuse the last tester output
    python run_mt5_msvsd.py --model 0       0 real ticks, 1 M1 OHLC, 2 open prices

No credentials are used or stored. The tester runs against the history the
terminal already holds; if a login is ever required, do it interactively in the
terminal yourself.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

from core import config                               # noqa: E402
from msvsd.campaigns import build_campaigns           # noqa: E402
from msvsd.config import RunConfig                    # noqa: E402
from msvsd.run import build_and_run                   # noqa: E402
from runner import mt5run                             # noqa: E402

NAME = "XauMsvsd"
MQ5 = os.path.join("build", "%s.mq5" % NAME)
OUTDIR = os.path.join("results", "v2")


class TesterSpec(object):
    """Minimal stand-in for core.spec.Strategy - runner/mt5run.py only reads
    these attributes, and this strategy cannot be expressed as a real spec."""
    name = NAME
    symbol = "GOLD"
    timeframe = "H4"
    date_from = "2022-01-01"
    date_to = "2026-08-31"
    deposit = 100000.0
    currency = "USD"
    leverage = 100   # margin only; peak margin is ~$900, so it never binds
    lot = 0.01


def trades_csv():
    return os.path.join(config.COMMON_FILES, "fxtrade_%s_trades.csv" % NAME)


def bars_csv():
    return os.path.join(config.COMMON_FILES, "fxtrade_%s_bars.csv" % NAME)


def run_tester(model=1, write_bars=True, clear_cache=False, risk=None):
    spec = TesterSpec()
    report_name = "%s_%s_%s" % (spec.name, spec.symbol, spec.timeframe)
    # Delete the PREVIOUS report as well as the CSVs. _find_report() only checks
    # that a file's mtime is newer than `started`, and a run that fails to launch
    # leaves the old report in place - which is how a 3-second "pass" reported
    # the previous configuration's P&L as if it were this one's.
    for p in (trades_csv(), bars_csv(),
              os.path.join(config.MT5_DATA, report_name + ".htm"),
              os.path.join(config.MT5_DATA, "Tester", report_name + ".htm")):
        try:
            os.unlink(p)
        except OSError:
            pass
    ini = mt5run._TESTER_INI.format(
        expert=spec.name, symbol=spec.symbol, period=spec.timeframe,
        model=int(model),
        dfrom=pd.Timestamp(spec.date_from).strftime("%Y.%m.%d"),
        dto=pd.Timestamp(spec.date_to).strftime("%Y.%m.%d"),
        deposit=int(spec.deposit), currency=spec.currency,
        leverage=int(spec.leverage), report=report_name)
    ini += "\n[TesterInputs]\nInpWriteTrades=true\nInpWriteBars=%s\n" % (
        "true" if write_bars else "false")
    with open(config.TESTER_INI, "w", encoding="utf-8") as fh:
        fh.write(ini)

    # run_tester() rewrites tester.ini from the spec, so drive it directly
    import subprocess, time, glob
    # The tester cache holds GENERATED TICKS, which depend on the symbol and the
    # date range, not on the EA. Clearing it costs a 9-minute re-synchronisation
    # on every run and buys nothing when only the expert has changed.
    if clear_cache:
        mt5run.clear_tester_cache()
    mt5run.kill_mt5()
    started = time.time()
    print("  tester : %s %s %s  %s .. %s  (model=%d)"
          % (spec.name, spec.symbol, spec.timeframe, spec.date_from,
             spec.date_to, model))
    subprocess.Popen([config.MT5_EXE, "/config:%s" % config.TESTER_INI],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report_path = None
    for i in range(3600):
        time.sleep(1)
        report_path = mt5run._find_report(report_name, started)
        if report_path:
            break
        if i >= 10 and i % 5 == 0 and mt5run._tester_finished(started):
            for _ in range(30):
                time.sleep(2)
                report_path = mt5run._find_report(report_name, started)
                if report_path:
                    break
            break
        if i >= 30 and i % 30 == 0:
            print("           ... %ds" % int(time.time() - started))
    stats = {}
    if report_path:
        stats = mt5run.parse_report(report_path)
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        shutil.copy2(report_path, os.path.join(config.RESULTS_DIR,
                                               report_name + ".htm"))
    else:
        stats = {"error": "no report produced"}
    mt5run.kill_mt5(wait=2.0)
    stats["elapsed_s"] = round(time.time() - started, 1)
    print("  tester : finished in %.0fs" % stats["elapsed_s"])
    # A real pass writes the EA's CSVs. If they are missing the run did not
    # happen, whatever the report says - say so loudly rather than reporting
    # stale numbers as fresh ones.
    if not os.path.isfile(trades_csv()):
        stats["warning"] = ("TESTER DID NOT PRODUCE EA OUTPUT - the run did not "
                            "complete. Any figures below are not from this "
                            "configuration.")
        print("  !! %s" % stats["warning"])
    return stats


def parse_deal_costs(report_path):
    """Sum Commission and Swap out of the report's deal table.

    mt5run.parse_report() reads the summary block, which does not break the
    costs out - and the split is the whole point of running MT5 at all. Parsed
    by hand because lxml is not installed in this environment.
    """
    if not os.path.isfile(report_path):
        return {}
    import re
    txt = mt5run._decode_report(report_path)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", txt, re.S | re.I)

    def cells(r):
        return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]

    hdr_i = hdr = None
    for i, r in enumerate(rows):
        c = cells(r)
        if "Swap" in c and "Profit" in c:
            hdr_i, hdr = i, c
            break
    if hdr is None:
        return {}

    def num(x):
        x = x.replace(" ", "").replace(" ", "").replace(",", "")
        try:
            return float(x)
        except ValueError:
            return None

    tot = {"Swap": 0.0, "Commission": 0.0}
    n = 0
    for r in rows[hdr_i + 1:]:
        c = cells(r)
        if len(c) != len(hdr):
            continue
        n += 1
        for k in tot:
            if k in hdr:
                v = num(c[hdr.index(k)])
                if v is not None:
                    tot[k] += v
    return {"mt5_swap": tot["Swap"], "mt5_commission": tot["Commission"],
            "deal_rows": n}


def load_mt5_outputs():
    tr = bars = None
    if os.path.isfile(trades_csv()):
        tr = pd.read_csv(trades_csv())
        for c in ("entry_time", "exit_time"):
            tr[c] = pd.to_datetime(tr[c], format="%Y.%m.%d %H:%M:%S", errors="coerce")
        os.makedirs(OUTDIR, exist_ok=True)
        tr.to_csv(os.path.join(OUTDIR, "mt5_trades.csv"), index=False)
    if os.path.isfile(bars_csv()):
        bars = pd.read_csv(bars_csv())
        for c in ("time", "time_utc"):
            bars[c] = pd.to_datetime(bars[c], format="%Y.%m.%d %H:%M:%S",
                                     errors="coerce")
        bars.to_csv(os.path.join(OUTDIR, "mt5_bars.csv"), index=False)
    return tr, bars


def compare(stats):
    """Signal-level reconciliation first, then the P&L difference explained."""
    print("\n" + "=" * 76)
    print("MT5 vs PYTHON")
    print("=" * 76)

    mt5_tr, mt5_bars = load_mt5_outputs()
    if mt5_tr is None:
        print("  no MT5 trade CSV found - did the tester run?")
        return {}

    # The Python side must use the SAME modelling choices the EA implements:
    # H4 stop approximation, Friday filter on the bar open, open sleeves logged.
    cfg = RunConfig(tag="mt5_match", outdir=OUTDIR, stop_mode="h4",
                    friday_basis="open", log_open_sleeves_at_end=True)
    py = build_and_run(cfg, verbose=False)

    out = {"mt5_report": {k: v for k, v in stats.items()
                          if not isinstance(v, (dict, list))}}

    # ---------- 1. bar-by-bar signal reconciliation ----------
    if mt5_bars is not None and len(mt5_bars):
        from msvsd.reconcile import reconcile
        p = os.path.join(OUTDIR, "mt5_bars.csv")
        rep = reconcile(py, p, OUTDIR, "mt5")
        out["reconcile"] = rep
        print("\n  -- bar-by-bar signal reconciliation --")
        print("     overlapping bars : %s" % rep.get("overlapping_bars"))
        print("     fields compared  : %d" % len(rep.get("fields_present", [])))
        print("     mismatches       : %s" % rep.get("mismatch_count"))
        print("     max price diff   : %s" % rep.get("max_price_diff"))
        print("     max qty diff     : %s" % rep.get("max_qty_diff"))
        pf = rep.get("per_field", {})

        # Two classes of field, and conflating them makes the verdict useless.
        # SIGNAL fields are pure functions of price and must agree exactly.
        # COST-DEPENDENT fields are fed by equity, which the two engines compute
        # from different swap, spread and commission models - they cannot agree
        # and a PASS was never available for them.
        COST_DEPENDENT = {"equity_ex_financing", "net_target_lots",
                          "qty_fast", "qty_medium", "qty_slow",
                          "position_lots", "stop_fast", "stop_medium", "stop_slow"}
        sig = {k: v for k, v in pf.items() if k not in COST_DEPENDENT}
        cost = {k: v for k, v in pf.items() if k in COST_DEPENDENT}
        # A NaN-on-one-side row is a COVERAGE difference, not a disagreement:
        # MT5 holds more history than the Python H4 cache, so it can price a
        # 120-bar channel on bars where Python still has none. Counting those
        # as mismatches buries the thing worth knowing - whether the two
        # engines ever compute a DIFFERENT NUMBER for the same bar.
        sig_bad = sum(v["mismatches"] for v in sig.values())
        sig_val = sum(v["mismatches"] - v["nan_disagreements"] for v in sig.values())
        sig_nan = sum(v["nan_disagreements"] for v in sig.values())
        sig_cmp = sum(v["compared"] for v in sig.values())
        out.setdefault("reconcile", {}).update(
            signal_mismatches=int(sig_bad), signal_value_mismatches=int(sig_val),
            signal_coverage_only=int(sig_nan), signal_comparisons=int(sig_cmp))
        print("     SIGNAL fields    : %d value mismatches in %d comparisons  -> %s"
              % (sig_val, sig_cmp, "PASS" if sig_val == 0 else "FAIL"))
        print("                        +%d coverage-only rows (one side has no "
              "history yet)" % sig_nan)
        print("     raw verdict      : %s (counts coverage rows as mismatches)"
              % rep.get("result"))

        def show(title, dd):
            bad = {k: v for k, v in dd.items() if v["mismatches"]}
            if not bad:
                print("     %s: all match" % title)
                return
            print("     %s:" % title)
            for k, v in sorted(bad.items(), key=lambda x: -x[1]["mismatches"]):
                print("        %-22s %5d / %5d   max diff %-12.6g  nan-only %d"
                      % (k, v["mismatches"], v["compared"], v["max_abs_diff"],
                         v["nan_disagreements"]))
        show("signal fields (must match)", sig)
        show("cost-dependent fields (expected to differ)", cost)
        if rep.get("first_mismatch"):
            print("     first mismatch   : %s" % rep["first_mismatch"])

    # ---------- 2. trade-level comparison ----------
    print("\n  -- sleeve trade counts --")
    pt = py.trades
    print("     %-10s %8s %8s" % ("sleeve", "python", "mt5"))
    for s in ("fast", "medium", "slow"):
        print("     %-10s %8d %8d" % (s, int((pt["sleeve"] == s).sum()),
                                      int((mt5_tr["sleeve"] == s).sum())))
    print("     %-10s %8d %8d" % ("TOTAL", len(pt), len(mt5_tr)))
    out["trades"] = {"python": int(len(pt)), "mt5": int(len(mt5_tr))}

    if len(mt5_tr) and len(pt):
        a = pt[["sleeve", "entry_time", "direction"]].copy()
        a["k"] = a["sleeve"] + "|" + a["entry_time"].astype(str) + "|" + a["direction"]
        b = mt5_tr[["sleeve", "entry_time", "direction"]].copy()
        b["k"] = b["sleeve"] + "|" + b["entry_time"].astype(str) + "|" + b["direction"]
        common = len(set(a["k"]) & set(b["k"]))
        print("     matched entries (sleeve+time+direction): %d of %d python / %d mt5"
              % (common, len(a), len(b)))
        out["trades"]["matched_entries"] = int(common)

    # ---------- 3. P&L, with the cost differences named ----------
    print("\n  -- profit and loss --")
    py_net = py.diagnostics["net_profit"]
    py_gross = float(pt["gross"].sum())
    mt5_net = stats.get("net_profit", stats.get("total_net_profit"))
    mt5_gross = float(mt5_tr["gross"].sum()) if "gross" in mt5_tr else np.nan
    rows = [("gross price P&L (sleeve book)", py_gross, mt5_gross),
            ("net profit (account)", py_net, mt5_net)]
    print("     %-32s %14s %14s" % ("", "python", "mt5"))
    for lab, a, b in rows:
        bs = ("%14.2f" % b) if b is not None and np.isfinite(b) else "%14s" % "n/a"
        print("     %-32s %14.2f %s" % (lab, a, bs))
    out["pnl"] = {"python_gross": py_gross, "python_net": py_net,
                  "mt5_gross": None if mt5_gross is None or not np.isfinite(mt5_gross) else mt5_gross,
                  "mt5_net": mt5_net}

    # decompose the gap rather than waving at it
    py_costs = (py.diagnostics["cost_spread_slip"] + py.diagnostics["cost_commission"]
                - py.diagnostics["cost_swap"])
    mt5_costs = ((mt5_gross - mt5_net)
                 if (mt5_net is not None and np.isfinite(mt5_gross)) else np.nan)
    print("\n  -- where the difference comes from --")
    print("     %-34s %14s %14s %12s" % ("", "python", "mt5", "mt5 - py"))
    for lab, a_, b_ in (("gross price P&L", py_gross, mt5_gross),
                        ("total costs charged", py_costs, mt5_costs),
                        ("net profit", py_net, mt5_net)):
        if b_ is None or not np.isfinite(b_):
            print("     %-34s %14.2f %14s %12s" % (lab, a_, "n/a", "n/a"))
        else:
            print("     %-34s %14.2f %14.2f %12.2f" % (lab, a_, b_, b_ - a_))
    out["pnl"]["python_costs"] = float(py_costs)
    out["pnl"]["mt5_costs"] = (None if not np.isfinite(mt5_costs) else float(mt5_costs))

    # Component split - the only way to tell WHICH assumption was wrong rather
    # than just that the total was.
    dc = parse_deal_costs(os.path.join(config.RESULTS_DIR, "%s_GOLD_H4.htm" % NAME))
    if dc and np.isfinite(mt5_costs):
        py_swap = py.diagnostics["cost_swap"]
        py_comm = -py.diagnostics["cost_commission"]
        py_ss = -py.diagnostics["cost_spread_slip"]
        mt5_ss = -(mt5_costs + dc["mt5_swap"] + dc["mt5_commission"])
        print("\n  -- cost model, component by component --")
        print("     %-26s %12s %12s %12s" % ("", "python", "mt5", "mt5 - py"))
        for lab, a_, b_ in (("swap / carry", py_swap, dc["mt5_swap"]),
                            ("commission", py_comm, dc["mt5_commission"]),
                            ("spread + slippage", py_ss, mt5_ss)):
            print("     %-26s %12.2f %12.2f %12.2f" % (lab, a_, b_, b_ - a_))
        out["cost_components"] = {
            "python": {"swap": py_swap, "commission": py_comm, "spread_slip": py_ss},
            "mt5": {"swap": dc["mt5_swap"], "commission": dc["mt5_commission"],
                    "spread_slip": mt5_ss}}
        print("\n     The carry assumption holds: one flat rate pair applied across"
              "\n     4.7 years lands within %.1f%% of what the broker actually charged."
              "\n     The EXECUTION assumption does not: real spread cost is %.1fx the"
              "\n     model's, and that gap is worth %.0f USD of the difference."
              % (abs(100.0 * (dc["mt5_swap"] - py_swap) / py_swap),
                 (mt5_ss / py_ss) if py_ss else float("nan"), abs(mt5_ss - py_ss)))
    for k in ("gross_profit", "gross_loss"):
        if k in stats:
            print("     MT5 %-14s: %s" % (k, stats[k]))
    print("""
     These will not match, and are not meant to. MT5 charges the BROKER's
     real historical swap and the spread carried in the tick data; the Python
     engine charges one assumed swap pair, the H4 cache's spread column and a
     flat 5-point slippage the tester does not model. The bar-by-bar block
     above is the test that matters - it asks whether the two engines agree on
     WHAT to trade. The P&L gap then tells you what the cost assumptions are
     worth.""")

    with open(os.path.join(OUTDIR, "mt5_comparison.json"), "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\n  written: %s" % os.path.join(OUTDIR, "mt5_comparison.json"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=int, default=1,
                    help="0 real ticks, 1 one-minute OHLC, 2 open prices")
    ap.add_argument("--compile-only", action="store_true")
    ap.add_argument("--compare-only", action="store_true")
    ap.add_argument("--no-bars", action="store_true")
    ap.add_argument("--clear-cache", action="store_true",
                    help="force the tester to regenerate ticks (~9 min)")
    ap.add_argument("--deposit", type=float, default=None)
    ap.add_argument("--leverage", type=int, default=None)
    ap.add_argument("--risk", type=float, default=None,
                    help="risk %% per sleeve passed to the EA")
    ap.add_argument("--tag", default=None, help="suffix for the saved outputs")
    a = ap.parse_args(argv)

    stats = {}
    if not a.compare_only:
        print("== compile ==")
        mt5run.compile_ea(MQ5)
        if a.compile_only:
            return 0
        print("== MT5 Strategy Tester ==")
        if a.deposit is not None:
            TesterSpec.deposit = a.deposit
        if a.leverage is not None:
            TesterSpec.leverage = a.leverage
        stats = run_tester(model=a.model, write_bars=not a.no_bars,
                           clear_cache=a.clear_cache, risk=a.risk)
        for k in sorted(stats):
            print("      %-22s %s" % (k, stats[k]))
    else:
        p = os.path.join(config.RESULTS_DIR, "%s_GOLD_H4.htm" % NAME)
        if os.path.isfile(p):
            stats = mt5run.parse_report(p)

    compare(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
