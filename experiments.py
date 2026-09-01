# -*- coding: utf-8 -*-
"""Pre-declared experiment runner.

This script does NOT pick a winner. It runs a grid that was declared before
the results were seen, writes every cell to disk with its full configuration,
and prints rank tables carrying an explicit multiple-testing warning. Choosing
a configuration from a rank table computed on 2022-2026 gold is exactly the
mistake the Deflated Sharpe Ratio exists to punish, and the runner reports the
trial count so that deflation can actually be applied.

    python experiments.py --suite axes        one axis at a time (default)
    python experiments.py --suite grid        full cross product (720 cells)
    python experiments.py --suite costs       execution and carry stress
    python experiments.py --suite stops       H4 approximation vs M5 vs M1
    python experiments.py --suite all

Every cell writes results/v2/exp_<suite>/<tag>_summary.json plus a combined
CSV/JSON index. Nothing overwrites the frozen baseline.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from msvsd import __version__                               # noqa: E402
from msvsd.campaigns import build_campaigns, daily_frames   # noqa: E402
from msvsd.config import RunConfig, code_version            # noqa: E402
from msvsd.reporting import notional_report, stop_report    # noqa: E402
from msvsd.run import build_and_run                         # noqa: E402
from msvsd.statistics import (deflated_sharpe, full_report, lo_adjusted_sharpe,
                              profit_factor, risk_metrics)  # noqa: E402

# ---- the declared grid ----------------------------------------------------
AXIS_SLEEVES = ["all", "medium-slow", "slow-only"]
AXIS_DIRECTION = ["symmetric", "long-only", "slow-confirmed-shorts"]
AXIS_ATR_MULT = [2.0, 2.5, 3.0, 3.5, 4.0]
AXIS_EXIT_SCALE = [0.75, 1.00, 1.25, 1.50]
AXIS_RISK = [0.10, 0.15, 0.20, 0.25]

COST_SCENARIOS = [
    ("cost_1x", dict(cost_scale=1.0)),
    ("cost_2x", dict(cost_scale=2.0)),
    ("cost_3x", dict(cost_scale=3.0)),
    ("swap_flat", dict(swap_model="flat")),
    ("swap_scenario_low", dict(swap_model="scenario", swap_scenario="low")),
    ("swap_scenario_base", dict(swap_model="scenario", swap_scenario="base")),
    ("swap_scenario_high", dict(swap_model="scenario", swap_scenario="high")),
    ("swap_none_TV_COMPARISON_ONLY", dict(swap_model="none")),
    ("lot_step_0.01", dict(lot_step=0.01)),
    ("lot_step_0.001", dict(lot_step=0.001)),
    ("capital_100k", dict(capital=100000.0)),
    ("capital_1m", dict(capital=1000000.0)),
]


def light_metrics(res, camps: pd.DataFrame, cfg: RunConfig) -> Dict:
    """Headline numbers only - the grid does not need 20k bootstraps per cell."""
    _eq, dret = daily_frames(res.equity)
    r = risk_metrics(res.equity, dret["ret"], res.bars, res.diagnostics, cfg.capital)
    lo = lo_adjusted_sharpe(dret["ret"])
    tr, d = res.trades, res.diagnostics
    nt = notional_report(res)
    out = dict(
        tag=cfg.tag,
        sleeves=cfg.sleeve_mode, direction=cfg.direction_mode,
        atr_mult=cfg.atr_mult, exit_scale=cfg.exit_scale, risk_pct=cfg.risk_pct,
        capital=cfg.capital, lot_step=cfg.lot_step, cost_scale=cfg.cost_scale,
        swap_model=cfg.swap_model, swap_scenario=cfg.swap_scenario,
        stop_mode=cfg.stop_mode,
        net_profit=r.get("net_profit"), return_pct=r.get("return_pct"),
        cagr_pct=r.get("cagr_pct"), max_dd_pct=r.get("max_dd_pct"),
        calmar=r.get("calmar"), sortino=r.get("sortino"),
        ann_vol_pct=r.get("ann_vol_pct"), exposure_pct=r.get("exposure_pct"),
        sharpe_naive=lo.get("sharpe_naive"), sharpe_lo=lo.get("sharpe_lo_adjusted"),
        sharpe_period=lo.get("sharpe_period"), n_days=lo.get("n_days"),
        skew_daily=r.get("skew_daily"), kurtosis_daily=r.get("kurtosis_daily"),
        gross_pnl=float(tr["gross"].sum()) if len(tr) else 0.0,
        spread_slip=d["cost_spread_slip"], commission=d["cost_commission"],
        swap=d["cost_swap"],
        trades=int(len(tr)),
        trade_mean_R=float(tr["r_multiple"].mean()) if len(tr) else np.nan,
        trade_pf_gross=profit_factor(tr["gross"].to_numpy(float)) if len(tr) else np.nan,
        campaigns=int(len(camps)),
        campaign_mean_usd=float(camps["net_pnl"].mean()) if len(camps) else np.nan,
        campaign_mean_R_eq=float(camps["net_R"].mean()) if len(camps) else np.nan,
        campaign_win_pct=float(100.0 * (camps["net_pnl"] > 0).mean()) if len(camps) else np.nan,
        cap_binding_bars=nt["cap_binding_bars"], cap_binding_pct=nt["cap_binding_pct"],
        exposure_shortfall_pct=nt["exposure_shortfall_pct"],
        net_target_rounding_loss=nt["net_target_rounding_loss_lot_bars"],
        sizing_rounding_loss_lots=nt["sizing_rounding_loss_lots"],
        sizing_rounding_loss_pct=nt["sizing_rounding_loss_pct"],
        max_position_lots=nt["max_abs_position_lots"],
        labels=";".join(cfg.labels()),
        config_fingerprint=cfg.fingerprint(),
    )
    # gross vs net, kept separate as required
    out["net_minus_gross"] = out["net_profit"] - out["gross_pnl"]
    return out


def yearly_split(res, camps) -> List[Dict]:
    from msvsd.reporting import yearly_table
    y = yearly_table(res.equity, res.trades, camps)
    return y.to_dict("records")


def run_cell(base: RunConfig, tag: str, overrides: Dict, outdir: str,
             keep_yearly: bool = True) -> Dict:
    cfg = base.replace(tag=tag, outdir=outdir, **overrides)
    cfg.validate()
    t0 = time.time()
    res = build_and_run(cfg, verbose=False)
    camps = build_campaigns(res.bars, res.trades, res.fills, res.financing,
                            cfg.contract_oz)
    m = light_metrics(res, camps, cfg)
    m["seconds"] = round(time.time() - t0, 2)
    os.makedirs(outdir, exist_ok=True)
    payload = {"config": cfg.to_dict(), "metrics": m,
               "code_version": code_version(),
               "stops": stop_report(res), "notional": notional_report(res)}
    if keep_yearly:
        payload["yearly"] = yearly_split(res, camps)
        m["yearly"] = payload["yearly"]
    with open(os.path.join(outdir, "%s_summary.json" % tag), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return m


# --------------------------------------------------------------------------
def suite_axes(base: RunConfig, outdir: str) -> List[Dict]:
    """One axis at a time from the baseline. Neighbouring-value stability is
    readable directly off each axis because everything else is held fixed."""
    cells = [("axis_baseline", {})]
    for v in AXIS_SLEEVES:
        cells.append(("axis_sleeves_%s" % v, dict(sleeve_mode=v)))
    for v in AXIS_DIRECTION:
        cells.append(("axis_direction_%s" % v, dict(direction_mode=v)))
    for v in AXIS_ATR_MULT:
        cells.append(("axis_atrmult_%.1f" % v, dict(atr_mult=v)))
    for v in AXIS_EXIT_SCALE:
        cells.append(("axis_exitscale_%.2f" % v, dict(exit_scale=v)))
    for v in AXIS_RISK:
        cells.append(("axis_risk_%.2f" % v, dict(risk_pct=v)))
    seen, out = set(), []
    for tag, ov in cells:
        key = json.dumps(ov, sort_keys=True)
        if key in seen and tag != "axis_baseline":
            continue
        seen.add(key)
        out.append(run_cell(base, tag, ov, outdir))
        print("   %-34s net %10.2f  dd %6.2f%%  sharpe_lo %6.3f  camps %4d"
              % (tag, out[-1]["net_profit"], out[-1]["max_dd_pct"],
                 out[-1]["sharpe_lo"] or float("nan"), out[-1]["campaigns"]))
    return out


def suite_grid(base: RunConfig, outdir: str) -> List[Dict]:
    combos = list(itertools.product(AXIS_SLEEVES, AXIS_DIRECTION, AXIS_ATR_MULT,
                                    AXIS_EXIT_SCALE, AXIS_RISK))
    print("   full cross product: %d cells" % len(combos))
    out = []
    for i, (sl, di, am, ex, rk) in enumerate(combos, 1):
        tag = "grid_%s_%s_a%.1f_e%.2f_r%.2f" % (sl, di, am, ex, rk)
        out.append(run_cell(base, tag, dict(sleeve_mode=sl, direction_mode=di,
                                            atr_mult=am, exit_scale=ex,
                                            risk_pct=rk), outdir,
                            keep_yearly=False))
        if i % 40 == 0 or i == len(combos):
            print("   %4d/%d cells" % (i, len(combos)))
    return out


def suite_costs(base: RunConfig, outdir: str, swap_file: Optional[str]) -> List[Dict]:
    out = []
    scen = list(COST_SCENARIOS)
    if swap_file:
        scen.insert(3, ("swap_historical", dict(swap_model="historical",
                                                swap_file=swap_file)))
    for tag, ov in scen:
        out.append(run_cell(base, "cost_" + tag, ov, outdir))
        m = out[-1]
        print("   %-34s net %10.2f  dd %6.2f%%  swap %10.2f  execcost %8.2f"
              % (tag, m["net_profit"], m["max_dd_pct"], m["swap"],
                 m["spread_slip"] + m["commission"]))
    return out


def suite_stops(base: RunConfig, outdir: str) -> List[Dict]:
    """H4 approximation against real intrabar replay at M5 and M1."""
    out = []
    for tag, ov in [("stops_h4_approx", dict(stop_mode="h4")),
                    ("stops_ltf_m5", dict(stop_mode="ltf", ltf_file="M5")),
                    ("stops_ltf_m1", dict(stop_mode="ltf", ltf_file="M1"))]:
        out.append(run_cell(base, tag, ov, outdir))
        m = out[-1]
        print("   %-20s net %10.2f  dd %6.2f%%  sharpe_lo %6.3f  trades %4d"
              % (tag, m["net_profit"], m["max_dd_pct"],
                 m["sharpe_lo"] or float("nan"), m["trades"]))
    return out


def stop_comparison(rows: List[Dict], outdir: str) -> pd.DataFrame:
    df = pd.DataFrame(rows).set_index("tag")
    if "stops_h4_approx" not in df.index:
        return pd.DataFrame()
    base = df.loc["stops_h4_approx"]
    cmp_rows = []
    for tag in df.index:
        r = df.loc[tag]
        cmp_rows.append(dict(
            run=tag, net_profit=r["net_profit"], return_pct=r["return_pct"],
            max_dd_pct=r["max_dd_pct"], sharpe_lo=r["sharpe_lo"],
            trades=r["trades"],
            d_net_vs_h4=r["net_profit"] - base["net_profit"],
            d_return_pp=r["return_pct"] - base["return_pct"],
            d_maxdd_pp=r["max_dd_pct"] - base["max_dd_pct"],
            d_sharpe=(r["sharpe_lo"] or np.nan) - (base["sharpe_lo"] or np.nan),
            d_trades=r["trades"] - base["trades"]))
    out = pd.DataFrame(cmp_rows)
    out.to_csv(os.path.join(outdir, "stop_mode_comparison.csv"),
               index=False, encoding="utf-8")
    return out


# --------------------------------------------------------------------------
def rank_tables(rows: List[Dict], outdir: str, suite: str) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(outdir, "index_%s.csv" % suite), index=False,
              encoding="utf-8")
    with open(os.path.join(outdir, "index_%s.json" % suite), "w",
              encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)

    n = len(df)
    # DSR needs PER-OBSERVATION Sharpes, so deflate on sharpe_period rather
    # than the annualised figure the rank tables display.
    sr = df["sharpe_period"].astype(float).dropna()
    sr_std = float(sr.std(ddof=1)) if len(sr) > 1 else None
    best = df.loc[sr.idxmax()] if len(sr) else None

    print("\n" + "=" * 78)
    print("RANK TABLES - FOR INSPECTION ONLY")
    print("=" * 78)
    print("""  %d configurations were evaluated on the SAME 2022-2026 gold sample.
  Ranking them and taking the top row is selection on noise. The spread of
  per-day Sharpe across these %d trials is %s, which is the
  input the Deflated Sharpe Ratio needs; the DSR for the apparent best cell is
  printed below. No configuration is recommended here, and none of these
  results is evidence that any variation beats the baseline out of sample."""
          % (n, n, ("%.4f" % sr_std) if sr_std else "undefined (need >1 trial)"))

    for metric, asc in (("sharpe_lo", False), ("net_profit", False),
                        ("max_dd_pct", True), ("calmar", False)):
        if metric not in df:
            continue
        sub = df.sort_values(metric, ascending=asc).head(10)
        cols = [c for c in ("tag", "net_profit", "return_pct", "max_dd_pct",
                            "sharpe_lo", "calmar", "campaigns", "trades")
                if c in sub.columns]
        print("\n  top 10 by %s" % metric)
        print(sub[cols].round(3).to_string(index=False))

    if best is not None and sr_std:
        ds = deflated_sharpe(float(best["sharpe_period"]),
                             int(best.get("n_days") or 0),
                             float(best.get("skew_daily") or 0.0),
                             float(best.get("kurtosis_daily") or 3.0),
                             n, sr_std)
        print("\n  Deflated Sharpe for the apparent best cell (%s):" % best["tag"])
        print("     %s" % json.dumps(ds, indent=6, default=str))
        print("     Below 0.95 means the apparent best is not distinguishable from")
        print("     the best you would expect by chance across %d trials." % n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MS-VSD experiment runner v%s" % __version__)
    p.add_argument("--suite", default="axes",
                   choices=("axes", "grid", "costs", "stops", "all"))
    p.add_argument("--outroot", default=os.path.join("results", "v2"))
    p.add_argument("--swap-file", default=None)
    p.add_argument("--seed", type=int, default=20260901)
    p.add_argument("--capital", type=float, default=100000.0)
    a = p.parse_args(argv)

    base = RunConfig(seed=a.seed, capital=a.capital)
    suites = (["axes", "costs", "stops"] if a.suite == "all" else [a.suite])
    all_rows: List[Dict] = []
    for s in suites:
        outdir = os.path.join(a.outroot, "exp_" + s)
        os.makedirs(outdir, exist_ok=True)
        print("\n== suite: %s -> %s ==" % (s, outdir))
        if s == "axes":
            rows = suite_axes(base, outdir)
        elif s == "grid":
            rows = suite_grid(base, outdir)
        elif s == "costs":
            rows = suite_costs(base, outdir, a.swap_file)
        else:
            rows = suite_stops(base, outdir)
            cmp_df = stop_comparison(rows, outdir)
            if len(cmp_df):
                print("\n  -- stop-model comparison (vs the H4 approximation) --")
                print(cmp_df.round(3).to_string(index=False))
        for r in rows:
            r.pop("yearly", None)
        rank_tables(rows, outdir, s)
        all_rows.extend(rows)

    if len(suites) > 1:
        with open(os.path.join(a.outroot, "experiments_index.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"code_version": code_version(),
                       "n_configs_tested": len(all_rows),
                       "suites": suites, "rows": all_rows}, fh, indent=2, default=str)
    print("\n  multiple-testing count for this session: %d configurations" % len(all_rows))
    print("  pass it on with:  bt_xau_msvsd.py --n-configs-tested %d" % len(all_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
