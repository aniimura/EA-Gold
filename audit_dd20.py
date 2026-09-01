# -*- coding: utf-8 -*-
"""Post-hoc experiment 724: the dd20_experiment risk profile.

Runs the strict baseline and dd20_experiment through the SAME final engine -
M1 intrabar stop replay, corrected (pre-fill) financing timing, Pine Friday
basis, and the existing spread / commission / slippage / swap assumptions.

Only the one specified 0.70 % risk level is tested. No nearby values are
searched and nothing is selected on profitability.

    python audit_dd20.py      -> results/v2/dd20_audit.json
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
from msvsd.reporting import (notional_report, sizing_report,             # noqa: E402
                             write_outputs, yearly_table)
from msvsd.run import build_and_run                                       # noqa: E402
from msvsd.statistics import full_report                                  # noqa: E402

OUT = os.path.join("results", "v2")
REGISTRY_INDEX = 724
DD_LIMIT_PCT = 20.0
OPEN_RISK_LIMIT_PCT = 2.00

# The final engine settings the published baseline uses. Applied identically to
# both runs so the only difference is the risk profile itself.
FINAL_ENGINE = dict(stop_mode="ltf", ltf_file="M1", friday_basis="close",
                    financing_timing="pre-fill", log_open_sleeves_at_end=True)


def pct(a, qs=(0, 10, 25, 50, 75, 90, 95, 100)):
    a = np.asarray([x for x in a if np.isfinite(x)], float)
    if not len(a):
        return {}
    return {("p%d" % q if 0 < q < 100 else ("min" if q == 0 else "max")):
            float(np.percentile(a, q)) for q in qs}


def run(cfg, tag):
    cfg = cfg.replace(tag=tag, outdir=OUT, n_configs_tested=REGISTRY_INDEX,
                      **FINAL_ENGINE)
    res = build_and_run(cfg, verbose=False)
    camps = build_campaigns(res.bars, res.trades, res.fills, res.financing,
                            cfg.contract_oz)
    _eq, dret = daily_frames(res.equity)
    stats = full_report(res.trades, camps, res.equity, dret["ret"], res.bars,
                        res.diagnostics, cfg.capital, cfg.bootstrap_n, cfg.seed,
                        n_trials=REGISTRY_INDEX)
    # the per-run machine-readable files, alongside the audit summary
    write_outputs(res, camps, stats, cfg)
    return cfg, res, camps, stats


def block(name, cfg, res, camps, stats):
    tr, z, d = res.trades, res.sizing, res.diagnostics
    eq = res.equity.dropna()
    risk = stats["risk"]
    acc = z[z["final_lots"] > 0]
    lots = tr["lots"].to_numpy(float) if len(tr) else np.array([])
    atr = tr["atr_at_entry"].to_numpy(float) if len(tr) else np.array([])
    corr = (float(np.corrcoef(lots, 1.0 / atr)[0, 1])
            if len(lots) > 2 and lots.std() > 0 else None)
    cb = stats.get("bootstrap_campaigns_usd") or {}
    conc = (concentration(camps["net_pnl"].to_numpy(float), "campaign_net")
            if len(camps) else {})

    by_dir, by_sleeve = {}, {}
    for key, dst in (("direction", by_dir), ("sleeve", by_sleeve)):
        if len(tr):
            for k, g in tr.groupby(key):
                dst[str(k)] = dict(trades=int(len(g)),
                                   gross=float(g["gross"].sum()),
                                   win_pct=float(100 * (g["gross"] > 0).mean()),
                                   mean_R=float(g["r_multiple"].mean()))

    return {
        "profile": name,
        "config": {k: cfg.to_dict()[k] for k in
                   ("capital", "risk_pct", "target_risk_pct_per_sleeve",
                    "max_total_open_risk_pct", "enforce_total_open_risk_on_normal",
                    "enable_min_lot_override", "minimum_lot", "lot_step",
                    "contract_oz", "stop_mode", "friday_basis",
                    "financing_timing")},
        "labels": cfg.labels(),
        "starting_equity": float(cfg.capital),
        "ending_equity": float(eq.iloc[-1]) if len(eq) else None,
        "net_profit": risk["net_profit"], "return_pct": risk["return_pct"],
        "cagr_pct": risk["cagr_pct"],
        "max_dd_usd": risk["max_dd_money"], "max_dd_pct": risk["max_dd_pct"],
        "ann_vol_pct": risk.get("ann_vol_pct"),
        "sharpe": stats["sharpe"].get("sharpe_lo_adjusted"),
        "sortino": risk.get("sortino"), "calmar": risk.get("calmar"),
        "exposure_pct": risk.get("exposure_pct"),
        "signals": int(len(z)), "trades": int(len(tr)),
        "campaigns": int(len(camps)),
        "by_reason": {k: int(v) for k, v in z["reason"].value_counts().items()},
        "avg_risk_pct": float(acc["actual_stop_risk_pct"].mean()) if len(acc) else None,
        "max_risk_pct": float(acc["actual_stop_risk_pct"].max()) if len(acc) else None,
        "avg_total_open_risk_pct": float(acc["total_open_risk_pct_after"].mean()) if len(acc) else None,
        "max_total_open_risk_pct": float(acc["total_open_risk_pct_after"].max()) if len(acc) else None,
        "distinct_sizes": int(len(np.unique(np.round(lots, 6)))) if len(lots) else 0,
        "corr_size_inv_atr": corr,
        "by_direction": by_dir, "by_sleeve": by_sleeve,
        "yearly": yearly_table(res.equity, tr, camps).round(4).to_dict("records"),
        "gross_profit": float(tr["gross"].sum()) if len(tr) else 0.0,
        "cost_spread_slip": d["cost_spread_slip"],
        "cost_commission": d["cost_commission"],
        "cost_swap": d["cost_swap"],
        "top5_share_pct": conc.get("campaign_net_top5_share_pct"),
        "excl_top5_total": conc.get("campaign_net_excl_top5_total"),
        "campaign_bootstrap_usd": cb,
        "p_mean_campaign_le_zero": cb.get("p_mean_le_zero"),
        "block_monthly": stats.get("bootstrap_block_monthly"),
        "block_quarterly": stats.get("bootstrap_block_quarterly"),
        "dist_risk_pct": pct(acc["actual_stop_risk_pct"]) if len(acc) else {},
        "dist_total_open_risk_pct": pct(acc["total_open_risk_pct_after"]) if len(acc) else {},
        "notional": notional_report(res), "sizing_report": sizing_report(res),
    }


def classify(base, exp):
    """The five declared failure conditions. Judged on risk, not on return."""
    checks = []

    dd = exp["max_dd_pct"]
    checks.append(dict(
        id="max_drawdown", limit="<= %.0f %%" % DD_LIMIT_PCT,
        observed="%.2f %%" % dd, failed=bool(dd > DD_LIMIT_PCT),
        note="declared acceptance limit for this experiment"))

    mo = exp["max_total_open_risk_pct"] or 0.0
    checks.append(dict(
        id="combined_open_risk", limit="<= %.2f %%" % OPEN_RISK_LIMIT_PCT,
        observed="%.3f %%" % mo, failed=bool(mo > OPEN_RISK_LIMIT_PCT + 1e-9),
        note="peak combined open stop risk across all sleeves, gross"))

    # volatility scaling: size must still respond to 1/ATR
    c = exp["corr_size_inv_atr"]
    n = exp["distinct_sizes"]
    gone = bool(n <= 1 or c is None or c < 0.50)
    checks.append(dict(
        id="volatility_scaling", limit="corr >= 0.50 and > 1 distinct size",
        observed="corr %s, %d sizes" % (("%+.2f" % c) if c is not None else "n/a", n),
        failed=gone,
        note="baseline for comparison: corr %+.2f, %d sizes"
             % (base["corr_size_inv_atr"], base["distinct_sizes"])))

    # dependence on the five largest campaigns
    t5 = exp["top5_share_pct"]
    ex5 = exp["excl_top5_total"]
    dep = bool(t5 is not None and (t5 > 100.0 or (ex5 is not None and ex5 <= 0)))
    checks.append(dict(
        id="top5_dependence", limit="net stays positive without the top five",
        observed="top5 %.1f %% of net; excluding them %s"
                 % (t5 if t5 is not None else float("nan"),
                    ("%+.2f USD" % ex5) if ex5 is not None else "n/a"),
        failed=dep,
        note="baseline: top5 %.1f %%, excluding them %+.2f USD"
             % (base["top5_share_pct"], base["excl_top5_total"])))

    # risk-adjusted performance versus the control
    sb, se = base["sharpe"], exp["sharpe"]
    worse = bool(se is not None and sb and se < sb * 0.75)
    checks.append(dict(
        id="risk_adjusted", limit="Sharpe >= 75 % of the baseline",
        observed="%.3f vs baseline %.3f (%.0f %%)"
                 % (se, sb, 100.0 * se / sb),
        failed=worse, note="materially worse is judged at a 25 % shortfall"))

    failed = [c["id"] for c in checks if c["failed"]]
    return {"checks": checks, "failed_conditions": failed,
            "verdict": "FAILED" if failed else "PASSED",
            "verdict_note":
                "Classification is against the five declared conditions only. "
                "A pass is not evidence of an edge; every figure is in-sample."}


def main():
    print("post-hoc experiment %d - dd20_experiment" % REGISTRY_INDEX)
    print("  engine: M1 stop replay, pre-fill carry, Pine Friday basis\n")

    bcfg, bres, bcamps, bstats = run(
        apply_profile(RunConfig(), "baseline_strict"), "dd20_control")
    base = block("baseline_strict", bcfg, bres, bcamps, bstats)
    print("  %-18s net %9.2f  ret %+6.2f%%  DD %6.2f%%  Sharpe %.3f"
          % ("baseline_strict", base["net_profit"], base["return_pct"],
             base["max_dd_pct"], base["sharpe"]))

    ecfg, eres, ecamps, estats = run(
        apply_profile(RunConfig(), "dd20_experiment"), "dd20_experiment")
    exp = block("dd20_experiment", ecfg, eres, ecamps, estats)
    print("  %-18s net %9.2f  ret %+6.2f%%  DD %6.2f%%  Sharpe %.3f"
          % ("dd20_experiment", exp["net_profit"], exp["return_pct"],
             exp["max_dd_pct"], exp["sharpe"]))

    verdict = classify(base, exp)
    print("\n  verdict: %s" % verdict["verdict"])
    for c in verdict["checks"]:
        print("     [%s] %-20s %-34s %s"
              % ("FAIL" if c["failed"] else " ok ", c["id"], c["observed"], c["limit"]))

    # append to the registry, never reset it
    rp = os.path.join(OUT, "experiment_registry.json")
    with open(rp, encoding="utf-8") as fh:
        reg = json.load(fh)
    reg["post_hoc_profiles"] = [p for p in reg["post_hoc_profiles"]
                                if p["name"] != "dd20_experiment"]
    reg["post_hoc_profiles"].append(
        {"index": REGISTRY_INDEX, "name": "dd20_experiment", "post_hoc": True,
         "diagnostic_only": True,
         "declared_limit": "max drawdown 20 %",
         "note": "0.70 % target per sleeve, 2.00 % combined open-risk ceiling "
                 "applied to every entry. Only this risk level was tested."})
    reg["total_configurations_examined"] = REGISTRY_INDEX
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2)

    payload = {"code_version": code_version(), "registry_index": REGISTRY_INDEX,
               "engine": FINAL_ENGINE, "baseline": base, "experiment": exp,
               "verdict": verdict,
               "in_sample_only": True,
               "note": "All figures are in-sample. This profile was specified "
                       "after the 2022-2026 results were known and is post-hoc "
                       "experiment %d. Nothing here is a validated edge."
                       % REGISTRY_INDEX}
    p = os.path.join(OUT, "dd20_audit.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=float)
    print("\n  registry -> %d configurations examined" % REGISTRY_INDEX)
    print("  written  : %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
