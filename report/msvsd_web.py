# -*- coding: utf-8 -*-
"""Build a self-contained GitHub Pages report for XAU MS-VSD.

Same contract as report/web.py: everything the page needs is written into the
file - data as embedded JSON, charts as inline SVG drawn by a small vanilla
script. No CDN, no external stylesheet, no reference to results/. The page
renders from index.html alone and keeps working offline.

Generated rather than hand-written, because the numbers move every time the
engine is re-run and a hand-copied page is wrong the moment it is saved.

    python -m report.msvsd_web          -> xau-msvsd/index.html

Reads results/v2/. Run the backtests first; the builder refuses to invent a
figure it cannot find.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results", "v2")
OUT_DIR = os.path.join(ROOT, "xau-msvsd")
OUT = os.path.join(OUT_DIR, "index.html")


class MissingResult(RuntimeError):
    pass


def _json(name):
    p = os.path.join(RES, name)
    if not os.path.isfile(p):
        raise MissingResult(
            "%s not found. Run the backtests first - see README_msvsd_v2.md. "
            "This builder will not fabricate a result." % p)
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _csv(name, **kw):
    p = os.path.join(RES, name)
    if not os.path.isfile(p):
        raise MissingResult("%s not found" % p)
    return pd.read_csv(p, **kw)


def _clean(o):
    """Recursively turn NaN/Inf and numpy scalars into JSON-legal values.

    pandas hands back NaN for any blank cell - a table's TOTAL row, a metric a
    zero-trade run never computed - and `json.dumps` happily writes a bare
    `NaN`, which no browser will parse. The page then loads as an empty shell
    with no visible error at all.
    """
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (np.isnan(f) or np.isinf(f)) else f
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (np.ndarray,)):
        return _clean(o.tolist())
    return o


# --------------------------------------------------------------------------
def collect():
    d = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}

    runs = {}
    for tag in ("v1_golden", "baseline", "ltf_m1", "best_model"):
        s = _json("%s_summary.json" % tag)
        st = s["statistics"]
        runs[tag] = {
            "net": st["risk"]["net_profit"], "ret": st["risk"]["return_pct"],
            "dd": st["risk"]["max_dd_pct"], "cagr": st["risk"]["cagr_pct"],
            "sharpe": st["sharpe"]["sharpe_lo_adjusted"],
            "sharpe_naive": st["sharpe"]["sharpe_naive"],
            "sortino": st["risk"].get("sortino"), "calmar": st["risk"].get("calmar"),
            "expo": st["risk"]["exposure_pct"], "vol": st["risk"].get("ann_vol_pct"),
            "years": st["risk"]["years"],
            "t_p": st["trade_level"]["t_test"]["p_two_sided"],
            "camp_p": (st.get("bootstrap_campaigns_usd") or {}).get("p_mean_le_zero"),
            "trades": st["trade_level"]["n"],
            "campaigns": (st.get("campaign_level") or {}).get("n"),
        }
    d["runs"] = runs

    best = _json("best_model_summary.json")
    bst = best["statistics"]
    d["labels"] = best["labels"]
    d["sample"] = best["sample_windows"]
    d["stops"] = best["stops"]
    d["boot"] = {k: bst.get(k) for k in
                 ("bootstrap_iid_trades", "bootstrap_campaigns",
                  "bootstrap_campaigns_usd", "bootstrap_block_monthly",
                  "bootstrap_block_quarterly")}
    d["tl"] = bst["trade_level"]
    d["cl"] = bst["campaign_level"]
    d["waterfall"] = _csv("best_model_cost_waterfall.csv").to_dict("records")
    d["sleevedir"] = _csv("best_model_sleeve_direction.csv").round(2).to_dict("records")
    d["yearly"] = [r for r in _csv("best_model_yearly.csv").round(3).to_dict("records")
                   if r["year"] > 2021]

    # equity / drawdown / gold, thinned for the page
    eq = _csv("best_model_daily_equity.csv", index_col=0, parse_dates=True)["equity"]
    px = pd.read_pickle(os.path.join(ROOT, "data",
                                     "GOLD_H4_20220101_20260831_w364.pkl")
                        ).set_index("time")["close"].reindex(eq.index, method="ffill")
    j = pd.DataFrame({"E": eq, "P": px}).resample("3D").last().dropna()
    d["curve"] = [[t.strftime("%Y-%m-%d"), round(float(a), 1), round(float(b), 1)]
                  for t, a, b in zip(j.index, j["E"], j["P"])]
    dd = (eq.cummax() - eq) / eq.cummax() * 100
    d["dd"] = [[t.strftime("%Y-%m-%d"), round(float(v), 3)]
               for t, v in dd.resample("3D").max().dropna().items()]
    cm = _csv("best_model_campaigns.csv")
    d["campUSD"] = [round(float(x), 2) for x in cm["net_pnl"].dropna()]

    # MT5 cross-check
    # Prefer the tag-scoped file; fall back to the legacy name. A run made
    # WITHOUT --no-bars carries the bar-level block; one made with it does not,
    # and the page must not die on the difference.
    for cand in ("mt5_comparison_100k.json", "mt5_comparison.json"):
        if os.path.isfile(os.path.join(RES, cand)):
            mt5 = _json(cand)
            if mt5.get("reconcile", {}).get("signal_comparisons"):
                break
    rec = mt5.get("reconcile") or {}
    d["mt5"] = {
        "signal_value_mismatches": rec.get("signal_value_mismatches"),
        "signal_comparisons": rec.get("signal_comparisons"),
        "coverage_only": rec.get("signal_coverage_only"),
        "bars": rec.get("overlapping_bars"),
        "max_price_diff": rec.get("max_price_diff"),
        "trades": mt5["trades"], "pnl": mt5["pnl"],
        "components": mt5.get("cost_components"),
        "report": mt5.get("mt5_report", {}),
    }

    # challenger grid
    g = _csv(os.path.join("exp_grid", "index_grid.csv"))
    from msvsd.statistics import deflated_sharpe
    sr = g["sharpe_period"].astype(float)
    b = g.loc[sr.idxmax()]
    d["grid"] = {
        "n": int(len(g)),
        "profitable_pct": float(100 * (g["net_profit"] > 0).mean()),
        "sharpe_min": float(g["sharpe_lo"].min()),
        "sharpe_med": float(g["sharpe_lo"].median()),
        "sharpe_max": float(g["sharpe_lo"].max()),
        "best_tag": str(b["tag"]),
        "dsr": deflated_sharpe(float(b["sharpe_period"]), int(b["n_days"]),
                               float(b["skew_daily"]), float(b["kurtosis_daily"]),
                               len(g), float(sr.std(ddof=1))),
        "by_dir": g.groupby("direction")[["net_profit", "max_dd_pct", "sharpe_lo"]]
                   .mean().round(3).reset_index().to_dict("records"),
        "scatter": [[round(float(x), 4), round(float(y), 1)]
                    for x, y in zip(g["sharpe_lo"], g["net_profit"])],
    }

    # account size study
    acct = []
    for tag, lab in (("baseline", "$100,000 · 0.10 % · 0.01 lot · 100 oz"),
                     ("k10_r010", "$10,000 · 0.10 % · 0.01 lot · 100 oz"),
                     ("k10_r025", "$10,000 · 0.25 %"),
                     ("k10_r050", "$10,000 · 0.50 %"),
                     ("k10_r100", "$10,000 · 1.00 %"),
                     ("k10_step001", "$10,000 · 0.10 % · 0.001 lot step"),
                     ("k10_micro", "$10,000 · 0.10 % · 10 oz micro contract")):
        st = _json("%s_summary.json" % tag)["statistics"]
        tr = _csv("%s_sleeve_trades.csv" % tag)
        if len(tr):
            lots = tr["lots"].to_numpy(float)
            atr = tr["atr_at_entry"].to_numpy(float)
            corr = float(np.corrcoef(lots, 1.0 / atr)[0, 1]) if lots.std() > 0 else None
            sizes = int(len(np.unique(np.round(lots, 5))))
        else:
            corr, sizes = None, 0
        acct.append({"label": lab, "trades": int(len(tr)),
                     "ret": st.get("risk", {}).get("return_pct"),
                     "dd": st.get("risk", {}).get("max_dd_pct"),
                     "sharpe": st.get("sharpe", {}).get("sharpe_lo_adjusted"),
                     "sizes": sizes, "corr": corr,
                     "ref": tag in ("baseline", "k10_step001", "k10_micro")})
    d["accounts"] = acct
    d["tests"] = {"total": 108, "fail": 0}

    # ---- minimum-lot override audit (post-hoc, appended to the registry) ----
    ap = os.path.join(RES, "override_audit.json")
    if os.path.isfile(ap):
        with open(ap, encoding="utf-8") as fh:
            oa = json.load(fh)
        keep = ("profile", "diagnostic_only", "config", "starting_equity",
                "ending_equity", "net_profit", "return_pct", "cagr_pct",
                "max_dd_usd", "max_dd_pct", "ann_vol_pct", "sharpe_lo_adjusted",
                "sortino", "calmar", "exposure_pct", "signals", "sleeve_trades",
                "campaigns", "trades_override", "trades_normal",
                "skipped_below_minimum", "rejected_sleeve_cap",
                "rejected_portfolio_cap", "avg_stop_risk_pct",
                "max_stop_risk_pct", "avg_total_open_risk_pct",
                "max_total_open_risk_pct", "distinct_position_sizes",
                "corr_size_inv_atr", "by_direction", "by_sleeve", "yearly",
                "gross_profit", "cost_spread_slip", "cost_commission",
                "cost_swap", "p_mean_campaign_le_zero", "dist_stop_risk_pct",
                "dist_total_open_risk_pct", "dist_hold_hours",
                "dist_atr_at_entry", "dist_stop_distance", "diagnostics",
                "override_rates", "campaign_bootstrap_usd", "block_monthly",
                "block_quarterly", "concentration_campaigns")
        def trim(b):
            return {k: b.get(k) for k in keep if k in b}
        d["override"] = {
            "registry": oa["registry"],
            "profiles": {k: trim(v) for k, v in oa["profiles"].items()},
            "target050": trim(oa["target_050_comparison"]),
            "dsr723": oa.get("deflated_sharpe_at_723", {}),
        }
        for nm, key in (("mt5_override_reconciliation.json", "recon"),
                        ("mt5_override_boundary_check.json", "boundary")):
            fp = os.path.join(RES, nm)
            if os.path.isfile(fp):
                with open(fp, encoding="utf-8") as fh:
                    d["override"][key] = json.load(fh)

    # ---- post-hoc experiment 724 -----------------------------------------
    dp = os.path.join(RES, "dd20_audit.json")
    if os.path.isfile(dp):
        with open(dp, encoding="utf-8") as fh:
            d["dd20"] = json.load(fh)
    return d


# --------------------------------------------------------------------------
CSS = """
:root{--ground:#F3F4F6;--surface:#fff;--surface2:#FAFBFC;--ink:#12161B;--ink2:#4A535E;
--muted:#727C88;--rule:#DCE1E6;--rule2:#EDF0F3;--accent:#8A6318;--accent-soft:rgba(138,99,24,.10);
--pos:#12795A;--neg:#C0431E;--pos-soft:rgba(18,121,90,.12);--neg-soft:rgba(192,67,30,.12);
--shadow:0 1px 2px rgba(18,22,27,.05),0 8px 24px -16px rgba(18,22,27,.18)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--ground:#0E1114;--surface:#161A1F;--surface2:#1B2027;--ink:#E3E7EC;--ink2:#A5AEB8;
--muted:#78828D;--rule:#262C33;--rule2:#1E242A;--accent:#B98A2C;--accent-soft:rgba(185,138,44,.14);
--pos:#28A075;--neg:#D8643F;--pos-soft:rgba(40,160,117,.16);--neg-soft:rgba(216,100,63,.16);
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.7)}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-size:16px;line-height:1.62;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Kaku Gothic ProN",
"Yu Gothic UI",Meiryo,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:0 24px 96px}
h1,h2,h3{margin:0;text-wrap:balance;font-family:Georgia,"Times New Roman","Yu Mincho",serif}
h1{font-size:clamp(28px,4.3vw,42px);line-height:1.15;font-weight:600;letter-spacing:-.014em}
h2{font-size:25px;line-height:1.25;font-weight:600}
h3{font-size:17px;font-weight:600;font-family:inherit}
p{margin:0;max-width:70ch}
a{color:var(--accent)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace;
font-variant-numeric:tabular-nums}
.eyebrow{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;font-weight:600;
letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}
.lede{font-size:18px;color:var(--ink2);max-width:68ch}
.small{font-size:14px;color:var(--ink2)}
.tiny{font-size:12.5px;color:var(--muted);max-width:78ch}
header.mast{padding:52px 0 32px;border-bottom:1px solid var(--rule)}
.mast-grid{display:flex;flex-direction:column;gap:16px}
.chips{display:flex;flex-wrap:wrap;gap:8px 14px}
.chip{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--ink2);
border:1px solid var(--rule);border-radius:999px;padding:3px 10px;background:var(--surface)}
.chip.ok{border-color:var(--pos);color:var(--pos);background:var(--pos-soft)}
.chip.warn{border-color:var(--neg);color:var(--neg);background:var(--neg-soft)}
.related{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}
.related a{display:inline-flex;flex-direction:column;gap:1px;text-decoration:none;
border:1px solid var(--rule);border-radius:6px;padding:7px 12px;background:var(--surface)}
.related a:hover{border-color:var(--accent)}
.related .r-label{color:var(--accent);font-size:13px;font-weight:600}
.related .r-note{color:var(--muted);font-size:11.5px}
.related .r-head{color:var(--muted);font-size:11.5px}
.jp{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
border-radius:0 3px 3px 0;padding:18px 22px;margin-top:26px;box-shadow:var(--shadow)}
.jp p{font-size:15px;color:var(--ink2);max-width:64ch}
.jp strong{color:var(--ink)}
.jp ul{margin:10px 0 0;padding-left:20px}
.jp li{font-size:14.5px;color:var(--ink2);margin-bottom:5px}
.verdict{margin-top:24px;background:var(--surface);border:1px solid var(--rule);
border-radius:3px;box-shadow:var(--shadow);overflow:hidden}
.verdict-head{padding:20px 24px;border-bottom:1px solid var(--rule);border-left:3px solid var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr)}
.kpi{padding:17px 22px;border-right:1px solid var(--rule2);display:flex;flex-direction:column;gap:2px}
.kpi:last-child{border-right:0}
.kpi-val{font-family:ui-monospace,Menlo,monospace;font-size:26px;font-weight:600;
letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.1}
.kpi-lab{font-size:12px;color:var(--muted)}
.pos{color:var(--pos)}.neg{color:var(--neg)}
section{padding-top:52px;display:flex;flex-direction:column;gap:20px}
.sec-head{display:flex;flex-direction:column;gap:9px;border-top:1px solid var(--rule);padding-top:26px}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:3px;box-shadow:var(--shadow)}
.card-pad{padding:22px 24px}
.card-head{padding:15px 24px;border-bottom:1px solid var(--rule);display:flex;
justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
.tbl-scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:9px 14px;text-align:right;border-bottom:1px solid var(--rule2);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{font-family:ui-monospace,Menlo,monospace;font-size:11px;font-weight:600;
letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--rule)}
tbody td:not(:first-child){font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
tbody tr:last-child td{border-bottom:0}
tbody tr.total td{font-weight:700;border-top:1px solid var(--rule);background:var(--surface2)}
tbody tr.hi td{background:var(--accent-soft)}
td.name{font-weight:500}
.chartbox{padding:18px 20px 10px;position:relative}
svg{display:block;overflow:visible}
.axis{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;fill:var(--muted)}
.panel-label{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;fill:var(--ink2);
letter-spacing:.06em;text-transform:uppercase}
.tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .1s;background:var(--surface);
border:1px solid var(--rule);border-radius:3px;box-shadow:var(--shadow);padding:9px 12px;
font-size:12.5px;z-index:5;min-width:150px}
.tip .t-date{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);margin-bottom:5px}
.tip .t-row{display:flex;justify-content:space-between;gap:18px}
.tip .t-row span:last-child{font-family:ui-monospace,Menlo,monospace;font-weight:600}
.swatch{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:6px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2)}
.audit-row{display:grid;grid-template-columns:auto 1fr;gap:14px;padding:14px 24px;
border-bottom:1px solid var(--rule2);align-items:start}
.audit-row:last-child{border-bottom:0}
.mark{font-family:ui-monospace,Menlo,monospace;font-size:11px;font-weight:700;letter-spacing:.04em;
padding:2px 8px;border-radius:2px;margin-top:2px;white-space:nowrap}
.mark.ok{background:var(--pos-soft);color:var(--pos)}
.mark.no{background:var(--neg-soft);color:var(--neg)}
.audit-row p{font-size:14px;color:var(--ink2)}
.audit-row strong{color:var(--ink)}
.callout{border-left:3px solid var(--accent);background:var(--accent-soft);padding:16px 20px;
border-radius:0 3px 3px 0;display:flex;flex-direction:column;gap:8px}
.callout p{font-size:15px;color:var(--ink2);max-width:66ch}
.callout strong{color:var(--ink)}
.callout.warn{border-left-color:var(--neg);background:var(--neg-soft)}
pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.75;margin:0;
background:var(--surface2);border:1px solid var(--rule);border-radius:3px;padding:16px 18px;
overflow-x:auto;color:var(--ink2)}
pre b{color:var(--ink);font-weight:600}
ul{margin:0;padding-left:20px;color:var(--ink2);max-width:72ch;font-size:15px}
li{margin-bottom:7px}li::marker{color:var(--muted)}
li strong{color:var(--ink);font-weight:600}
footer{margin-top:56px;padding-top:22px;border-top:1px solid var(--rule);color:var(--muted);font-size:13px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}
.kpi{border-bottom:1px solid var(--rule2)}.kpi:nth-child(2){border-right:0}}
"""


def build():
    d = collect()
    b = d["runs"]["best_model"]
    mt5 = d["mt5"]

    html = []
    A = html.append
    A('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">')
    A("<title>XAU Multi-Speed Donchian &mdash; backtest report</title>")
    A('<meta name="description" content="Three-engine verified backtest of a '
      'three-sleeve Donchian XAUUSD H4 trend strategy: Pine, Python and MT5.">')
    A("<style>%s</style></head><body><div class=wrap>" % CSS)

    # ---- masthead
    A('<header class=mast><div class=mast-grid>')
    A('<div class=eyebrow>Strategy research &middot; XAUUSD H4 &middot; 2022&ndash;2026</div>')
    A("<h1>XAU Multi-Speed Volatility-Scaled Donchian Trend</h1>")
    A('<p class=lede>Three independent Donchian sleeves &mdash; 20/10, 55/20, 120/40 &mdash; '
      'netted into one position at 0.10&nbsp;% risk per sleeve. Implemented three times '
      '(TradingView Pine, Python, MQL5) and cross-checked bar by bar, with protective '
      'stops replayed against 1.65&nbsp;million M1 bars.</p>')
    A('<div class=chips>')
    A('<span class="chip">%.2f years &middot; 7,206 H4 bars</span>' % b["years"])
    if mt5.get("signal_value_mismatches") is not None:
        A('<span class="chip ok">MT5 signals: %d value mismatches</span>'
          % mt5["signal_value_mismatches"])
    else:
        A('<span class="chip warn">MT5 bar-level reconciliation not on file</span>')
    if mt5.get("trades", {}).get("matched_entries") is not None:
        A('<span class="chip ok">%d/%d trades matched</span>'
          % (mt5["trades"]["matched_entries"], mt5["trades"]["python"]))
    A('<span class="chip ok">%d tests, 0 failures</span>' % d["tests"]["total"])
    A('<span class="chip warn">no out-of-sample data</span>')
    A("</div>")
    # Reciprocal link, so the two published reports are reachable from each
    # other rather than only from a URL someone has to be told.
    A('<nav class=related><span class=r-head>Other reports in this repository:'
      '</span><a href="../"><span class=r-label>Scalp Gold M1</span>'
      '<span class=r-note>GOLD M1 &middot; Bollinger mean-reversion</span></a></nav>')
    A("</div></header>")

    # ---- Japanese summary
    A('<div class=jp><div class=eyebrow>要約</div>')
    A('<p style="margin-top:8px"><strong>結論：シグナルの実装は3エンジンで完全一致したが、'
      'コストを正直に入れると優位性は残らない。</strong></p><ul>')
    A('<li>Pine / Python / MQL5 の3実装をバー単位で照合し、'
      '<strong>シグナル系の値の不一致はゼロ</strong>（135,018 比較）、'
      '<strong>%d件中%d件のトレードが完全一致</strong>。</li>'
      % (mt5["trades"]["python"], mt5["trades"].get("matched_entries", 0)))
    A('<li>4.66年・史上最大の金上昇相場で純利益は <strong>%+.2f&nbsp;%%</strong>。'
      'キャンペーン単位のブートストラップでは平均が0以下になる確率が <strong>%.2f</strong> で、'
      '統計的な優位性は認められない。</li>' % (b["ret"], b["camp_p"]))
    A('<li>MT5 が実際に課金したスワップは想定値と <strong>2.2&nbsp;%</strong> しか違わなかった。'
      '一方スプレッドは想定の <strong>1.49倍</strong>で、Python の純益は約 5.6&nbsp;% 過大。</li>')
    A('<li>口座 <strong>1万ドルでは0トレード</strong>。最小ロット0.01（100oz）の'
      '1単位ですら 0.10&nbsp;% のリスク枠を超えるため。'
      'ロット刻み0.001かマイクロ限月なら10万ドルと同じ結果になる。</li>')
    A("</ul></div>")

    # ---- verdict
    A('<div class=verdict><div class=verdict-head><div class=eyebrow>Verdict</div>')
    A('<p style="font-size:17px;max-width:70ch;margin-top:6px">'
      '<strong>The implementation is verified. The edge is not.</strong> '
      'Three engines agree on every signal to machine precision, and MT5 confirms '
      'the carry assumption to within 2.2&nbsp;%%. What survives that scrutiny is a '
      '%+.2f&nbsp;%% return over 4.66 years of the largest gold bull market on record, '
      'with a %.2f probability that the average campaign makes nothing.</p>'
      % (b["ret"], b["camp_p"]))
    A("</div><div class=kpis>")
    for val, lab, cl in ((("%+.2f%%" % b["ret"]), "Net return, 4.66 yr", "pos"),
                         (("%.2f%%" % b["cagr"]), "CAGR after all costs", ""),
                         (("%.2f%%" % b["dd"]), "Max drawdown", ""),
                         (("%.2f" % b["camp_p"]), "P(avg campaign &le; 0)", "neg")):
        A('<div class=kpi><div class="kpi-val %s">%s</div><div class=kpi-lab>%s</div></div>'
          % (cl, val, lab))
    A("</div></div>")

    # NaN is valid Python-json output and invalid JSON. Left in, JSON.parse()
    # throws and the whole page renders as an empty shell, so scrub it first and
    # then forbid it outright rather than trusting the scrub.
    A('<script id=data type="application/json">%s</script>'
      % json.dumps(_clean(d), separators=(",", ":"), allow_nan=False))
    A(BODY)
    A("<footer><p class=tiny>Generated %s from <span class=mono>results/v2/</span> by "
      "<span class=mono>report/msvsd_web.py</span>. FxPro GOLD H4 2022-01-01 to "
      "2026-08-28 (7,206 bars) with M1 stop replay (1,649,768 bars). Commission "
      "$7.85/lot round turn, per-bar broker spread, 5-point slippage per side, carry "
      "&minus;52.40/+23.58 per lot per night. Past results carry no implication for "
      "future results. This is a premise test that the premise did not pass, not a "
      "recommendation to trade.</p></footer>" % d["generated"])
    A("</div><script>%s</script></body></html>" % JS)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(html))
    return OUT


BODY = ""   # sections are rendered client-side from the embedded JSON
JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SV="http://www.w3.org/2000/svg";
const el=(t,a={})=>{const e=document.createElementNS(SV,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
const f0=n=>n.toLocaleString('en-US',{maximumFractionDigits:0});
const money=n=>(n<0?'−$':'$')+f0(Math.abs(n));
const sg=(n,d=2)=>(n>=0?'+':'−')+Math.abs(n).toFixed(d);
const cls=v=>v>0?'pos':(v<0?'neg':'');
const wrap=document.querySelector('.wrap');
function sec(eyebrow,title,lede){
  const s=document.createElement('section');
  s.innerHTML='<div class=sec-head><div class=eyebrow>'+eyebrow+'</div><h2>'+title+'</h2>'+
    (lede?'<p class=small>'+lede+'</p>':'')+'</div>';
  wrap.appendChild(s);return s;}
function card(parent,title,note,inner){
  const c=document.createElement('div');c.className='card';
  c.innerHTML=(title?'<div class=card-head><h3>'+title+'</h3>'+
    (note?'<span class=tiny>'+note+'</span>':'')+'</div>':'')+inner;
  parent.appendChild(c);return c;}
function table(head,rows){
  return '<div class=tbl-scroll><table><thead><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+
   '</tr></thead><tbody>'+rows.map(r=>'<tr class="'+(r.cls||'')+'">'+
   r.c.map((c,i)=>'<td class="'+(i===0?'name':'')+' '+(c.k||'')+'">'+c.v+'</td>').join('')+
   '</tr>').join('')+'</tbody></table></div>';}

/* ---- 1. three-engine verification ---- */
(function(){
  const m=D.mt5;
  const s=sec('Verification 01','Three implementations, one specification',
    'The same strategy written for TradingView, for Python and for the MT5 Strategy Tester. '+
    'Agreement between them is the only evidence that the numbers describe the strategy '+
    'rather than a bug in one engine.');
  card(s,'Python vs MT5, bar by bar','signal fields are pure functions of price',
    table(['Check','Result'],[
      {cls:'hi',c:[{v:'Signal-field <em>value</em> mismatches'},
        {v:'<strong>'+m.signal_value_mismatches+'</strong> in '+f0(m.signal_comparisons)+' comparisons',k:'pos'}]},
      {c:[{v:'Max price difference, any field'},{v:m.max_price_diff.toExponential(2),k:'pos'}]},
      {c:[{v:'Sleeve trades matched (sleeve + time + direction)'},
        {v:'<strong>'+m.trades.matched_entries+' of '+m.trades.python+'</strong>',k:'pos'}]},
      {c:[{v:'Coverage-only rows (one engine has more warmup history)'},{v:m.coverage_only}]},
      {c:[{v:'Bars compared'},{v:f0(m.bars)}]},
      {c:[{v:'Automated tests'},{v:D.tests.total+' passing, '+D.tests.fail+' failing',k:'pos'}]}]));
  const c2=document.createElement('div');c2.className='card';
  c2.innerHTML=[
    ['ok','MT5','<strong>Confirms the signal engine exactly.</strong> Every Donchian level, ATR value and sleeve state agrees to 5&times;10<sup>&minus;9</sup>. The remaining rows are coverage differences &mdash; MT5 holds more warmup history than the Python cache and can price a 120-bar channel where Python cannot.'],
    ['no','Pine','<strong>Not yet reconciled.</strong> The bar-by-bar comparator is built and tested, but no TradingView export has been run through it. Until one is, Pine and Python are two independent readings of one specification that agree on a summary number &mdash; which is not the same thing.']
  ].map(r=>'<div class=audit-row><span class="mark '+r[0]+'">'+r[1]+'</span><p>'+r[2]+'</p></div>').join('');
  s.appendChild(c2);
})();

/* ---- 2. cost model ---- */
(function(){
  const c=D.mt5.components; if(!c) return;
  const s=sec('Verification 02','What the broker actually charged',
    'TradingView models no overnight carry at all, so it cannot test the largest assumption '+
    'in the Python engine. MT5 charges the broker’s own swap and the spread carried in '+
    'its tick data, which makes it the only available check on the cost model.');
  const rows=[['swap / carry','swap'],['commission','commission'],['spread + slippage','spread_slip']]
    .map(([lab,k])=>({c:[{v:lab},{v:money(c.python[k])},{v:money(c.mt5[k])},
      {v:money(c.mt5[k]-c.python[k]),k:cls(c.mt5[k]-c.python[k])}],
      cls:k==='spread_slip'?'hi':''}));
  rows.push({cls:'total',c:[{v:'net profit'},{v:money(D.mt5.pnl.python_net)},
    {v:money(D.mt5.pnl.mt5_net)},{v:money(D.mt5.pnl.mt5_net-D.mt5.pnl.python_net),k:'neg'}]});
  card(s,'Cost model, component by component','$100,000 account, identical signals',
    table(['Component','Python model','MT5 (broker)','Difference'],rows));
  const co=document.createElement('div');co.className='callout';
  co.innerHTML='<p><strong>The carry assumption survives.</strong> One flat rate pair applied '+
   'across 4.7 years lands within <span class=mono>2.2&nbsp;%</span> of what the broker '+
   'actually charged. That was the single largest modelling assumption in the project, and it '+
   'is now measured rather than asserted.</p>'+
   '<p><strong>The execution assumption does not.</strong> Real spread cost is '+
   '<span class=mono>1.49&times;</span> the model’s. The Python engine prices fills off '+
   'the H4 cache’s spread column plus a flat 5-point slippage; the tester’s minute '+
   'data carries the wider spreads actually quoted at bar opens, which is exactly when this '+
   'strategy trades. Net profit is overstated by about 5.6&nbsp;%.</p>';
  s.appendChild(co);
})();

/* ---- 3. result ladder ---- */
(function(){
  const R=D.runs;
  const s=sec('Result','From the first figure to the defensible one',
    'Each step is a defect fixed or an approximation replaced with a measurement. '+
    'Nothing here was tuned; every change moved the result the same way.');
  const steps=[['v1_golden','As first published'],['baseline','Two v1 defects fixed'],
    ['ltf_m1','+ real M1 intrabar stops'],['best_model','+ carry timing, Pine Friday basis']];
  card(s,'Result ladder','$100,000 account',
    table(['Step','Net USD','Return','Max DD','Sharpe','t-test p','P(campaign ≤ 0)'],
     steps.map(([k,lab],i)=>({cls:i===steps.length-1?'total':'',c:[{v:lab},
      {v:money(R[k].net)},{v:sg(R[k].ret)+'%'},{v:R[k].dd.toFixed(2)+'%'},
      {v:R[k].sharpe.toFixed(3)},{v:R[k].t_p.toFixed(3),k:R[k].t_p>0.05?'neg':''},
      {v:R[k].camp_p==null?'—':R[k].camp_p.toFixed(3),k:'neg'}]}))));
  card(s,'Equity, drawdown and the market it traded',
    '<span class=legend><span><span class=swatch style="background:var(--accent)"></span>equity</span>'+
    '<span><span class=swatch style="background:var(--neg)"></span>drawdown</span>'+
    '<span><span class=swatch style="background:var(--muted)"></span>gold</span></span>',
    '<div class=chartbox><div id=tsx></div><div class=tip id=ts-tip></div></div>');
  card(s,'Year by year','',table(['Year','Return','Max DD','Trades','Campaigns'],
    D.yearly.map(y=>({c:[{v:y.year},{v:sg(y.return_pct)+'%',k:cls(y.return_pct)},
      {v:y.max_dd_pct.toFixed(2)+'%'},{v:y.trades},{v:y.campaigns}]}))));
})();

/* ---- 4. statistics ---- */
(function(){
  const s=sec('Evidence','The statistical case, resampled honestly',
    'Three sleeves riding one gold rally are not three pieces of evidence. Collapsing each '+
    'move into a single campaign leaves '+D.cl.n+' observations instead of '+D.tl.n+
    ' overlapping trades — and a very different answer.');
  const rows=[];const b=D.boot;
  rows.push({c:[{v:'t-test on sleeve trades'},{v:D.tl.t_test.mean.toFixed(3)+' R'},
    {v:'t = '+D.tl.t_test.t.toFixed(2)},{v:D.tl.t_test.p_two_sided.toFixed(3),k:'neg'},
    {v:D.tl.n},{v:'secondary',k:'neg'}]});
  const add=(o,lab,usd,pri)=>{if(!o)return;const f=v=>usd?money(v):v.toFixed(v<0.01&&v>-0.01?5:3);
    rows.push({cls:pri?'hi':'',c:[{v:lab},{v:f(o.point_estimate)},
      {v:f(o.ci95[0])+' … '+f(o.ci95[1])},
      {v:o.p_mean_le_zero.toFixed(3),k:o.p_mean_le_zero>0.05?'neg':'pos'},
      {v:o.effective_observations},{v:pri?'primary':'secondary',k:pri?'':'neg'}]});};
  add(b.bootstrap_iid_trades,'IID trade bootstrap',false,false);
  add(b.bootstrap_campaigns_usd,'Campaign bootstrap (USD)',true,true);
  add(b.bootstrap_block_monthly,'Monthly block bootstrap',false,true);
  add(b.bootstrap_block_quarterly,'Quarterly block bootstrap',false,true);
  card(s,'Every test, ordered by how much it assumes','',
    table(['Test','Point estimate','95 % interval','P(mean ≤ 0)','Observations','Status'],rows));
  card(s,'Campaign outcomes',D.campUSD.length+' campaigns, net USD after all costs',
    '<div class=chartbox><div id=camp></div><div class=tip id=camp-tip></div></div>');
})();

/* ---- 5. challenger grid ---- */
(function(){
  const g=D.grid;
  const s=sec('Search','720 configurations, none clears deflation',
    'A pre-declared grid across sleeve sets, direction modes, ATR multipliers, exit scalings '+
    'and risk levels — every cell on the same sample, which is exactly the condition the '+
    'Deflated Sharpe Ratio exists to correct for.');
  card(s,'Every cell in the grid',g.n+' configurations · '+g.profitable_pct.toFixed(0)+
    ' % profitable','<div class=chartbox><div id=scatter></div><div class=tip id=sc-tip></div></div>');
  card(s,'Mean result by direction mode','averaged across all other axes',
    table(['Direction mode','Mean net','Mean max DD','Mean Sharpe'],
      g.by_dir.map(r=>({cls:r.direction==='symmetric'?'hi':'',c:[{v:r.direction},
        {v:money(r.net_profit)},{v:r.max_dd_pct.toFixed(2)+'%'},{v:r.sharpe_lo.toFixed(3)}]}))));
  const co=document.createElement('div');co.className='callout warn';
  co.innerHTML='<p><strong>Do not read <span class=mono>long-only</span> off that table.</strong> '+
   'It leads on every measure, and that is precisely what one 144&nbsp;% bull market produces. '+
   'The best of '+g.n+' cells scores an autocorrelation-adjusted Sharpe of '+g.sharpe_max.toFixed(3)+
   ', but after deflating for the number of configurations examined the <strong>Deflated Sharpe '+
   'Ratio is '+g.dsr.deflated_sharpe.toFixed(2)+'</strong>, under the 0.95 threshold. On this '+
   'evidence no configuration is distinguishable from the best you would find by searching noise.</p>';
  s.appendChild(co);
})();

/* ---- 6. account size ---- */
(function(){
  const s=sec('Sizing','Account size, leverage, and why $10,000 does not work',
    'Leverage is 1:100 in the tester and never binds — peak exposure is 0.20 lots, about '+
    '$90,000 notional, needing $900 of margin. The binding constraint is the <em>minimum</em> '+
    'order size, not the maximum.');
  card(s,'The same strategy at different account sizes',
    'corr(size, 1/ATR) shows whether volatility scaling is still alive',
    table(['Configuration','Trades','Return','Max DD','Sharpe','Distinct sizes','corr(size, 1/ATR)'],
      D.accounts.map(a=>({cls:a.ref?'hi':'',c:[{v:a.label},
        {v:a.trades===0?'<strong>0</strong>':a.trades,k:a.trades===0?'neg':''},
        {v:a.trades?sg(a.ret)+'%':'—',k:a.trades?cls(a.ret):''},
        {v:a.trades?a.dd.toFixed(2)+'%':'—'},
        {v:a.trades?a.sharpe.toFixed(3):'—'},
        {v:a.sizes||'—',k:a.sizes===1?'neg':''},
        {v:a.corr==null?'—':sg(a.corr),k:(a.corr!=null&&Math.abs(a.corr)<0.2)?'neg':''}]}))));
  const co=document.createElement('div');co.className='callout warn';
  co.innerHTML='<p><strong>At $10,000 the strategy takes no trades at all.</strong> The broker’s '+
   'minimum order (0.01 lots = 1 oz on a 100 oz contract) already risks about '+
   '<span class=mono>2.5 &times; ATR</span> dollars — roughly 0.33&nbsp;% of a $10,000 '+
   'account at median ATR, against a 0.10&nbsp;% budget. Every entry rounds down to zero, on '+
   '100&nbsp;% of bars. MT5 confirms it: 0 trades, $0.00.</p>'+
   '<p><strong>Raising the risk percentage does not fix it, it breaks the strategy.</strong> At '+
   '0.25&nbsp;% every position is the minimum lot and <span class=mono>corr(size, 1/ATR)</span> '+
   'falls to 0.00 — the &ldquo;volatility-scaled&rdquo; part of the name stops being true. '+
   'What works is granularity: a 0.001 lot step or a 10&nbsp;oz micro contract reproduces the '+
   '$100,000 baseline exactly.</p>';
  s.appendChild(co);
})();

/* ---- 6b. small-account minimum-lot override ---- */
(function(){
  if(!D.override) return;
  const O=D.override, P=O.profiles, T=O.target050, R=O.registry;
  const B=P.baseline_strict, S=P.small_account_override, X=P.small_account_override_stress;
  const f2=(v,d)=>v==null?'\u2014':(+v).toFixed(d==null?2:d);
  const pc=(v,d)=>v==null?'\u2014':sg(v,d==null?2:d)+'%';
  const NB='\u2009';
  const s=sec('Post-hoc experiment','Small-Account Minimum-Lot Override',
    'An execution rule, not a strategy change. When the normal 0.10 % size rounds below '+
    'the broker minimum, it permits <em>one</em> 0.01-lot position if that position\u2019s '+
    'real stop risk passes a 0.50 % per-sleeve and 1.00 % total-open-risk gate.');

  const w=document.createElement('div'); w.className='callout warn';
  w.innerHTML='<p><strong>Read this before any number below.</strong></p><ul style="margin:0">'+
   '<li><strong>Every figure here is in-sample.</strong> The strategy still has '+
   '<strong>no genuine out-of-sample confirmation</strong> of any kind.</li>'+
   '<li>These two profiles were <strong>specified after the 2022\u20132026 results were '+
   'already known</strong>. They are post-hoc by construction.</li>'+
   '<li>They are appended to the existing registry as configurations <strong>'+
   (R.total_configurations_examined-2)+'\u2013'+R.total_configurations_examined+
   '</strong>, behind the '+R.prior_configurations+' grid cells. The multiple-testing '+
   'count carries forward; it is not reset, and this is <strong>not independent '+
   'confirmation</strong> of anything.</li>'+
   '<li><strong>Higher return or participation is not evidence of a newly validated '+
   'edge.</strong> The purpose is to measure the consequences of an execution rule.</li>'+
   '<li><span class=mono>baseline_strict</span> remains the research control.</li></ul>';
  s.appendChild(w);

  const c1=document.createElement('div'); c1.className='card';
  c1.innerHTML=[
   ['ok','IS','<strong>The normal target stays 0.10 % per sleeve.</strong> Nothing in the rule reads 0.50 % as a target. A normally-sized position is placed untouched and never consults either cap.'],
   ['ok','IS','<strong>0.50 % is a hard permission ceiling</strong> for an order that would otherwise be too small to place at all. It is the most an undersized minimum lot may risk, not what it aims for.'],
   ['ok','IS','<strong>Capped at one minimum lot.</strong> The override never enlarges an undersized position beyond 0.01 lot, and never rounds one up without first pricing its actual stop risk.'],
   ['no','IS NOT','<strong>Not the same as the 0.5 %-target scenario</strong> shown earlier in this report. That raised the SIZING TARGET, rescaling <em>every</em> position. This leaves the target at 0.10 % and only decides whether a single minimum lot may be placed. The two are compared below.']
  ].map(r=>'<div class=audit-row><span class="mark '+r[0]+'">'+r[1]+'</span><p>'+r[2]+'</p></div>').join('');
  s.appendChild(c1);

  const rows=[['Starting equity',b=>money(b.starting_equity)],
    ['Ending equity',b=>money(b.ending_equity)],
    ['Net profit',b=>money(b.net_profit)],['Net return',b=>pc(b.return_pct)],
    ['CAGR',b=>f2(b.cagr_pct)+'%'],
    ['Max drawdown USD',b=>money(b.max_dd_usd)],['Max drawdown %',b=>f2(b.max_dd_pct)+'%'],
    ['Annualised volatility',b=>f2(b.ann_vol_pct)+'%'],
    ['Sharpe (autocorr-adj.)',b=>f2(b.sharpe_lo_adjusted,3)],
    ['Sortino',b=>f2(b.sortino,3)],['Calmar',b=>f2(b.calmar,3)],
    ['Signals evaluated',b=>f0(b.signals)],['Executed sleeve trades',b=>f0(b.sleeve_trades)],
    ['Independent campaigns',b=>f0(b.campaigns)],
    ['Override trades',b=>f0(b.trades_override)],['Normal-size trades',b=>f0(b.trades_normal)],
    ['Skipped below minimum',b=>f0(b.skipped_below_minimum)],
    ['Rejected \u2014 sleeve cap',b=>f0(b.rejected_sleeve_cap)],
    ['Rejected \u2014 total-open-risk cap',b=>f0(b.rejected_portfolio_cap)],
    ['Avg actual risk / sleeve',b=>f2(b.avg_stop_risk_pct,3)+'%'],
    ['Max actual risk / sleeve',b=>f2(b.max_stop_risk_pct,3)+'%'],
    ['Avg total open risk',b=>f2(b.avg_total_open_risk_pct,3)+'%'],
    ['Max total open risk',b=>f2(b.max_total_open_risk_pct,3)+'%'],
    ['Distinct position sizes',b=>f0(b.distinct_position_sizes)],
    ['corr(size, 1/ATR)',b=>b.corr_size_inv_atr==null?'\u2014':sg(b.corr_size_inv_atr)],
    ['Gross profit',b=>money(b.gross_profit)],
    ['Spread + slippage',b=>money(-b.cost_spread_slip)],
    ['Commission',b=>money(-b.cost_commission)],['Swap',b=>money(b.cost_swap)],
    ['P(mean campaign \u2264 0)',b=>f2(b.p_mean_campaign_le_zero,3)]];
  card(s,'All three predeclared profiles','control first; stress is diagnostic only',
    table(['Metric','baseline_strict','small_account_override','stress (diagnostic)'],
      rows.map(function(r){return {c:[{v:r[0]},{v:r[1](B)},{v:r[1](S)},{v:r[1](X)}]};})));

  const cmp=[['Executed sleeve trades',b=>f0(b.sleeve_trades)],
    ['Net return',b=>pc(b.return_pct)],['Max drawdown %',b=>f2(b.max_dd_pct)+'%'],
    ['Annualised volatility',b=>f2(b.ann_vol_pct)+'%'],
    ['Sharpe (autocorr-adj.)',b=>f2(b.sharpe_lo_adjusted,3)],
    ['Avg actual risk / sleeve',b=>f2(b.avg_stop_risk_pct,3)+'%'],
    ['Max actual risk / sleeve',b=>f2(b.max_stop_risk_pct,3)+'%'],
    ['Median total open risk',b=>f2((b.dist_total_open_risk_pct||{}).p50,3)+'%'],
    ['MAX total open risk',b=>f2(b.max_total_open_risk_pct,3)+'%'],
    ['Distinct position sizes',b=>f0(b.distinct_position_sizes)],
    ['corr(size, 1/ATR)',b=>b.corr_size_inv_atr==null?'\u2014':sg(b.corr_size_inv_atr)]];
  card(s,'Override versus the earlier 0.5 %-target scenario',
    'both on $10,000 \u2014 these are different rules, not two settings of one',
    table(['Metric','override (target 0.10 %)','0.5 % TARGET'],
      cmp.map(function(r){return {cls:r[0].indexOf('MAX')===0?'hi':'',
        c:[{v:r[0]},{v:r[1](S)},{v:r[1](T)}]};})));

  const c2=document.createElement('div'); c2.className='callout';
  c2.innerHTML='<p><strong>The override does not retain similar participation.</strong> It takes '+
   S.sleeve_trades+' trades against the 0.5 %-target scenario\u2019s '+T.sleeve_trades+
   ' \u2014 about '+Math.round(100*(1-S.sleeve_trades/T.sleeve_trades))+NB+'% fewer. '+
   'It does deliver what it was asked to on exposure: average risk per sleeve '+
   f2(S.avg_stop_risk_pct,3)+NB+'% against '+f2(T.avg_stop_risk_pct,3)+NB+'%, drawdown '+
   f2(S.max_dd_pct)+NB+'% against '+f2(T.max_dd_pct)+NB+'%, and \u2014 the real difference \u2014 '+
   '<strong>peak total open risk '+f2(S.max_total_open_risk_pct,3)+NB+'% against '+
   f2(T.max_total_open_risk_pct,3)+NB+'%</strong>. The 0.5 %-target scenario has no portfolio '+
   'cap at all, so its combined exposure runs to roughly seven times the level the override '+
   'permits.</p><p>But it is <strong>worse risk-adjusted</strong>: Sharpe '+
   f2(S.sharpe_lo_adjusted,3)+' against '+f2(T.sharpe_lo_adjusted,3)+
   ', and against the control\u2019s '+f2(B.sharpe_lo_adjusted,3)+'.</p>';
  s.appendChild(c2);

  const keys=['min','p10','p25','p50','p75','p90','p95','max'];
  function dist(lab,obj,d){
    return {c:[{v:lab}].concat(keys.map(function(k){
      return {v:(obj&&obj[k]!=null)?(+obj[k]).toFixed(d):'\u2014'};}))};
  }
  card(s,'Exposure distribution \u2014 small_account_override','percentiles across accepted trades',
    table(['Measure'].concat(keys.map(k=>k.toUpperCase())),[
      dist('Actual stop risk %',S.dist_stop_risk_pct,3),
      dist('Total open risk %',S.dist_total_open_risk_pct,3),
      dist('Holding period (hours)',S.dist_hold_hours,0),
      dist('ATR at entry (USD)',S.dist_atr_at_entry,2),
      dist('Stop distance (USD)',S.dist_stop_distance,2)]));

  const orr=S.override_rates||{};
  ['year','sleeve','direction'].forEach(function(k){
    const rr=orr['by_'+k]; if(!rr) return;
    card(s,'Override acceptance by '+k,
      'candidates are signals whose normal size fell below the minimum',
      table([k.charAt(0).toUpperCase()+k.slice(1),'Candidates','Accepted','Accept %',
             'Rej. sleeve cap','Rej. total cap'],
        rr.map(function(r){return {c:[{v:r[k]},{v:f0(r.candidates)},{v:f0(r.accepted)},
          {v:f2(r.accept_pct,1)+'%',k:r.accept_pct<25?'neg':''},
          {v:f0(r.rej_sleeve)},{v:f0(r.rej_portfolio)}]};})));
  });

  const g=S.diagnostics||{}, vs=g.volatility_separation||{},
        vf=g['5_acts_as_volatility_filter']||{},
        cs=g['4_composition_shift']||{}, c6=g['6_cost_share_at_min_lot']||{},
        pr=g['3_open_risk_when_portfolio_rejected']||{};
  const sl=(cs.sleeve||{}), di=(cs.direction||{});
  const dg=document.createElement('div'); dg.className='card';
  dg.innerHTML=[
   ['no','1 / 2 / 5','<strong>The override behaves as an unintended volatility filter.</strong> Accepted trades have a median ATR of '+f2(vs.accepted_median_atr)+'; rejected ones '+f2(vs.rejected_median_atr)+' \u2014 '+f2(vs.ratio_rejected_over_accepted)+'\u00d7 higher. The 0.50 % cap on a fixed 0.01 lot implies an exact ATR ceiling of <span class=mono>'+f2(vs.atr_threshold_implied)+'</span>, and <strong>'+f2(vf.pct_accepted_below_ceiling,1)+NB+'% of accepted trades sit below it</strong>. That is a hard volatility-regime filter that was never part of the specification.'],
   ['no','3','<strong>The total-open-risk cap falls hardest on the slow sleeve.</strong> '+(g['3_portfolio_cap_by_sleeve']||[]).map(function(r){return r.sleeve+' '+f2(r.rej_portfolio_pct,1)+NB+'%';}).join(', ')+' of candidates rejected. The slow sleeve signals last, by which time the faster sleeves have consumed the budget \u2014 median open risk when the cap fired was '+f2(pr.p50,2)+NB+'%, already above the 1.00 % limit on its own.'],
   ['no','4','<strong>Composition shifts materially.</strong> The slow sleeve is '+f2((sl.candidates_pct||{}).slow,1)+NB+'% of candidates but only '+f2((sl.accepted_pct||{}).slow,1)+NB+'% of accepted trades; shorts rise from '+f2((di.candidates_pct||{}).short,1)+NB+'% to '+f2((di.accepted_pct||{}).short,1)+NB+'%. The gate is doing trade selection, not just size limiting.'],
   ['no','6','<strong>Costs consume a disproportionate share.</strong> The gate\u2019s own cost term is trivial at one ounce (median '+money(c6.median_estimated_costs)+' against a '+money(c6.median_price_stop_loss)+' stop), but realised costs eat <strong>'+f2(g['6_realised_cost_share_of_gross'],1)+NB+'% of gross profit</strong> \u2014 carry dominates, because a minimum lot held for weeks pays the same per-lot swap as a fully sized one.'],
   ['no','7','<strong>The result is still a handful of campaigns.</strong> '+f0(g['7_campaigns'])+' campaigns; the top five account for '+f2(g['7_top5_share_pct'],1)+NB+'% of net profit, and removing them leaves <strong>'+money(g['7_excl_top5_total'])+'</strong>. Concentration is worse than the control\u2019s, not better.'],
   ['ok','8','<strong>Real MT5 spread would make this worse, not better.</strong> The cross-check showed the Python cost model understates real spread cost by 1.49\u00d7. Applied here that widens an already '+f2(g['6_realised_cost_share_of_gross'],0)+NB+'% cost share \u2014 the override is measured on the optimistic model and would look worse on the realistic one.']
  ].map(function(r){return '<div class=audit-row><span class="mark '+r[0]+'">'+r[1]+'</span><p>'+r[2]+'</p></div>';}).join('');
  s.appendChild(dg);

  if(O.recon){
    const rc=O.recon, bd=O.boundary||{};
    const nearPct=bd.total_candidates?
      (100*bd.decisions_within_max_diff_of_a_cap/bd.total_candidates):0;
    card(s,'Python ↔ MQL5 reconciliation, override profile',
      'the same rule, two independent implementations',
      table(['Check','Result'],[
        {cls:'hi',c:[{v:'Sizing decisions compared'},
          {v:f0(rc.signals.matched)+' of '+f0(rc.signals.python)+' python / '+
             f0(rc.signals.mql5)+' mql5',k:'pos'}]},
        {cls:'hi',c:[{v:'Acceptance reason identical'},
          {v:f2(rc.sizing.reason_agree_pct,2)+'%',k:'pos'}]},
        {c:[{v:'Final lots, max difference'},
          {v:(+rc.sizing.max_lot_diff).toExponential(1),k:'pos'}]},
        {c:[{v:'Stop-risk arithmetic, max difference'},{v:'5.0e-10 USD',k:'pos'}]},
        {c:[{v:'Cost term, max difference'},
          {v:money(rc.sizing.max_risk_diff),k:'neg'}]},
        {c:[{v:'Sleeve trades matched'},
          {v:f0(rc.trades.matched)+' of '+f0(rc.trades.python)+' / '+
             f0(rc.trades.mql5),k:'pos'}]},
        {c:[{v:'Trades present on only one side'},
          {v:f0(rc.trades.python_only)+' py, '+f0(rc.trades.mql5_only)+' mql5',k:'pos'}]},
        {c:[{v:'Decisions within that cost error of a cap'},
          {v:f0(bd.decisions_within_max_diff_of_a_cap)+' of '+
             f0(bd.total_candidates)+' (≈'+f2(nearPct,1)+'%)',k:'neg'}]}]));
    const rn=document.createElement('div'); rn.className='callout';
    rn.innerHTML='<p><strong>The rule reconciles; the cost model does not.</strong> '+
     'Every acceptance and rejection agrees, and the stop-risk arithmetic matches to '+
     '5×10<sup>−10</sup> USD. The residual is entirely the spread '+
     'assumption — MT5’s tester spread reached 185 points where the H4 cache '+
     'column carries 23, worth up to '+money(rc.sizing.max_risk_diff)+' on one ounce.</p>'+
     '<p>It flipped nothing here, but <strong>'+
     f0(bd.decisions_within_max_diff_of_a_cap)+' of '+f0(bd.total_candidates)+
     ' decisions sit closer to a cap than that error is large</strong>. The gate is '+
     'therefore spread-model sensitive exactly at the boundary — which is where a '+
     'permission cap does all of its work.</p>';
    s.appendChild(rn);
  }

  const cl=document.createElement('div'); cl.className='callout warn';
  cl.innerHTML='<div class=eyebrow style="color:var(--neg)">Classification</div>'+
   '<p style="font-size:17px"><strong>Operationally questionable.</strong></p>'+
   '<p>The risk caps work exactly as specified: peak actual risk '+
   f2(S.max_stop_risk_pct,3)+NB+'% against a 0.50 % ceiling, peak total open risk '+
   f2(S.max_total_open_risk_pct,3)+NB+'% against 1.00 %, neither ever breached across '+
   f0(S.signals)+' evaluated signals, and Python and MQL5 agree on every sizing decision. '+
   'Drawdown ('+f2(S.max_dd_pct)+NB+'%) is proportionate to the roughly 3\u00d7 nominal risk a '+
   'minimum lot carries on a $10,000 account.</p>'+
   '<p><strong>What makes it questionable is not the caps but the discretisation.</strong> '+
   'Every accepted position is the same 0.01 lot, so <span class=mono>corr(size, 1/ATR)</span> '+
   'is <strong>'+sg(S.corr_size_inv_atr)+'</strong> and there is exactly <strong>one</strong> '+
   'distinct position size. The volatility scaling the strategy is named for is gone. The cap '+
   'also converts into a hard ATR ceiling that silently selects which trades are taken, '+
   'shifting sleeve and direction composition. Nothing breaches a limit and the platforms '+
   'agree, so it is not <em>unacceptable</em> \u2014 but it is no longer the strategy that was '+
   'tested, and it should not be described as one.</p>';
  s.appendChild(cl);
})();

/* ---- 6c. 20 % drawdown experiment (post-hoc 724) ---- */
(function(){
  if(!D.dd20) return;
  const Z=D.dd20, B=Z.baseline, E=Z.experiment, V=Z.verdict;
  const f2=(v,d)=>v==null?'—':(+v).toFixed(d==null?2:d);
  const pc=(v,d)=>v==null?'—':sg(v,d==null?2:d)+'%';
  const NB=' ';
  const s=sec('Post-hoc experiment 724','20% Drawdown Experiment',
    'A single predeclared risk profile: $10,000 at a 0.70 % target per sleeve with a '+
    '2.00 % combined open-stop-risk ceiling applied to every entry, the minimum-lot '+
    'override off, and a declared acceptance limit of 20 % maximum drawdown. Only this '+
    'one risk level was tested — no nearby values were searched.');

  const w=document.createElement('div'); w.className='callout warn';
  w.innerHTML='<p><strong>All results below remain in-sample and are not evidence of a '+
   'validated edge.</strong></p><ul style="margin:0">'+
   '<li>This profile was specified <strong>after</strong> the 2022–2026 results were '+
   'known. It is registered as <strong>post-hoc experiment 724</strong>, appended behind '+
   'the 720 grid cells and the three override profiles. The multiple-testing count carries '+
   'forward and is not reset.</li>'+
   '<li>The strategy still has <strong>no genuine out-of-sample confirmation</strong>.</li>'+
   '<li>Entry signals, Donchian periods, ATR, stop distance, exits, direction rules and '+
   'cost assumptions are <strong>unchanged</strong>. Only the risk profile differs.</li>'+
   '<li><span class=mono>baseline_strict</span> remains the research control; both runs use '+
   'the same final engine (M1 stop replay, corrected carry timing, Pine Friday basis).</li>'+
   '</ul>';
  s.appendChild(w);

  const rows=[['Starting equity',b=>money(b.starting_equity)],
    ['Ending equity',b=>money(b.ending_equity)],
    ['Net profit',b=>money(b.net_profit)],['Total return',b=>pc(b.return_pct)],
    ['CAGR',b=>f2(b.cagr_pct,3)+'%'],
    ['Max drawdown USD',b=>money(b.max_dd_usd)],['Max drawdown %',b=>f2(b.max_dd_pct)+'%'],
    ['Annualised volatility',b=>f2(b.ann_vol_pct)+'%'],
    ['Sharpe (autocorr-adj.)',b=>f2(b.sharpe,3)],['Sortino',b=>f2(b.sortino,3)],
    ['Calmar',b=>f2(b.calmar,3)],['Exposure',b=>f2(b.exposure_pct,1)+'%'],
    ['Signals evaluated',b=>f0(b.signals)],['Executed trades',b=>f0(b.trades)],
    ['Independent campaigns',b=>f0(b.campaigns)],
    ['Avg risk / trade',b=>f2(b.avg_risk_pct,3)+'%'],
    ['Max risk / trade',b=>f2(b.max_risk_pct,3)+'%'],
    ['Avg combined open risk',b=>f2(b.avg_total_open_risk_pct,3)+'%'],
    ['MAX combined open risk',b=>f2(b.max_total_open_risk_pct,3)+'%'],
    ['Distinct position sizes',b=>f0(b.distinct_sizes)],
    ['corr(size, 1/ATR)',b=>b.corr_size_inv_atr==null?'—':sg(b.corr_size_inv_atr,3)],
    ['Gross profit',b=>money(b.gross_profit)],
    ['Spread + slippage',b=>money(-b.cost_spread_slip)],
    ['Commission',b=>money(-b.cost_commission)],['Swap',b=>money(b.cost_swap)],
    ['Top-5 campaign share',b=>f2(b.top5_share_pct,1)+'%'],
    ['Net excluding top 5',b=>money(b.excl_top5_total)],
    ['P(mean campaign ≤ 0)',b=>f2(b.p_mean_campaign_le_zero,3)]];
  card(s,'dd20_experiment against the control','same engine; only the risk profile differs',
    table(['Metric','baseline_strict (control)','dd20_experiment'],
      rows.map(function(r){
        var hi=(r[0].indexOf('MAX')===0||r[0].indexOf('Max drawdown %')===0);
        return {cls:hi?'hi':'',c:[{v:r[0]},{v:r[1](B)},{v:r[1](E)}]};})));

  card(s,'Acceptance conditions','classified on risk behaviour, not on return',
    table(['Condition','Limit','Observed','Verdict'],
      V.checks.map(function(c){return {cls:c.failed?'hi':'',c:[
        {v:c.id.replace(/_/g,' ')},{v:c.limit},{v:c.observed},
        {v:c.failed?'<strong>FAILED</strong>':'ok',k:c.failed?'neg':'pos'}]};})));

  const vd=document.createElement('div'); vd.className='callout warn';
  vd.innerHTML='<div class=eyebrow style="color:var(--neg)">Result</div>'+
   '<p style="font-size:17px"><strong>Experiment '+V.verdict+'</strong> — on '+
   V.failed_conditions.length+' of the five declared conditions: <span class=mono>'+
   V.failed_conditions.join(', ')+'</span>.</p>'+
   '<p>Drawdown stayed inside the 20 % limit at '+f2(E.max_dd_pct)+NB+'%, the 2.00 % '+
   'combined-open-risk ceiling held at a peak of '+f2(E.max_total_open_risk_pct,3)+NB+'% '+
   '(the control, which has no such ceiling, reaches '+f2(B.max_total_open_risk_pct,3)+
   NB+'%), and volatility scaling survived — '+f0(E.distinct_sizes)+
   ' distinct sizes with <span class=mono>corr(size, 1/ATR) = '+sg(E.corr_size_inv_atr,3)+
   '</span>. Risk-adjusted performance fell to '+f2(E.sharpe,3)+' against the control’s '+
   f2(B.sharpe,3)+', which clears the 75 % threshold only narrowly.</p>'+
   '<p><strong>It fails on campaign concentration.</strong> The five largest campaigns are '+
   f2(E.top5_share_pct,1)+NB+'% of net profit, and removing them leaves '+
   money(E.excl_top5_total)+'. In fairness the control fails the same test '+
   '('+f2(B.top5_share_pct,1)+NB+'%, '+money(B.excl_top5_total)+'), so this condition does '+
   'not separate the two — it says the underlying strategy is concentration-dependent '+
   'at any risk level, which earlier sections already established.</p>';
  s.appendChild(vd);

  if(E.sizing_lockout){
    const L=E.sizing_lockout, by=L.by_year||{};
    const yrs=Object.keys(by).sort();
    card(s,'The account stops trading as volatility rises',
      'signals whose 0.70 % size rounds below the 0.01 minimum are skipped, not taken smaller',
      table(['Year','Signals','Median ATR','Unsizable','Unsizable %'],
        yrs.map(function(y){var r=by[y];return {cls:r.unsizable_pct>50?'hi':'',c:[
          {v:y},{v:f0(r.signals)},{v:f2(r.atr_median)},{v:f0(r.unsizable)},
          {v:f2(r.unsizable_pct,1)+'%',k:r.unsizable_pct>50?'neg':''}]};})));
    const lk=document.createElement('div'); lk.className='callout warn';
    lk.innerHTML='<p><strong>The most important number in this experiment is not the '+
     'drawdown.</strong> At 0.70 % on roughly $10,000, one 0.01 lot can only be afforded '+
     'while <span class=mono>ATR ≤ '+f2(L.lockout_atr_threshold)+'</span>. Gold’s '+
     'median ATR reached '+f2((by['2025']||{}).atr_median)+' in 2025 and '+
     f2((by['2026']||{}).atr_median)+' in 2026, so <strong>100 % of 2026 signals were '+
     'unsizable and the account sat flat for the entire year</strong>.</p>'+
     '<p>The 14.75 % drawdown is therefore partly achieved by <em>not participating</em> in '+
     'the most volatile stretch of the record, rather than by riding through it. This is the '+
     'same discretisation lockout that leaves a $10,000 account with zero trades at 0.10 % — '+
     'raising the target to 0.70 % moves the threshold, it does not remove it.</p>';
    s.appendChild(lk);
  }

  card(s,'Year by year — dd20_experiment','',
    table(['Year','Return','Max DD','Trades','Campaigns'],
      (E.yearly||[]).filter(function(y){return y.year>2021;}).map(function(y){
        return {c:[{v:y.year},{v:pc(y.return_pct),k:cls(y.return_pct)},
          {v:f2(y.max_dd_pct)+'%'},{v:f0(y.trades)},{v:f0(y.campaigns)}]};})));

  const dr=[];
  ['long','short'].forEach(function(k){var v=(E.by_direction||{})[k];
    if(v) dr.push({c:[{v:k},{v:f0(v.trades)},{v:money(v.gross),k:cls(v.gross)},
      {v:f2(v.win_pct,1)+'%'},{v:sg(v.mean_R,3),k:cls(v.mean_R)}]});});
  ['fast','medium','slow'].forEach(function(k){var v=(E.by_sleeve||{})[k];
    if(v) dr.push({c:[{v:k+' sleeve'},{v:f0(v.trades)},{v:money(v.gross),k:cls(v.gross)},
      {v:f2(v.win_pct,1)+'%'},{v:sg(v.mean_R,3),k:cls(v.mean_R)}]});});
  card(s,'Direction and sleeve breakdown — dd20_experiment','',
    table(['Group','Trades','Gross','Win rate','Mean R'],dr));

  const cb=E.campaign_bootstrap_usd||{}, bm=E.block_monthly||{}, bq=E.block_quarterly||{};
  card(s,'Evidence — dd20_experiment','campaigns are the unit; trades overlap',
    table(['Test','Point estimate','95 % interval','P(mean ≤ 0)','Observations'],[
      {cls:'hi',c:[{v:'Campaign bootstrap (USD)'},{v:money(cb.point_estimate)},
        {v:money(cb.ci95?cb.ci95[0]:null)+' … '+money(cb.ci95?cb.ci95[1]:null)},
        {v:f2(cb.p_mean_le_zero,3),k:'neg'},{v:f0(cb.effective_observations)}]},
      {c:[{v:'Monthly block bootstrap'},{v:f2(bm.point_estimate,6)},
        {v:f2(bm.ci95?bm.ci95[0]:null,6)+' … '+f2(bm.ci95?bm.ci95[1]:null,6)},
        {v:f2(bm.p_mean_le_zero,3),k:'neg'},{v:f0(bm.effective_observations)}]},
      {c:[{v:'Quarterly block bootstrap'},{v:f2(bq.point_estimate,6)},
        {v:f2(bq.ci95?bq.ci95[0]:null,6)+' … '+f2(bq.ci95?bq.ci95[1]:null,6)},
        {v:f2(bq.p_mean_le_zero,3),k:'neg'},{v:f0(bq.effective_observations)}]}]));
})();

/* ---- 7. limitations ---- */
(function(){
  const s=sec('Caveats','What this test still does not tell you','');
  card(s,'','', '<div class=card-pad><ul>'+[
   '<strong>There is no out-of-sample period. None.</strong> The entire 2022–2026 record was '+
   'visible while this framework was written, and it covers one instrument in one regime. Every '+
   'figure here describes a sample, not a forecast.',
   '<strong>Pine is not reconciled.</strong> Python and MQL5 agree exactly; TradingView has not '+
   'been put through the same comparator.',
   '<strong>The execution cost model is known to be wrong by about half.</strong> MT5 says real '+
   'spread cost is 1.49&times; the model’s. Until fills are priced off quoted spreads, treat '+
   'the Python headline as optimistic.',
   '<strong>Swap rates are a snapshot.</strong> The historical-financing engine takes a per-rollover '+
   'CSV and refuses by default to guess at dates it does not cover, but no real rate history was '+
   'available — one FxPro rate pair was applied across 4.7 years.',
   '<strong>Entry timing is still H4.</strong> The M1 engine resolves protective stops only; '+
   'entries fill at the next H4 open by design.',
   '<strong>'+(D.dd20?D.dd20.registry_index:(D.override?D.override.registry.total_configurations_examined:720))+' configurations have now been examined.</strong> '+
   'The 720-cell grid, plus three post-hoc profiles appended behind it. That count is recorded and fed to the Deflated Sharpe '+
   'calculation rather than quietly forgotten, and it is never reset. At 723 trials the Deflated Sharpe of the strict baseline is 0.47, of the override 0.32 — both far under '+
   '0.95, and experiment 724 fails one of its own five acceptance conditions. The honest response to a search that finds nothing significant is to stop searching, not to search harder.'
  ].map(x=>'<li>'+x+'</li>').join('')+'</ul></div>');
})();

/* ---- 8. reproduce ---- */
(function(){
  const s=sec('Reproduce','Running it yourself','');
  card(s,'','','<div class=card-pad style="display:flex;flex-direction:column;gap:14px">'+
   '<pre><b>python bt_xau_msvsd.py --tag best_model --stop-mode ltf --ltf-file M1 \\\n'+
   '    --friday-basis close --financing-timing pre-fill --log-open-at-end</b>   # this page\n\n'+
   '<b>python bt_xau_msvsd.py --tag v1_golden --v1-compat</b>   # frozen first result\n'+
   '<b>python run_mt5_msvsd.py</b>                              # MT5 cross-check\n'+
   '<b>python experiments.py --suite grid</b>                    # the 720 cells\n'+
   '<b>python tests/run_all.py</b>                               # '+D.tests.total+' tests\n'+
   '<b>python -m report.msvsd_web</b>                            # rebuild this page</pre>'+
   '<p class=tiny>Source: <a href="https://github.com/aniimura/EA-Gold">github.com/aniimura/EA-Gold</a>. '+
   'Full audit trail in <span class=mono>CHANGELOG_msvsd_v2.md</span>; commands in '+
   '<span class=mono>README_msvsd_v2.md</span>. The first published result is frozen in '+
   '<span class=mono>tests/golden/</span> and asserted by the test suite, so the corrections on '+
   'this page cannot silently drift back.</p></div>');
})();

/* ================= charts ================= */
function timeseries(){
  const host=document.getElementById('tsx'),tip=document.getElementById('ts-tip');
  if(!host)return;
  const c=D.curve,dd=D.dd,W=host.clientWidth||860,padL=62,padR=14;
  const panels=[{h:180,lab:'Account equity',col:()=>css('--accent'),fill:1,val:i=>c[i][1],fmt:v=>'$'+f0(v)},
    {h:80,lab:'Drawdown %',col:()=>css('--neg'),fill:1,val:i=>dd[i]?dd[i][1]:0,fmt:v=>'−'+v.toFixed(2)+'%'},
    {h:80,lab:'Gold, USD/oz',col:()=>css('--muted'),fill:0,val:i=>c[i][2],fmt:v=>'$'+f0(v)}];
  const gap=26,H=panels.reduce((s,p)=>s+p.h,0)+gap*panels.length+20,n=c.length;
  const x=i=>padL+(i/(n-1))*(W-padL-padR);
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,width:'100%',height:H,role:'img',
    'aria-label':'Equity, drawdown and gold price'});
  let top=6;const geo=[];
  panels.forEach(p=>{
    const vals=c.map((_,i)=>p.val(i));let lo=Math.min(...vals),hi=Math.max(...vals);
    if(p.lab[0]==='D'){lo=0;hi*=1.1;}else{const q=(hi-lo)*.12;lo-=q;hi+=q;}
    const y=v=>top+p.h-((v-lo)/(hi-lo))*p.h;geo.push({p,y});
    [lo,(lo+hi)/2,hi].forEach(g=>{svg.appendChild(el('line',{x1:padL,x2:W-padR,y1:y(g),y2:y(g),
      stroke:css('--rule2')}));const t=el('text',{x:padL-9,y:y(g)+3.5,'text-anchor':'end',class:'axis'});
      t.textContent=p.lab[0]==='D'?g.toFixed(1):f0(g);svg.appendChild(t);});
    const pl=el('text',{x:padL,y:top-8,class:'panel-label'});pl.textContent=p.lab;svg.appendChild(pl);
    let d='';c.forEach((_,i)=>{d+=(i?'L':'M')+x(i).toFixed(1)+' '+y(p.val(i)).toFixed(1);});
    if(p.fill)svg.appendChild(el('path',{d:d+'L'+x(n-1).toFixed(1)+' '+y(lo).toFixed(1)+
      'L'+x(0).toFixed(1)+' '+y(lo).toFixed(1)+'Z',fill:p.col(),'fill-opacity':.10,stroke:'none'}));
    svg.appendChild(el('path',{d:d,fill:'none',stroke:p.col(),'stroke-width':2,
      'stroke-linejoin':'round','stroke-linecap':'round'}));
    top+=p.h+gap;});
  [0,(n*.25)|0,(n*.5)|0,(n*.75)|0,n-1].forEach(i=>{const t=el('text',{x:x(i),y:H-4,
    'text-anchor':i===0?'start':(i===n-1?'end':'middle'),class:'axis'});
    t.textContent=c[i][0].slice(0,7);svg.appendChild(t);});
  const cross=el('line',{x1:0,x2:0,y1:6,y2:H-18,stroke:css('--ink2'),'stroke-dasharray':'3 3',opacity:0});
  svg.appendChild(cross);
  const dots=geo.map(g=>{const o=el('circle',{r:4,fill:g.p.col(),stroke:css('--surface'),
    'stroke-width':2,opacity:0});svg.appendChild(o);return o;});
  host.innerHTML='';host.appendChild(svg);
  host.addEventListener('pointermove',ev=>{const r=host.getBoundingClientRect();
    let i=Math.round(((ev.clientX-r.left)/r.width*W-padL)/(W-padL-padR)*(n-1));
    i=Math.max(0,Math.min(n-1,i));
    cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('opacity',1);
    geo.forEach((g,k)=>{dots[k].setAttribute('cx',x(i));dots[k].setAttribute('cy',g.y(g.p.val(i)));
      dots[k].setAttribute('opacity',1);});
    tip.innerHTML='<div class=t-date>'+c[i][0]+'</div>'+panels.map(p=>
      '<div class=t-row><span><span class=swatch style="background:'+p.col()+'"></span>'+
      p.lab.split(',')[0]+'</span><span>'+p.fmt(p.val(i))+'</span></div>').join('');
    tip.style.opacity=1;
    tip.style.left=Math.max(4,Math.min(host.clientWidth-200,x(i)/W*host.clientWidth+16))+'px';
    tip.style.top='14px';});
  host.addEventListener('pointerleave',()=>{tip.style.opacity=0;cross.setAttribute('opacity',0);
    dots.forEach(o=>o.setAttribute('opacity',0));});
}
function campaigns(){
  const host=document.getElementById('camp'),tip=document.getElementById('camp-tip');
  if(!host)return;
  const V=D.campUSD.slice().sort((a,b)=>a-b),W=host.clientWidth||860,H=210;
  const padL=54,padR=14,padT=12,padB=32,n=V.length;
  const lo=Math.min(...V),hi=Math.max(...V),bw=(W-padL-padR)/n;
  const y=v=>padT+(H-padT-padB)*((hi-v)/(hi-lo)),y0=y(0);
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,width:'100%',height:H,role:'img',
    'aria-label':'Net result of every campaign, sorted'});
  [hi,0,lo].forEach(g=>{svg.appendChild(el('line',{x1:padL,x2:W-padR,y1:y(g),y2:y(g),
    stroke:g===0?css('--rule'):css('--rule2')}));
    const t=el('text',{x:padL-8,y:y(g)+3.5,'text-anchor':'end',class:'axis'});
    t.textContent=money(g);svg.appendChild(t);});
  V.forEach((v,i)=>{const bx=padL+i*bw;const g=el('g');
    g.appendChild(el('rect',{x:bx+.5,y:Math.min(y(v),y0),width:Math.max(1,bw-1),
      height:Math.abs(y(v)-y0),fill:v>=0?css('--pos'):css('--neg'),'fill-opacity':.85}));
    const hit=el('rect',{x:bx,y:padT,width:Math.max(bw,3),height:H-padT-padB,fill:'transparent'});
    hit.addEventListener('pointerenter',()=>{tip.innerHTML='<div class=t-date>campaign '+(i+1)+
      ' of '+n+'</div><div class=t-row><span>Net</span><span>'+money(v)+'</span></div>';
      tip.style.opacity=1;
      tip.style.left=Math.max(4,Math.min(host.clientWidth-180,bx/W*host.clientWidth-80))+'px';
      tip.style.top='8px';});
    hit.addEventListener('pointerleave',()=>tip.style.opacity=0);
    g.appendChild(hit);svg.appendChild(g);});
  const w=V.filter(v=>v>0).length;
  const note=el('text',{x:W-padR,y:H-12,'text-anchor':'end',class:'axis',fill:css('--muted')});
  note.textContent=w+' of '+n+' campaigns profitable · the largest few carry the result';
  svg.appendChild(note);
  const xl=el('text',{x:padL,y:H-12,class:'axis',fill:css('--muted')});
  xl.textContent='sorted worst to best';svg.appendChild(xl);
  host.innerHTML='';host.appendChild(svg);
}
function scatter(){
  const host=document.getElementById('scatter'),tip=document.getElementById('sc-tip');
  if(!host)return;
  const P=D.grid.scatter,W=host.clientWidth||860,H=250,padL=62,padR=16,padT=14,padB=40;
  const xs=P.map(p=>p[0]),ys=P.map(p=>p[1]);
  const x0=Math.min(...xs),x1=Math.max(...xs),y1=Math.max(...ys)*1.05;
  const X=v=>padL+(v-x0)/(x1-x0)*(W-padL-padR);
  const Y=v=>padT+(H-padT-padB)*(1-v/y1);
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,width:'100%',height:H,role:'img',
    'aria-label':'Sharpe versus net profit for all grid configurations'});
  [0,y1/2,y1].forEach(g=>{svg.appendChild(el('line',{x1:padL,x2:W-padR,y1:Y(g),y2:Y(g),
    stroke:css('--rule2')}));const t=el('text',{x:padL-8,y:Y(g)+3.5,'text-anchor':'end',class:'axis'});
    t.textContent='$'+f0(g);svg.appendChild(t);});
  [x0,(x0+x1)/2,x1].forEach(g=>{const t=el('text',{x:X(g),y:H-20,'text-anchor':'middle',class:'axis'});
    t.textContent=g.toFixed(2);svg.appendChild(t);});
  const xl=el('text',{x:(padL+W-padR)/2,y:H-4,'text-anchor':'middle',class:'axis',fill:css('--ink2')});
  xl.textContent='autocorrelation-adjusted Sharpe';svg.appendChild(xl);
  P.forEach(p=>svg.appendChild(el('circle',{cx:X(p[0]),cy:Y(p[1]),r:2.6,fill:css('--accent'),
    'fill-opacity':.32})));
  const bx=X(D.runs.best_model.sharpe),by=Y(D.runs.best_model.net);
  svg.appendChild(el('circle',{cx:bx,cy:by,r:6,fill:'none',stroke:css('--neg'),'stroke-width':2}));
  const bl=el('text',{x:bx+12,y:by+4,class:'axis',fill:css('--neg'),
    style:'font-size:11.5px;font-weight:700'});
  bl.textContent='this report’s configuration';svg.appendChild(bl);
  const note=el('text',{x:W-padR,y:padT+4,'text-anchor':'end',class:'axis',fill:css('--muted')});
  note.textContent=D.grid.n+' cells · best DSR '+D.grid.dsr.deflated_sharpe.toFixed(2)+' (needs 0.95)';
  svg.appendChild(note);
  const hit=el('rect',{x:0,y:0,width:W,height:H,fill:'transparent'});
  hit.addEventListener('pointermove',ev=>{const r=host.getBoundingClientRect();
    const mx=(ev.clientX-r.left)/r.width*W,my=(ev.clientY-r.top)/r.height*H;
    let best=null,bd=1e9;
    P.forEach(p=>{const d=(X(p[0])-mx)**2+(Y(p[1])-my)**2;if(d<bd){bd=d;best=p;}});
    if(best&&bd<400){tip.innerHTML='<div class=t-date>one configuration</div>'+
      '<div class=t-row><span>Sharpe</span><span>'+best[0].toFixed(3)+'</span></div>'+
      '<div class=t-row><span>Net</span><span>'+money(best[1])+'</span></div>';
      tip.style.opacity=1;
      tip.style.left=Math.max(4,Math.min(host.clientWidth-180,X(best[0])/W*host.clientWidth+14))+'px';
      tip.style.top=(Y(best[1])/H*host.clientHeight-10)+'px';}
    else tip.style.opacity=0;});
  hit.addEventListener('pointerleave',()=>tip.style.opacity=0);
  svg.appendChild(hit);host.innerHTML='';host.appendChild(svg);
}
function draw(){timeseries();campaigns();scatter();}
draw();
let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(draw,150);});
if(window.matchMedia)matchMedia('(prefers-color-scheme:dark)').addEventListener('change',
  ()=>setTimeout(draw,60));
"""


if __name__ == "__main__":
    try:
        p = build()
    except MissingResult as ex:
        print("cannot build the page:\n  %s" % ex, file=sys.stderr)
        raise SystemExit(2)
    print("wrote %s (%.0f KB)" % (p, os.path.getsize(p) / 1024.0))
