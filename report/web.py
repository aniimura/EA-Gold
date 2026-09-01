# -*- coding: utf-8 -*-
"""A self-contained ``index.html`` for GitHub Pages.

Everything the page needs is written into the file: the trade data as embedded
JSON and the chart as inline SVG drawn by a small vanilla script.  There is no
CDN, no stylesheet, and no reference to ``results/`` - the page renders even if
only ``index.html`` is pushed, and it keeps working offline.

The page is generated rather than hand-written because the strategy is still
being iterated: a static copy would be wrong the moment ``pybt`` runs again.
Re-run ``cli.py web`` after a backtest and commit the result.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

import numpy as np
import pandas as pd

from core import config
from report.chart import _excursions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Other reports published from this repository. Listed here rather than
# hard-coded into the template so a page stays a generic artefact of whatever
# strategy produced it - `build()` filters out the one it is currently writing.
RELATED = [
    {"name": "ScalpGoldM1",
     "href": "./",
     "label": "Scalp Gold M1",
     "note": "GOLD M1 · Bollinger mean-reversion"},
    {"name": "XauMsvsd",
     "href": "xau-msvsd/",
     "label": "XAU Multi-Speed Donchian",
     "note": "GOLD H4 · trend · verified across Pine, Python and MT5"},
]


def _load_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _verdict(name):
    """The reconciliation verdict, if a report has been produced."""
    p = os.path.join(config.RESULTS_DIR, "%s_reconcile.txt" % name)
    if not os.path.isfile(p):
        return None
    text = open(p, "r", encoding="utf-8", errors="replace").read()
    m = re.search(r"VERDICT:\s*(PASS|WARN|FAIL)", text)
    if not m:
        return None
    tr = re.search(r"exact match rate\s*:\s*([\d.]+)%", text)
    return {"verdict": m.group(1), "match_rate": float(tr.group(1)) if tr else None}


def build(strategy, strategy_path=None, related=None):
    """Write ``index.html`` at the repository root and return its path.

    ``strategy_path`` is only used to print the exact commands that regenerate
    the page, so the instructions at the top always name the file that actually
    produced it rather than a guess.

    ``related`` overrides the sibling-report links; the default is ``RELATED``
    with the current strategy filtered out, so a page never links to itself.
    """
    name = strategy.name
    tr_path = os.path.join(config.RESULTS_DIR, "%s_py_trades.csv" % name)
    if not os.path.isfile(tr_path):
        raise IOError("no Python result yet - run `pybt` first (%s)" % tr_path)

    trades = pd.read_csv(tr_path, parse_dates=["entry_time", "exit_time"])
    trades = trades.sort_values("exit_time").reset_index(drop=True)

    bars_path = os.path.join(config.RESULTS_DIR, "%s_py_bars.pkl" % name)
    bars = pd.read_pickle(bars_path) if os.path.isfile(bars_path) else None

    sym = _load_json(os.path.join(config.DATA_DIR, "%s_syminfo.json" % name))
    contract = float(sym.get("contract_size", 100.0))
    run_up, draw = _excursions(trades, bars, contract)

    def col(c, cast=float):
        return [None if pd.isna(v) else cast(v) for v in trades[c]]

    data = {
        "t": [int(v.value // 10 ** 6) for v in trades["exit_time"]],   # ms epoch
        "tin": [int(v.value // 10 ** 6) for v in trades["entry_time"]],
        "dir": list(trades["direction"]),
        "pin": col("entry_price"),
        "pout": col("exit_price"),
        "lots": col("lots"),
        "net": col("net_money"),
        "gross": col("pnl_money"),
        "reason": list(trades["exit_reason"]),
        "trailed": [bool(v) for v in trades["trailed"]],
        "bars": col("bars_held", int),
        "runup": None if run_up is None else [None if np.isnan(v) else float(v) for v in run_up],
        "draw": None if draw is None else [None if np.isnan(v) else float(v) for v in draw],
    }

    py = _load_json(os.path.join(config.RESULTS_DIR, "%s_py_stats.json" % name))
    mt5 = _load_json(os.path.join(config.RESULTS_DIR, "%s_mt5_stats.json" % name))

    if strategy_path:
        rel = os.path.relpath(os.path.abspath(strategy_path), ROOT).replace("\\", "/")
    else:
        rel = "strategies/%s.py" % name.lower()

    meta = {
        "name": name,
        "strategy_file": rel,
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "date_from": str(strategy.date_from),
        "date_to": str(strategy.date_to),
        "currency": strategy.currency,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "py": py,
        "mt5": mt5,
        "recon": _verdict(name),
        "related": [r for r in (related if related is not None else RELATED)
                    if r.get("name") != name],
    }

    html = _TEMPLATE.replace("__META__", json.dumps(meta, default=str)) \
                    .replace("__DATA__", json.dumps(data)) \
                    .replace("__TITLE__", "%s - %s %s" % (name, strategy.symbol,
                                                          strategy.timeframe))
    out = os.path.join(ROOT, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out


# ---------------------------------------------------------------------------
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#ffffff; --panel:#ffffff; --ink:#1c2127; --muted:#6b7280;
    --line:#e5e7eb; --grid:#eef1f5;
    --green:#089981; --red:#f23645; --grey:#9aa0a6; --accent:#0b7285;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0d1117; --panel:#161b22; --ink:#e6edf3; --muted:#8b949e;
      --line:#30363d; --grid:#21262d;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
  .howto{border:1px solid var(--line);border-left:4px solid var(--accent);
         background:var(--panel);border-radius:10px;padding:16px 18px;margin-bottom:24px}
  .howto h2{margin:0 0 10px;font-size:15px;font-weight:650}
  .howto p{margin:0 0 8px}
  .howto .say{background:var(--grid);border-radius:8px;padding:12px 14px;margin:8px 0 14px;
              font-size:15px;font-weight:600;line-height:1.6}
  .howto details{margin-top:6px}
  .howto summary{cursor:pointer;color:var(--muted);font-size:12px;user-select:none}
  .howto pre{background:var(--grid);border-radius:8px;padding:12px 14px;margin:8px 0 0;
             overflow-x:auto;font-size:12px;line-height:1.7}
  header{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:22px}
  h1{margin:0 0 6px;font-size:22px;letter-spacing:-.01em}
  .sub{color:var(--muted);font-size:13px}
  .related{display:flex;flex-wrap:wrap;gap:8px;margin:-8px 0 22px}
  .related a{display:inline-flex;flex-direction:column;gap:1px;text-decoration:none;
             border:1px solid var(--line);border-radius:6px;padding:7px 12px;
             background:var(--panel);min-width:0}
  .related a:hover{border-color:var(--accent)}
  .related .r-label{color:var(--accent);font-size:13px;font-weight:600}
  .related .r-note{color:var(--muted);font-size:11.5px}
  .related .r-head{color:var(--muted);font-size:11.5px;align-self:center;margin-right:2px}
  .badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
         font-weight:600;margin-left:8px;vertical-align:2px}
  .pass{background:rgba(8,153,129,.14);color:var(--green)}
  .fail{background:rgba(242,54,69,.14);color:var(--red)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:22px 0 26px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  .card .v{font-size:20px;font-weight:650;margin-top:3px;font-variant-numeric:tabular-nums}
  .pos{color:var(--green)} .neg{color:var(--red)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         padding:14px 14px 6px;margin-bottom:16px;position:relative}
  .panel h2{margin:0 0 2px;font-size:13px;font-weight:600}
  .panel .hint{color:var(--muted);font-size:12px;margin:0 0 8px}
  svg{display:block;width:100%;height:auto;overflow:visible}
  .tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .08s;
       background:var(--panel);border:1px solid var(--line);border-radius:8px;
       padding:8px 10px;font-size:12px;line-height:1.5;white-space:nowrap;
       box-shadow:0 6px 20px rgba(0,0,0,.16);z-index:5;font-variant-numeric:tabular-nums}
  table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
  th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
  th:first-child,td:first-child{text-align:left;color:var(--muted)}
  thead th{color:var(--muted);font-weight:600;font-size:11px;
           text-transform:uppercase;letter-spacing:.05em}
  footer{color:var(--muted);font-size:12px;margin-top:30px;
         border-top:1px solid var(--line);padding-top:14px}
  code{background:var(--grid);padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <div class="howto">
    <h2>🔄 このページを更新する方法</h2>
    <p>Claude Code に、こう言うだけです：</p>
    <div class="say">「バックテストを実行して、結果を GitHub Pages で見れるようにして」</div>
    <p style="color:var(--muted);font-size:12px;margin:0">
      バックテストの再実行 → このページの再生成 → GitHub への push まで、まとめて行われます。</p>
    <details>
      <summary>自分で実行する場合のコマンド</summary>
      <pre id="cmds"></pre>
    </details>
  </div>

  <noscript>
    <div class="howto" style="border-left-color:var(--red)">
      <h2>グラフを表示するには JavaScript が必要です</h2>
      <p style="margin:0">このページはトレードデータを埋め込み、ブラウザ側で SVG を描画しています。</p>
    </div>
  </noscript>

  <header>
    <h1 id="title"></h1>
    <div class="sub" id="subtitle"></div>
  </header>

  <nav class="related" id="related"></nav>

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>Cumulative P/L</h2>
    <p class="hint">Solid = net (after swap and commission). Dashed = gross, price movement only.
       Where they separate, costs are eating the edge.</p>
    <svg id="equity" viewBox="0 0 1200 420"></svg>
    <div class="tip" id="tip"></div>
  </div>

  <div class="panel" id="excPanel">
    <h2>Run-ups and drawdowns</h2>
    <p class="hint">Best and worst open P/L reached during each trade.</p>
    <svg id="exc" viewBox="0 0 1200 180"></svg>
  </div>

  <div class="panel">
    <h2>Wins and losses</h2>
    <svg id="strip" viewBox="0 0 1200 34"></svg>
  </div>

  <div class="panel" id="cmpPanel">
    <h2>Python vs MT5</h2>
    <p class="hint">The Python engine prices movement only; MT5 also books swap and commission.</p>
    <table id="cmp"></table>
  </div>

  <footer id="foot"></footer>
</div>

<script>
const META = __META__;
const D = __DATA__;

const NS = "http://www.w3.org/2000/svg";
const el = (n, a) => { const e = document.createElementNS(NS, n);
  for (const k in (a||{})) e.setAttribute(k, a[k]); return e; };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (v, d=2) => (v==null || isNaN(v)) ? "-" :
  v.toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
const sign = (v, d=2) => (v>=0?"+":"") + fmt(v, d);
const dstr = ms => { const x = new Date(ms);
  return x.toISOString().slice(0,16).replace("T"," "); };

/* running sums */
const cum = a => { let s=0; return a.map(v => s += (v||0)); };
const cumNet = cum(D.net), cumGross = cum(D.gross);
const n = D.t.length;

/* ---------- how to update ---------- */
document.getElementById("cmds").textContent = [
  "python cli.py pybt " + META.strategy_file + " --bars   # バックテスト",
  "python cli.py web  " + META.strategy_file + "          # このページを再生成",
  "git add -A && git commit -m \"update results\" && git push",
].join("\n");

/* ---------- header ---------- */
document.title = META.name + " - " + META.symbol + " " + META.timeframe;
document.getElementById("title").textContent =
  META.name + "  ·  " + META.symbol + " " + META.timeframe;
let sub = META.date_from + " .. " + META.date_to + "  ·  " + n + " trades";
document.getElementById("subtitle").innerHTML = sub +
  (META.recon ? '<span class="badge ' + (META.recon.verdict==="PASS"?"pass":"fail") + '">' +
    "Python vs MT5: " + META.recon.verdict +
    (META.recon.match_rate!=null ? " · " + META.recon.match_rate + "% exact" : "") +
   '</span>' : "");

/* ---------- sibling reports ---------- */
const rel = META.related || [];
document.getElementById("related").innerHTML = rel.length
  ? '<span class="r-head">Other reports in this repository:</span>' + rel.map(r =>
      '<a href="' + r.href + '"><span class="r-label">' + r.label + '</span>' +
      '<span class="r-note">' + r.note + '</span></a>').join("")
  : "";

/* ---------- stat cards ---------- */
const py = META.py || {};
const cards = [
  ["Trades", n, ""],
  ["Win rate", fmt(py.win_rate ?? 100*D.net.filter(v=>v>0).length/n) + " %", ""],
  ["Profit factor", fmt(py.profit_factor), ""],
  ["Net P/L", sign(py.net_profit ?? cumNet[n-1]), (py.net_profit ?? cumNet[n-1])>=0?"pos":"neg"],
  ["Gross P/L", sign(py.gross_profit ?? cumGross[n-1]), (py.gross_profit ?? cumGross[n-1])>=0?"pos":"neg"],
  ["Max drawdown", fmt(py.max_dd), ""],
];
document.getElementById("cards").innerHTML = cards.map(c =>
  '<div class="card"><div class="k">'+c[0]+'</div><div class="v '+c[2]+'">'+c[1]+'</div></div>'
).join("");

/* ---------- scales ---------- */
const t0 = Math.min(D.t[0], D.tin[0]), t1 = D.t[n-1];
const PAD = {l:62, r:64, t:14, b:26};
const X = (ms, w) => PAD.l + (ms - t0) / (t1 - t0 || 1) * (w - PAD.l - PAD.r);

function yScale(vals, h, padFrac=0.08){
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const span = (hi - lo) || 1, p = span * padFrac;
  lo -= p; hi += p;
  return v => PAD.t + (hi - v) / (hi - lo) * (h - PAD.t - PAD.b);
}
function ticks(lo, hi, count=5){
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1,2,2.5,5,10].map(m=>m*mag).find(s=>s>=raw) || mag*10;
  const out = []; for (let v = Math.ceil(lo/step)*step; v <= hi; v += step) out.push(v);
  return out;
}
function timeAxis(svg, w, h){
  const days = (t1 - t0) / 86400000;
  const stepDays = days > 240 ? 30 : days > 60 ? 14 : days > 20 ? 7 : 2;
  const g = el("g");
  for (let ms = t0; ms <= t1; ms += stepDays * 86400000){
    const x = X(ms, w);
    const d = new Date(ms);
    const lbl = el("text", {x:x, y:h-8, "text-anchor":"middle",
      "font-size":11, fill:css("--muted")});
    lbl.textContent = (d.getUTCMonth()+1) + "/" + d.getUTCDate();
    g.appendChild(lbl);
  }
  svg.appendChild(g);
}

/* ---------- equity panel ---------- */
(function(){
  const svg = document.getElementById("equity");
  const W = 1200, H = 420;
  const all = cumNet.concat(cumGross);
  const y = yScale(all, H);
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  const span = (hi-lo)||1; lo -= span*0.08; hi += span*0.08;

  for (const v of ticks(lo, hi)){
    svg.appendChild(el("line", {x1:PAD.l, x2:W-PAD.r, y1:y(v), y2:y(v),
      stroke:css("--grid"), "stroke-width":1}));
    const tx = el("text", {x:PAD.l-10, y:y(v)+4, "text-anchor":"end",
      "font-size":11, fill:css("--muted")});
    tx.textContent = fmt(v, 0); svg.appendChild(tx);
  }
  svg.appendChild(el("line", {x1:PAD.l, x2:W-PAD.r, y1:y(0), y2:y(0),
    stroke:css("--muted"), "stroke-width":1, opacity:.55}));

  const path = a => a.map((v,i) => (i?"L":"M") + X(D.t[i],W) + " " + y(v)).join(" ");
  svg.appendChild(el("path", {d: path(cumNet) + " L" + X(D.t[n-1],W) + " " + y(0) +
    " L" + X(D.t[0],W) + " " + y(0) + " Z", fill:css("--green"), opacity:.12}));
  svg.appendChild(el("path", {d: path(cumGross), fill:"none", stroke:css("--grey"),
    "stroke-width":1.4, "stroke-dasharray":"5 4"}));
  svg.appendChild(el("path", {d: path(cumNet), fill:"none", stroke:css("--green"),
    "stroke-width":2}));

  const last = cumNet[n-1], good = last >= 0;
  const bx = X(D.t[n-1], W) + 6, by = y(last);
  svg.appendChild(el("rect", {x:bx, y:by-11, width:58, height:22, rx:5,
    fill: good ? css("--green") : css("--red")}));
  const bt = el("text", {x:bx+29, y:by+4, "text-anchor":"middle", "font-size":11,
    fill:"#fff", "font-weight":"700"});
  bt.textContent = sign(last); svg.appendChild(bt);

  timeAxis(svg, W, H);

  /* hover */
  const hair = el("line", {y1:PAD.t, y2:H-PAD.b, stroke:css("--muted"),
    "stroke-width":1, opacity:0});
  const dot = el("circle", {r:4, fill:css("--green"), stroke:css("--panel"),
    "stroke-width":2, opacity:0});
  svg.appendChild(hair); svg.appendChild(dot);
  const tip = document.getElementById("tip");
  const panel = svg.parentElement;

  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * W;
    let best = 0, bd = Infinity;
    for (let i=0;i<n;i++){ const d = Math.abs(X(D.t[i],W) - px);
      if (d < bd){ bd = d; best = i; } }
    const x = X(D.t[best], W);
    hair.setAttribute("x1", x); hair.setAttribute("x2", x); hair.setAttribute("opacity", .45);
    dot.setAttribute("cx", x); dot.setAttribute("cy", y(cumNet[best]));
    dot.setAttribute("opacity", 1);
    const q = D.net[best] >= 0 ? "pos" : "neg";
    tip.innerHTML =
      "<b>#" + (best+1) + "  " + D.dir[best].toUpperCase() + "</b>  " +
      D.lots[best] + " lot<br>" +
      dstr(D.tin[best]) + " &rarr; " + dstr(D.t[best]) +
      "  (" + D.bars[best] + " bars)<br>" +
      fmt(D.pin[best]) + " &rarr; " + fmt(D.pout[best]) +
      "  <span style='color:var(--muted)'>" + D.reason[best] +
      (D.trailed[best] ? " (trailed)" : "") + "</span><br>" +
      "net <b class='" + q + "'>" + sign(D.net[best]) + "</b>" +
      "   ·   cumulative <b>" + sign(cumNet[best]) + "</b>";
    tip.style.opacity = 1;
    const lx = ev.clientX - panel.getBoundingClientRect().left;
    const ly = ev.clientY - panel.getBoundingClientRect().top;
    tip.style.left = Math.min(lx + 16, panel.clientWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (ly - tip.offsetHeight - 12) + "px";
  });
  svg.addEventListener("mouseleave", () => {
    tip.style.opacity = 0; hair.setAttribute("opacity", 0); dot.setAttribute("opacity", 0);
  });
})();

/* ---------- run-up / drawdown ---------- */
(function(){
  if (!D.runup){ document.getElementById("excPanel").remove(); return; }
  const svg = document.getElementById("exc");
  const W = 1200, H = 180;
  const vals = D.runup.concat(D.draw).filter(v => v != null);
  const y = yScale(vals, H);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const span=(hi-lo)||1; lo-=span*0.08; hi+=span*0.08;
  for (const v of ticks(lo, hi, 3)){
    svg.appendChild(el("line", {x1:PAD.l, x2:W-PAD.r, y1:y(v), y2:y(v),
      stroke:css("--grid"), "stroke-width":1}));
    const tx = el("text", {x:PAD.l-10, y:y(v)+4, "text-anchor":"end",
      "font-size":11, fill:css("--muted")});
    tx.textContent = fmt(v, 0); svg.appendChild(tx);
  }
  for (let i=0;i<n;i++){
    const x = X(D.t[i], W);
    if (D.runup[i] != null)
      svg.appendChild(el("line", {x1:x, x2:x, y1:y(0), y2:y(D.runup[i]),
        stroke:css("--green"), "stroke-width":1.2, opacity:.85}));
    if (D.draw[i] != null)
      svg.appendChild(el("line", {x1:x, x2:x, y1:y(0), y2:y(D.draw[i]),
        stroke:css("--red"), "stroke-width":1.2, opacity:.85}));
  }
  svg.appendChild(el("line", {x1:PAD.l, x2:W-PAD.r, y1:y(0), y2:y(0),
    stroke:css("--muted"), "stroke-width":1, opacity:.55}));
  timeAxis(svg, W, H);
})();

/* ---------- win / loss strip ---------- */
(function(){
  const svg = document.getElementById("strip");
  const W = 1200;
  for (let i=0;i<n;i++){
    svg.appendChild(el("rect", {x: X(D.t[i], W) - 1.5, y: 6, width: 3, height: 20,
      fill: D.net[i] > 0 ? css("--green") : css("--red"), opacity:.9}));
  }
})();

/* ---------- Python vs MT5 ---------- */
(function(){
  const m = META.mt5 || {};
  if (!Object.keys(m).length){ document.getElementById("cmpPanel").remove(); return; }
  const p = META.py || {};
  const rows = [
    ["Trades", n, m.total_trades],
    ["Win rate %", fmt(p.win_rate), fmt(m.win_rate)],
    ["Profit factor", fmt(p.profit_factor), fmt(m.profit_factor)],
    ["Net profit", sign(p.net_profit), sign(m.total_net_profit)],
    ["Max drawdown %", fmt(p.max_dd_pct), fmt(m.max_dd_pct)],
  ];
  document.getElementById("cmp").innerHTML =
    "<thead><tr><th>Metric</th><th>Python</th><th>MT5</th></tr></thead><tbody>" +
    rows.map(r => "<tr><td>"+r[0]+"</td><td>"+r[1]+"</td><td>"+r[2]+"</td></tr>").join("") +
    "</tbody>";
})();

document.getElementById("foot").innerHTML =
  "Generated " + META.generated + " by <code>cli.py web</code> · " +
  "values in " + META.currency + " · " +
  "signal on the last closed bar, filled at the next bar's open.";
</script>
</body>
</html>
"""
