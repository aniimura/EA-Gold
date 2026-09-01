# -*- coding: utf-8 -*-
"""Audit of the minimum-lot override across the three predeclared profiles.

Runs ONLY the three profiles that were declared before this analysis:
baseline_strict, small_account_override, small_account_override_stress.
No parameter search, no new configurations.

RESEARCH INTEGRITY
    The two small-account profiles were specified AFTER the 2022-2026 results
    were already known. They are post-hoc, they are appended to the existing
    experiment registry behind the 720 grid cells, and the multiple-testing
    count carries forward rather than resetting. Nothing here is independent
    confirmation of anything.

    python audit_override.py        -> results/v2/override_audit.json
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from msvsd.campaigns import build_campaigns, concentration, daily_frames  # noqa: E402
from msvsd.config import RunConfig, apply_profile, code_version           # noqa: E402
from msvsd.reporting import (cost_waterfall, notional_report,             # noqa: E402
                             sizing_report, yearly_table)
from msvsd.run import build_and_run                                       # noqa: E402
from msvsd.statistics import deflated_sharpe, full_report                 # noqa: E402

OUT = os.path.join("results", "v2")
PROFILES = ["baseline_strict", "small_account_override",
            "small_account_override_stress"]
# The pre-existing search. The profiles below are appended to it, never
# substituted for it.
PRIOR_CONFIGS = 720


def pct(a, qs=(0, 10, 25, 50, 75, 90, 95, 100)):
    a = np.asarray([x for x in a if np.isfinite(x)], float)
    if not len(a):
        return {}
    return {("p%d" % q if 0 < q < 100 else ("min" if q == 0 else "max")):
            float(np.percentile(a, q)) for q in qs}


def run_profile(name, n_configs):
    cfg = apply_profile(RunConfig(tag="audit_" + name, outdir=OUT), name)
    cfg = cfg.replace(n_configs_tested=n_configs)
    res = build_and_run(cfg, verbose=False)
    camps = build_campaigns(res.bars, res.trades, res.fills, res.financing,
                            cfg.contract_oz)
    _eq, dret = daily_frames(res.equity)
    stats = full_report(res.trades, camps, res.equity, dret["ret"], res.bars,
                        res.diagnostics, cfg.capital, cfg.bootstrap_n, cfg.seed,
                        n_trials=n_configs)
    return cfg, res, camps, dret["ret"], stats


def profile_block(name, cfg, res, camps, dret, stats):
    z, tr, d = res.sizing, res.trades, res.diagnostics
    eq = res.equity.dropna()
    risk = stats["risk"]
    acc = z[z["final_lots"] > 0]
    ovr = z[z["override_used"]]

    lots = tr["lots"].to_numpy(float) if len(tr) else np.array([])
    atr = tr["atr_at_entry"].to_numpy(float) if len(tr) else np.array([])
    corr = (float(np.corrcoef(lots, 1.0 / atr)[0, 1])
            if len(lots) > 2 and lots.std() > 0 else None)

    hold_h = ((pd.DatetimeIndex(tr["exit_time"]) - pd.DatetimeIndex(tr["entry_time"]))
              .total_seconds().to_numpy() / 3600.0) if len(tr) else np.array([])

    by_dir, by_sleeve = {}, {}
    if len(tr):
        for k, g in tr.groupby("direction"):
            by_dir[k] = dict(trades=int(len(g)), gross=float(g["gross"].sum()),
                             win_pct=float(100 * (g["gross"] > 0).mean()),
                             mean_R=float(g["r_multiple"].mean()))
        for k, g in tr.groupby("sleeve"):
            by_sleeve[k] = dict(trades=int(len(g)), gross=float(g["gross"].sum()),
                                win_pct=float(100 * (g["gross"] > 0).mean()),
                                mean_R=float(g["r_multiple"].mean()))

    reasons = z["reason"].value_counts().to_dict() if len(z) else {}
    camp_boot = stats.get("bootstrap_campaigns_usd") or {}

    out = {
        "profile": name,
        "diagnostic_only": name.endswith("_stress"),
        "labels": cfg.labels(),
        "config": {k: cfg.to_dict()[k] for k in
                   ("capital", "risk_pct", "target_risk_pct_per_sleeve",
                    "enable_min_lot_override", "override_max_risk_pct_per_sleeve",
                    "max_total_open_risk_pct", "minimum_lot", "lot_step",
                    "contract_oz")},
        # ---- headline
        "starting_equity": float(cfg.capital),
        "ending_equity": float(eq.iloc[-1]) if len(eq) else None,
        "net_profit": risk["net_profit"], "return_pct": risk["return_pct"],
        "cagr_pct": risk["cagr_pct"],
        "max_dd_usd": risk["max_dd_money"], "max_dd_pct": risk["max_dd_pct"],
        "ann_vol_pct": risk.get("ann_vol_pct"),
        "sharpe_lo_adjusted": stats["sharpe"].get("sharpe_lo_adjusted"),
        "sharpe_naive": stats["sharpe"].get("sharpe_naive"),
        "sortino": risk.get("sortino"), "calmar": risk.get("calmar"),
        "exposure_pct": risk.get("exposure_pct"),
        # ---- counts
        "signals": int(len(z)),
        "sleeve_trades": int(len(tr)),
        "campaigns": int(len(camps)),
        "trades_override": int(reasons.get("ORDER_ACCEPTED_MINIMUM_OVERRIDE", 0)),
        "trades_normal": int(reasons.get("ORDER_ACCEPTED_NORMAL_SIZE", 0)),
        "skipped_below_minimum": int(reasons.get("OVERRIDE_DISABLED", 0)),
        "rejected_sleeve_cap": int(reasons.get("OVERRIDE_SLEEVE_RISK_EXCEEDED", 0)),
        "rejected_portfolio_cap": int(reasons.get("PORTFOLIO_OPEN_RISK_EXCEEDED", 0)),
        "by_reason": {k: int(v) for k, v in reasons.items()},
        # ---- risk actually taken
        "avg_stop_risk_pct": float(acc["actual_stop_risk_pct"].mean()) if len(acc) else None,
        "max_stop_risk_pct": float(acc["actual_stop_risk_pct"].max()) if len(acc) else None,
        "avg_total_open_risk_pct": float(acc["total_open_risk_pct_after"].mean()) if len(acc) else None,
        "max_total_open_risk_pct": float(acc["total_open_risk_pct_after"].max()) if len(acc) else None,
        # ---- sizing fidelity
        "distinct_position_sizes": int(len(np.unique(np.round(lots, 6)))) if len(lots) else 0,
        "corr_size_inv_atr": corr,
        # ---- composition
        "by_direction": by_dir, "by_sleeve": by_sleeve,
        "yearly": yearly_table(res.equity, tr, camps).round(4).to_dict("records"),
        # ---- costs
        "gross_profit": float(tr["gross"].sum()) if len(tr) else 0.0,
        "cost_spread_slip": d["cost_spread_slip"],
        "cost_commission": d["cost_commission"],
        "cost_swap": d["cost_swap"],
        "cost_waterfall": cost_waterfall(res).to_dict("records"),
        # ---- evidence
        "concentration_campaigns": (concentration(camps["net_pnl"].to_numpy(float),
                                                  "campaign_net") if len(camps) else {}),
        "campaign_bootstrap_usd": camp_boot,
        "p_mean_campaign_le_zero": camp_boot.get("p_mean_le_zero"),
        "block_monthly": stats.get("bootstrap_block_monthly"),
        "block_quarterly": stats.get("bootstrap_block_quarterly"),
        "deflated_sharpe": stats.get("deflated_sharpe"),
        # ---- exposure distributions
        "dist_stop_risk_pct": pct(acc["actual_stop_risk_pct"]) if len(acc) else {},
        "dist_total_open_risk_pct": pct(acc["total_open_risk_pct_after"]) if len(acc) else {},
        "dist_hold_hours": pct(hold_h) if len(hold_h) else {},
        "dist_atr_at_entry": pct(atr) if len(atr) else {},
        "dist_stop_distance": pct(tr["stop_dist"].to_numpy(float)) if len(tr) else {},
        "notional": notional_report(res),
        "sizing_report": sizing_report(res),
    }
    return out


def override_rates(z):
    """Acceptance and rejection rates by year, sleeve and direction."""
    if not len(z):
        return {}
    z = z.copy()
    z["year"] = pd.DatetimeIndex(z["time"]).year
    z["accepted"] = z["final_lots"] > 0
    cand = z[z["condition"] == "NORMAL_SIZE_BELOW_MINIMUM"]
    out = {}
    for key in ("year", "sleeve", "direction"):
        rows = []
        for k, g in cand.groupby(key):
            rows.append({key: (int(k) if key == "year" else str(k)),
                         "candidates": int(len(g)),
                         "accepted": int(g["accepted"].sum()),
                         "accept_pct": float(100 * g["accepted"].mean()),
                         "rej_sleeve": int((g["reason"] == "OVERRIDE_SLEEVE_RISK_EXCEEDED").sum()),
                         "rej_portfolio": int((g["reason"] == "PORTFOLIO_OPEN_RISK_EXCEEDED").sum())})
        out["by_" + key] = rows
    return out


def diagnostics(name, cfg, res, camps):
    """The eight required investigations."""
    z, tr = res.sizing, res.trades
    cand = z[z["condition"] == "NORMAL_SIZE_BELOW_MINIMUM"].copy()
    if not len(cand):
        return {}
    acc = cand[cand["final_lots"] > 0]
    rej = cand[cand["final_lots"] <= 0]
    d = {}

    # 1 / 2 / 5 -- volatility regime
    d["1_accepted_atr"] = pct(acc["atr"], (10, 50, 90))
    d["2_rejected_atr"] = pct(rej["atr"], (10, 50, 90))
    d["volatility_separation"] = {
        "accepted_median_atr": float(acc["atr"].median()) if len(acc) else None,
        "rejected_median_atr": float(rej["atr"].median()) if len(rej) else None,
        "ratio_rejected_over_accepted": (float(rej["atr"].median() / acc["atr"].median())
                                         if len(acc) and len(rej) and acc["atr"].median() else None),
        "atr_threshold_implied": float(cfg.override_max_risk_pct_per_sleeve / 100.0
                                       * cfg.capital / (cfg.atr_mult * cfg.contract_oz
                                                        * cfg.minimum_lot)),
    }
    # 5 -- is it a volatility filter in disguise?
    thr = d["volatility_separation"]["atr_threshold_implied"]
    d["5_acts_as_volatility_filter"] = {
        "implied_atr_ceiling": thr,
        "pct_accepted_below_ceiling": (float(100 * (acc["atr"] <= thr).mean())
                                       if len(acc) else None),
        "pct_rejected_above_ceiling": (float(100 * (rej["atr"] > thr).mean())
                                       if len(rej) else None),
    }
    # 3 -- does the portfolio cap suppress the third sleeve?
    rows = []
    for k, g in cand.groupby("sleeve"):
        rows.append({"sleeve": str(k), "candidates": int(len(g)),
                     "rej_portfolio": int((g["reason"] == "PORTFOLIO_OPEN_RISK_EXCEEDED").sum()),
                     "rej_portfolio_pct": float(100 * (g["reason"] == "PORTFOLIO_OPEN_RISK_EXCEEDED").mean())})
    d["3_portfolio_cap_by_sleeve"] = rows
    # how many sleeves were already open when the portfolio cap fired
    pcap = cand[cand["reason"] == "PORTFOLIO_OPEN_RISK_EXCEEDED"]
    d["3_open_risk_when_portfolio_rejected"] = pct(pcap["total_open_risk_pct_before"],
                                                   (10, 50, 90))
    # 4 -- composition shift
    comp = {}
    for key in ("sleeve", "direction"):
        c_all = cand[key].value_counts(normalize=True).mul(100).round(2).to_dict()
        c_acc = acc[key].value_counts(normalize=True).mul(100).round(2).to_dict() if len(acc) else {}
        comp[key] = {"candidates_pct": c_all, "accepted_pct": c_acc}
    d["4_composition_shift"] = comp
    # 6 -- cost share at the minimum lot
    if len(acc):
        d["6_cost_share_at_min_lot"] = {
            "median_price_stop_loss": float(acc["price_stop_loss"].median()),
            "median_estimated_costs": float(acc["estimated_costs"].median()),
            "costs_as_pct_of_stop_risk": float(100 * acc["estimated_costs"].sum()
                                               / acc["actual_stop_risk"].sum()),
        }
    if len(tr):
        gross = float(tr["gross"].sum())
        costs = (res.diagnostics["cost_spread_slip"] + res.diagnostics["cost_commission"]
                 - res.diagnostics["cost_swap"])
        d["6_realised_cost_share_of_gross"] = (float(100 * costs / gross)
                                               if gross else None)
    # 7 -- campaign concentration
    if len(camps):
        c = concentration(camps["net_pnl"].to_numpy(float), "campaign_net")
        d["7_top5_share_pct"] = c.get("campaign_net_top5_share_pct")
        d["7_excl_top5_total"] = c.get("campaign_net_excl_top5_total")
        d["7_campaigns"] = int(len(camps))
    return d


def main():
    reg_path = os.path.join(OUT, "experiment_registry.json")
    registry = {"prior_configurations": PRIOR_CONFIGS,
                "prior_source": "experiments.py --suite grid (720 cells)",
                "post_hoc_profiles": [],
                "note": "The profiles below were specified AFTER the 2022-2026 "
                        "results were known. They are appended to the prior "
                        "count, never substituted for it."}
    n = PRIOR_CONFIGS
    blocks = {}
    for name in PROFILES:
        n += 1
        registry["post_hoc_profiles"].append(
            {"index": n, "name": name, "post_hoc": True,
             "diagnostic_only": name.endswith("_stress")})
        cfg, res, camps, dret, stats = run_profile(name, n)
        blk = profile_block(name, cfg, res, camps, dret, stats)
        blk["registry_index"] = n
        blk["n_configs_tested"] = n
        blk["override_rates"] = override_rates(res.sizing)
        blk["diagnostics"] = diagnostics(name, cfg, res, camps)
        blocks[name] = blk
        print("  %-32s net %9.2f  DD %6.2f%%  Sharpe %6.3f  ovr %4d  rej %4d"
              % (name, blk["net_profit"], blk["max_dd_pct"],
                 blk["sharpe_lo_adjusted"] or float("nan"),
                 blk["trades_override"], blk["rejected_sleeve_cap"]
                 + blk["rejected_portfolio_cap"]))
    registry["total_configurations_examined"] = n
    with open(reg_path, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2)

    # the earlier 0.50 %-TARGET scenario, for the required comparison
    comp_cfg = RunConfig(tag="audit_target050", outdir=OUT, capital=10000.0,
                         risk_pct=0.50, target_risk_pct_per_sleeve=0.50)
    cres = build_and_run(comp_cfg, verbose=False)
    ccamps = build_campaigns(cres.bars, cres.trades, cres.fills, cres.financing)
    _e, cdret = daily_frames(cres.equity)
    cstats = full_report(cres.trades, ccamps, cres.equity, cdret["ret"], cres.bars,
                         cres.diagnostics, comp_cfg.capital, comp_cfg.bootstrap_n,
                         comp_cfg.seed, n_trials=n)
    comp = profile_block("target_0.50pct_scenario", comp_cfg, cres, ccamps,
                         cdret["ret"], cstats)
    comp["note"] = ("NOT the override. This raises the SIZING TARGET to 0.50 %, "
                    "which changes every position size. The override leaves the "
                    "target at 0.10 % and only permits a single minimum lot.")

    payload = {"code_version": code_version(), "registry": registry,
               "profiles": blocks, "target_050_comparison": comp}
    p = os.path.join(OUT, "override_audit.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=float)
    print("\n  registry: %d prior + %d post-hoc = %d configurations examined"
          % (PRIOR_CONFIGS, len(PROFILES), n))
    print("  written : %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
