# -*- coding: utf-8 -*-
"""Result tables, exports and the console summary.

Every headline figure written here is NET of the selected spread, slippage,
commission and financing model. The no-swap variant exists only as a
TradingView comparison and is labelled as such wherever it appears; it is
never presented as deployable performance.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import RunConfig


def _nights(t0, t1) -> int:
    """Broker rollover boundaries between two server timestamps."""
    if pd.isna(t0) or pd.isna(t1):
        return 0
    a, b = pd.Timestamp(t0).normalize(), pd.Timestamp(t1).normalize()
    return max(0, int((b - a).days))


def sleeve_direction_table(res, contract_oz: float = 100.0) -> pd.DataFrame:
    """Trades / gross / costs / net, split by sleeve and direction.

    Execution costs are the ACTUAL costs paid on the netted position, allocated
    to each sleeve by its share of the gross sleeve exposure that produced each
    fill, then split within a sleeve pro rata by traded lots per direction.
    Swap here is the VIRTUAL attribution - what each sleeve would have financed
    standalone. It does not sum to the account's actual carry when sleeves
    offset; that gap is reported separately by `financing_reconciliation`.
    """
    tr = res.trades.copy()
    if not len(tr):
        return pd.DataFrame()
    tr["nights"] = [_nights(a, b) for a, b in zip(tr["entry_time"], tr["exit_time"])]
    tr["lot_nights"] = tr["lots"] * tr["nights"]

    # per-sleeve execution costs -> split by direction pro rata by traded lots
    costs = res.sleeve_costs
    rows = []
    for sleeve, g in tr.groupby("sleeve"):
        c = costs.get(sleeve, {"spread_slip": 0.0, "commission": 0.0, "swap": 0.0})
        tot_lots = float(g["lots"].sum()) or 1.0
        for direction, gd in g.groupby("direction"):
            share = float(gd["lots"].sum()) / tot_lots
            ss = c["spread_slip"] * share
            cm = c["commission"] * share
            if len(res.sleeve_financing):
                sf = res.sleeve_financing
                m = ((sf["sleeve"] == sleeve)
                     & (sf["direction"] == (1 if direction == "long" else -1)))
                sw = float(sf.loc[m, "amount"].sum())
                ln = float(sf.loc[m, "lot_nights"].sum())
            else:
                sw, ln = c["swap"] * share, float(gd["lot_nights"].sum())
            gross = float(gd["gross"].sum())
            risk = float((gd["stop_dist"] * gd["lots"] * contract_oz).sum())
            net = gross - ss - cm + sw
            rows.append(dict(
                sleeve=sleeve, direction=direction, trades=int(len(gd)),
                gross_pnl=gross, spread_slip=ss, commission=cm, swap=sw,
                net_pnl=net,
                avg_holding_nights=float(gd["nights"].mean()),
                total_lot_nights=ln if ln else float(gd["lot_nights"].sum()),
                gross_mean_R=float(gd["r_multiple"].mean()),
                net_mean_R=float(net / risk * len(gd)) if risk > 0 else np.nan,
                win_rate_pct=float(100.0 * (gd["gross"] > 0).mean())))
    df = pd.DataFrame(rows).sort_values(["sleeve", "direction"]).reset_index(drop=True)
    tot = {c: df[c].sum() for c in ("trades", "gross_pnl", "spread_slip",
                                    "commission", "swap", "net_pnl", "total_lot_nights")}
    tot.update(sleeve="TOTAL", direction="", avg_holding_nights=np.nan,
               gross_mean_R=np.nan, net_mean_R=np.nan, win_rate_pct=np.nan)
    return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)


def financing_reconciliation(res) -> Dict:
    """Actual carry on the netted position vs the sum of virtual sleeve carry."""
    actual = float(res.diagnostics.get("cost_swap", 0.0))
    virtual = (float(res.sleeve_financing["amount"].sum())
               if len(res.sleeve_financing) else 0.0)
    return {
        "actual_account_swap": actual,
        "sum_of_virtual_sleeve_swap": virtual,
        "netting_difference": actual - virtual,
        "explanation":
            "The account finances ONE netted position. Each sleeve is priced as if "
            "it stood alone. When sleeves oppose each other, or when the notional "
            "cap or lot step trims the net target below the sum of sleeve sizes, "
            "the account carries less (or more) than the sleeves imply. The gap is "
            "shown rather than smoothed away.",
    }


def cost_waterfall(res) -> pd.DataFrame:
    d = res.diagnostics
    gross = float(res.trades["gross"].sum()) if len(res.trades) else 0.0
    rows = [("gross_price_pnl", gross),
            ("spread_and_slippage", -d["cost_spread_slip"]),
            ("commission", -d["cost_commission"]),
            ("financing_swap", d["cost_swap"])]
    resid = d["net_profit"] - sum(v for _, v in rows)
    rows.append(("netting_and_lot_rounding", resid))
    rows.append(("net_to_account", d["net_profit"]))
    return pd.DataFrame(rows, columns=["component", "usd"])


def yearly_table(equity: pd.Series, trades: pd.DataFrame,
                 camps: pd.DataFrame) -> pd.DataFrame:
    eq = equity.dropna()
    rows = []
    for y, g in eq.groupby(eq.index.year):
        start = eq.loc[:g.index[0]].iloc[-2] if g.index[0] != eq.index[0] else g.iloc[0]
        pk = g.cummax()
        nt = int((pd.DatetimeIndex(trades["exit_time"]).year == y).sum()) if len(trades) else 0
        nc = int((pd.DatetimeIndex(camps["end"]).year == y).sum()) if len(camps) else 0
        rows.append(dict(year=int(y), start_equity=float(start),
                         end_equity=float(g.iloc[-1]),
                         pnl=float(g.iloc[-1] - start),
                         return_pct=float((g.iloc[-1] / start - 1) * 100.0),
                         max_dd_pct=float(((pk - g) / pk * 100.0).max()),
                         trades=nt, campaigns=nc))
    return pd.DataFrame(rows)


def sample_windows(res, cfg: RunConfig) -> Dict:
    """Explicit in-sample / out-of-sample declaration.

    There is no held-out period in this project and pretending otherwise would
    be the single most misleading thing the report could do. Every figure is
    IN-SAMPLE. The out_of_sample block exists to say so in a field a reader
    cannot skim past, and to be filled in the day genuine holdout data exists.
    """
    t = pd.DatetimeIndex(res.bars["time"])
    return {
        "in_sample_start": str(t[0]),
        "in_sample_end": str(t[-1]),
        "in_sample_bars": int(len(t)),
        "out_of_sample_start": None,
        "out_of_sample_end": None,
        "out_of_sample_bars": 0,
        "declaration":
            "NO OUT-OF-SAMPLE PERIOD EXISTS. The whole record above was visible "
            "while this framework was written, and it covers one instrument in "
            "one regime - the largest gold bull market on record. Nothing here "
            "has been validated on data the author had not already seen. Treat "
            "every statistic as a description of this sample.",
    }


def notional_report(res) -> Dict:
    d, b = res.diagnostics, res.bars
    n = int(np.isfinite(b["net_raw_lots"].to_numpy(float)).sum())
    return {
        "bars_evaluated": n,
        "cap_binding_bars": int(d["cap_binding_bars"]),
        "cap_binding_pct": float(100.0 * d["cap_binding_bars"] / n) if n else 0.0,
        "intended_exposure_lot_bars": float(d["intended_exposure_lots"]),
        "executed_exposure_lot_bars": float(d["executed_exposure_lots"]),
        "exposure_shortfall_pct": (
            float(100.0 * (1 - d["executed_exposure_lots"] / d["intended_exposure_lots"]))
            if d["intended_exposure_lots"] else 0.0),
        "net_target_rounding_loss_lot_bars": float(d["lot_rounding_loss_lots"]),
        "sizing_intended_lots": float(d.get("sizing_intended_lots", 0.0)),
        "sizing_executed_lots": float(d.get("sizing_executed_lots", 0.0)),
        "sizing_rounding_loss_lots": float(d.get("sizing_rounding_loss_lots", 0.0)),
        "sizing_rounding_loss_pct": (
            float(100.0 * d.get("sizing_rounding_loss_lots", 0.0)
                  / d["sizing_intended_lots"])
            if d.get("sizing_intended_lots") else 0.0),
        "max_abs_position_lots": float(np.nanmax(np.abs(
            b["position_oz"].to_numpy(float))) / 100.0),
    }


def sizing_report(res) -> Dict:
    """Every sizing verdict, accepted or rejected, summarised.

    The rejection counts are the point: a run that quietly takes no trades and
    a run that takes them all look identical in a P&L table, and the difference
    between them is entirely in here.
    """
    z = res.sizing
    cfg = res.config
    out = {
        "override_enabled": bool(cfg.enable_min_lot_override),
        "target_risk_pct_per_sleeve": cfg.effective_target_risk_pct(),
        "override_max_risk_pct_per_sleeve": cfg.override_max_risk_pct_per_sleeve,
        "max_total_open_risk_pct": cfg.max_total_open_risk_pct,
        "minimum_lot": cfg.minimum_lot, "lot_step": cfg.lot_step,
        "signals_evaluated": int(len(z)),
    }
    if not len(z):
        return out
    out["by_reason"] = {k: int(v) for k, v in z["reason"].value_counts().items()}
    acc = z[z["final_lots"] > 0]
    out["accepted"] = int(len(acc))
    out["accepted_normal"] = int((z["reason"] == "ORDER_ACCEPTED_NORMAL_SIZE").sum())
    out["accepted_override"] = int((z["reason"] == "ORDER_ACCEPTED_MINIMUM_OVERRIDE").sum())
    out["rejected"] = int(len(z) - len(acc))
    if len(acc):
        out["accepted_max_stop_risk_pct"] = float(acc["actual_stop_risk_pct"].max())
        out["accepted_median_stop_risk_pct"] = float(acc["actual_stop_risk_pct"].median())
        out["accepted_max_total_open_risk_pct"] = float(acc["total_open_risk_pct_after"].max())
    ov = z[z["override_used"]]
    if len(ov):
        out["override_stop_risk_pct_median"] = float(ov["actual_stop_risk_pct"].median())
        out["override_stop_risk_pct_max"] = float(ov["actual_stop_risk_pct"].max())
    return out


def stop_report(res) -> Dict:
    d = res.diagnostics
    return {
        "mode": res.config.stop_mode,
        "h4_approximated_stops": int(d["stops_h4_approx"]),
        "ltf_stops_resolved_at_stop_price": int(d["stops_ltf_exact"]),
        "ltf_stops_gapped_through": int(d["stops_ltf_gap"]),
        "ltf_ambiguous_same_bar_events": int(d["ltf_ambiguous_events"]),
        "h4_bars_without_ltf_coverage": int(d["ltf_missing_h4_bars"]),
        "ltf_coverage": d.get("ltf_coverage"),
        "ambiguity_rule": ("all sleeves whose stop is touched inside one LTF bar are "
                           "filled at their own stop price with adverse slippage, in "
                           "the fixed order fast -> medium -> slow; the intrabar path "
                           "is unknowable so the event is logged, never guessed"),
        "ambiguity_log": d.get("ambiguity_log", [])[:50],
    }


# --------------------------------------------------------------------------
def write_outputs(res, camps: pd.DataFrame, stats: Dict, cfg: RunConfig) -> Dict:
    from .campaigns import daily_frames
    base = os.path.join(cfg.outdir, cfg.tag)
    os.makedirs(cfg.outdir, exist_ok=True)

    daily_eq, daily_ret = daily_frames(res.equity)
    paths = {}

    def _w(name, obj, index=False):
        p = base + "_" + name
        if isinstance(obj, pd.DataFrame) or isinstance(obj, pd.Series):
            obj.to_csv(p, index=index, encoding="utf-8")
        else:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2, default=str)
        paths[name] = p
        return p

    _w("sleeve_trades.csv", res.trades)
    _w("campaigns.csv", camps)
    _w("daily_equity.csv", daily_eq, index=True)
    _w("daily_returns.csv", daily_ret, index=True)
    _w("fills.csv", res.fills)
    _w("financing.csv", res.financing)
    _w("sleeve_financing.csv", res.sleeve_financing)
    _w("sizing_log.csv", res.sizing)
    _w("bars_debug.csv", res.bars)
    _w("sleeve_direction.csv", sleeve_direction_table(res, cfg.contract_oz))
    _w("cost_waterfall.csv", cost_waterfall(res))
    _w("yearly.csv", yearly_table(res.equity, res.trades, camps))

    payload = {
        "tag": cfg.tag,
        "labels": cfg.labels(),
        "config": cfg.to_dict(),
        "code_version": res.diagnostics.get("code_version"),
        "config_fingerprint": res.diagnostics.get("config_fingerprint"),
        "statistics": stats,
        "sample_windows": sample_windows(res, cfg),
        "notional": notional_report(res),
        "sizing": sizing_report(res),
        "stops": stop_report(res),
        "financing_model": res.diagnostics.get("financing_coverage"),
        "financing_reconciliation": financing_reconciliation(res),
        "data_quality": res.diagnostics.get("data_quality"),
        "diagnostics": {k: v for k, v in res.diagnostics.items()
                        if k not in ("ambiguity_log", "data_quality")},
    }
    _w("summary.json", payload)
    return paths


# --------------------------------------------------------------------------
def print_summary(res, camps: pd.DataFrame, stats: Dict, cfg: RunConfig) -> None:
    W = 78
    print("=" * W)
    print("XAU Multi-Speed Volatility-Scaled Donchian  -  run tag: %s" % cfg.tag)
    print("=" * W)
    for lab in cfg.labels():
        print("  !! %s" % lab)
    sw = sample_windows(res, cfg)
    print("  !! IN-SAMPLE %s .. %s (%d bars); OUT-OF-SAMPLE: none"
          % (sw["in_sample_start"][:10], sw["in_sample_end"][:10],
             sw["in_sample_bars"]))
    r = stats.get("risk", {})
    print("\n  -- headline (net of every modelled cost) --")
    for k in ("years", "net_profit", "return_pct", "cagr_pct", "max_dd_pct",
              "ann_vol_pct", "sortino", "calmar", "exposure_pct"):
        if k in r:
            print("     %-26s %12.4f" % (k, r[k]))
    sh = stats.get("sharpe", {})
    print("     %-26s %12.4f" % ("sharpe_naive", sh.get("sharpe_naive", float("nan"))))
    print("     %-26s %12.4f" % ("sharpe_lo_adjusted", sh.get("sharpe_lo_adjusted", float("nan"))))

    print("\n  -- cost waterfall (USD) --")
    for _, row in cost_waterfall(res).iterrows():
        print("     %-28s %12.2f" % (row["component"], row["usd"]))

    sd = sleeve_direction_table(res, cfg.contract_oz)
    if len(sd):
        print("\n  -- by sleeve and direction --")
        show = sd[["sleeve", "direction", "trades", "gross_pnl", "spread_slip",
                   "commission", "swap", "net_pnl", "avg_holding_nights",
                   "total_lot_nights", "gross_mean_R"]]
        print(show.round(2).to_string(index=False))

    fr = financing_reconciliation(res)
    print("\n  -- carry reconciliation --")
    print("     actual on netted position   %12.2f" % fr["actual_account_swap"])
    print("     sum of virtual sleeve carry %12.2f" % fr["sum_of_virtual_sleeve_swap"])
    print("     netting difference          %12.2f" % fr["netting_difference"])

    if len(camps):
        print("\n  -- campaigns --")
        cl = stats.get("campaign_level", {})
        print("     %-26s %12s" % ("campaigns", cl.get("n")))
        for k in ("mean_net_R_equal_weighted", "risk_weighted_net_R",
                  "median_net_R", "mean_net_usd", "win_rate_pct",
                  "profit_factor_net", "median_days"):
            if k in cl:
                print("     %-26s %12.4f" % (k, cl[k]))

    print("\n  -- resampling (20k draws, seed %d) --" % cfg.seed)
    for key in ("bootstrap_iid_trades", "bootstrap_campaigns",
                "bootstrap_campaigns_usd", "bootstrap_block_monthly",
                "bootstrap_block_quarterly"):
        b = stats.get(key)
        if not b:
            continue
        print("     %-28s point %+.5f  95%% [%+.5f, %+.5f]  P(<=0)=%.3f  n_eff=%d"
              % (b["method"], b["point_estimate"], b["ci95"][0], b["ci95"][1],
                 b["p_mean_le_zero"], b["effective_observations"]))
    tl = stats.get("trade_level", {}).get("t_test", {})
    if tl:
        print("     %-28s t=%.2f p=%.4f  [%s]"
              % ("t_test_on_sleeve_trades", tl["t"], tl["p_two_sided"], tl["status"]))

    ds = stats.get("deflated_sharpe", {})
    print("\n  -- deflated sharpe --")
    print("     %s" % (ds.get("deflated_sharpe") if ds.get("deflated_sharpe") is not None
                       else ds.get("reason")))

    nt = notional_report(res)
    print("\n  -- exposure control --")
    print("     cap binding on %d bars (%.2f%%); exposure shortfall %.2f%%; "
          "max position %.2f lots"
          % (nt["cap_binding_bars"], nt["cap_binding_pct"],
             nt["exposure_shortfall_pct"], nt["max_abs_position_lots"]))

    st = stop_report(res)
    print("\n  -- protective stops --")
    print("     mode=%s  h4_approx=%d  ltf_at_stop=%d  ltf_gapped=%d  "
          "ambiguous=%d  uncovered_h4_bars=%d"
          % (st["mode"], st["h4_approximated_stops"],
             st["ltf_stops_resolved_at_stop_price"], st["ltf_stops_gapped_through"],
             st["ltf_ambiguous_same_bar_events"], st["h4_bars_without_ltf_coverage"]))
    print()
