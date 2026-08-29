# -*- coding: utf-8 -*-
"""FxTrade_202608 command line.

    python cli.py env                       show detected paths
    python cli.py show    <strategy.py>     print the spec + generated MQL5
    python cli.py pybt    <strategy.py>     Python backtest
    python cli.py gen     <strategy.py>     generate the .mq5
    python cli.py mt5bt   <strategy.py>     compile + run the Strategy Tester
    python cli.py recon   <strategy.py>     compare the two runs
    python cli.py chart   <strategy.py>     performance panel from the Python run
    python cli.py web     <strategy.py>     self-contained index.html for GitHub Pages
    python cli.py all     <strategy.py>     pybt -> gen -> mt5bt -> recon

Common flags:
    --bars          also compare every indicator bar by bar (slower, thorough)
    --refresh       re-download bars instead of using the cache
    --model N       tester model: 0 real ticks, 1 one-minute OHLC (default), 2 open
    --spread N      spread in points assumed by the Python engine
    --replay-spread reuse the per-trade spread MT5 reported, then re-run pybt
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codegen import mq5gen                                   # noqa: E402
from core import config, data as datamod                     # noqa: E402
from core.backtest import run_backtest, summarize            # noqa: E402
from core.spec import load_strategy                          # noqa: E402
from core.types import SymbolInfo, trades_to_frame           # noqa: E402
from reconcile import compare as recon                       # noqa: E402
from report import chart, web                                # noqa: E402
from runner import mt5run                                    # noqa: E402


def _paths(name):
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    base = os.path.join(config.RESULTS_DIR, name)
    return {
        "py_trades": base + "_py_trades.csv",
        "py_bars": base + "_py_bars.pkl",
        "py_stats": base + "_py_stats.json",
        "syminfo": os.path.join(config.DATA_DIR, "%s_syminfo.json" % name),
    }


def _load_syminfo(strategy, refresh=False):
    p = _paths(strategy.name)["syminfo"]
    if os.path.isfile(p) and not refresh:
        with open(p, "r", encoding="utf-8") as fh:
            return SymbolInfo(**json.load(fh))
    si = datamod.get_symbol_info(strategy.symbol, config.MT5_EXE)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(si.to_dict(), fh, indent=2)
    return si


# --------------------------------------------------------------------------
def cmd_env(args):
    print("FxTrade_202608 environment")
    print(config.describe())
    return 0


def cmd_show(args):
    s = load_strategy(args.strategy)
    print(s.summary())
    path = mq5gen.generate(s, source_file=os.path.abspath(args.strategy))
    print("\ngenerated: %s (%d lines)"
          % (path, sum(1 for _ in open(path, encoding="utf-8"))))
    if args.full:
        print("-" * 70)
        print(open(path, encoding="utf-8").read())
    return 0


def cmd_pybt(args, strategy=None, mt5_spread=False):
    s = strategy or load_strategy(args.strategy)
    print("== Python backtest ==")
    print(s.summary())
    warm = s.warmup_bars()
    df = datamod.load_rates(s.symbol, s.timeframe, s.date_from, s.date_to,
                            warmup_bars=warm, refresh=args.refresh,
                            terminal_path=config.MT5_EXE)
    htf_frames = {}
    for tf, inner_warm in s.htf_timeframes().items():
        htf_frames[tf] = datamod.load_rates(
            s.symbol, tf, s.date_from, s.date_to,
            warmup_bars=int(inner_warm) + 64, refresh=args.refresh,
            terminal_path=config.MT5_EXE)
        print("  htf    : %s %d bars" % (tf, len(htf_frames[tf])))
    sym = _load_syminfo(s, refresh=args.refresh)
    spread = args.spread if args.spread is not None else s.spread_points
    if spread is None:
        spread = datamod.median_spread_points(df)
        if not np.isfinite(spread) or spread <= 0:
            spread = 5.0
    print("  symbol : digits=%d point=%g stoplevel=%d contract=%g"
          % (sym.digits, sym.point, sym.trade_stops_level, sym.contract_size))

    spread_series = None
    if mt5_spread:
        spread_series = mt5run.spread_series_for(s, df)
    if spread_series is not None:
        print("  spread : per-bar, replayed from MT5 (median %.1f points)"
              % float(np.nanmedian(spread_series)))
    else:
        print("  spread : %.1f points assumed (flat)" % spread)

    res = run_backtest(s, df, sym, spread_points=spread,
                       spread_series=spread_series,
                       start_time=s.date_from, end_time=s.date_to,
                       collect_bars=args.bars, htf_frames=htf_frames)

    p = _paths(s.name)
    frame = trades_to_frame(res.trades, sym.contract_size, s.lot)
    frame.to_csv(p["py_trades"], index=False, encoding="utf-8")
    if res.bars is not None:
        res.bars.to_pickle(p["py_bars"])
    with open(p["py_stats"], "w", encoding="utf-8") as fh:
        json.dump(res.stats, fh, indent=2, default=float)

    print("  result :")
    for k in ("trades", "gross_profit", "swap", "commission", "net_profit",
              "win_rate", "profit_factor", "sharpe", "max_dd_pct", "pnl_atr",
              "ambiguous"):
        if k in res.stats:
            print("      %-14s %s" % (k, _fmt(res.stats[k])))
    print("  saved  : %s" % p["py_trades"])
    return 0


def _fmt(v):
    if isinstance(v, float):
        return "%.4f" % v
    return str(v)


def cmd_gen(args):
    s = load_strategy(args.strategy)
    path = mq5gen.generate(s, source_file=os.path.abspath(args.strategy))
    print("== codegen ==")
    print("  spec   : %s" % os.path.abspath(args.strategy))
    print("  mq5    : %s (%d lines)"
          % (path, sum(1 for _ in open(path, encoding="utf-8"))))
    print("  warmup : %d bars   indicators: %d" % (s.warmup_bars(), len(s.order)))
    return 0


def cmd_mt5bt(args, strategy=None):
    s = strategy or load_strategy(args.strategy)
    print("== MT5 backtest ==")
    path = os.path.join(config.BUILD_DIR, "%s.mq5" % s.name)
    if not os.path.isfile(path):
        path = mq5gen.generate(s, source_file=os.path.abspath(args.strategy))
    mt5run.compile_ea(path)
    stats = mt5run.run_tester(s, model=args.model, write_bars=args.bars)
    for k, v in sorted(stats.items()):
        print("      %-18s %s" % (k, _fmt(v) if isinstance(v, float) else v))
    with open(os.path.join(config.RESULTS_DIR, "%s_mt5_stats.json" % s.name),
              "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2, default=str)
    return 0


def cmd_recon(args, strategy=None):
    s = strategy or load_strategy(args.strategy)
    p = _paths(s.name)
    if not os.path.isfile(p["py_trades"]):
        print("no Python result yet - run `pybt` first")
        return 2

    py_tr = pd.read_csv(p["py_trades"], parse_dates=["entry_time", "exit_time"])
    mt5_tr = mt5run.read_mt5_trades(s)
    sym = _load_syminfo(s)

    py_bars = pd.read_pickle(p["py_bars"]) if os.path.isfile(p["py_bars"]) else None
    mt5_bars = mt5run.read_mt5_bars(s)
    bar_sum, bar_stats = recon.reconcile_bars(py_bars, mt5_bars, list(s.order))

    spread = args.spread if args.spread is not None else s.spread_points
    detail, tstats = recon.reconcile_trades(py_tr, mt5_tr, s.timeframe,
                                            sym.point, spread_points=spread,
                                            contract_size=sym.contract_size,
                                            lot=s.lot)

    py_stats = json.load(open(p["py_stats"], encoding="utf-8")) \
        if os.path.isfile(p["py_stats"]) else None
    mt5_stats_path = os.path.join(config.RESULTS_DIR, "%s_mt5_stats.json" % s.name)
    mt5_stats = json.load(open(mt5_stats_path, encoding="utf-8")) \
        if os.path.isfile(mt5_stats_path) else None

    text, verdict = recon.render_report(s.name, bar_sum, bar_stats, detail,
                                        tstats, py_stats, mt5_stats, lot=s.lot)
    print(text)
    out = recon.save_report(s.name, text, detail, bar_sum)
    print("\nsaved: %s" % out)
    return 0 if verdict == recon.PASS else (1 if verdict == recon.WARN else 2)


def cmd_chart(args, strategy=None):
    s = strategy or load_strategy(args.strategy)
    print("== performance chart ==")
    path = chart.render(s)
    print("  saved  : %s" % path)
    return 0


def cmd_web(args, strategy=None):
    s = strategy or load_strategy(args.strategy)
    print("== GitHub Pages ==")
    path = web.build(s, strategy_path=args.strategy)
    print("  saved  : %s" % path)
    print("  commit index.html, then enable Pages (branch: main, folder: / root)")
    return 0


def cmd_all(args):
    s = load_strategy(args.strategy)
    rc = cmd_pybt(args, s)
    if rc:
        return rc
    print()
    rc = cmd_gen(args)
    if rc:
        return rc
    print()
    rc = cmd_mt5bt(args, s)
    if rc:
        return rc
    print()
    if args.replay_spread:
        try:
            print("== replay: Python backtest with MT5's own per-bar spread ==")
            cmd_pybt(args, s, mt5_spread=True)
            print()
        except Exception as exc:
            print("  (spread replay skipped: %s)" % exc)
    return cmd_recon(args, s)


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="fxtrade", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, need_strategy=True):
        p = sub.add_parser(name)
        if need_strategy:
            p.add_argument("strategy")
        p.add_argument("--bars", action="store_true")
        p.add_argument("--refresh", action="store_true")
        p.add_argument("--model", type=int, default=1)
        p.add_argument("--spread", type=float, default=None)
        p.add_argument("--replay-spread", action="store_true")
        p.add_argument("--full", action="store_true")
        p.set_defaults(func=fn)
        return p

    add("env", cmd_env, need_strategy=False)
    add("show", cmd_show)
    add("pybt", cmd_pybt)
    add("gen", cmd_gen)
    add("mt5bt", cmd_mt5bt)
    add("recon", cmd_recon)
    add("chart", cmd_chart)
    add("web", cmd_web)
    add("all", cmd_all)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
