# -*- coding: utf-8 -*-
"""XAU Multi-Speed Volatility-Scaled Donchian Trend - backtester (v2).

The v1 single-file runner is frozen at tests/golden/v1_frozen_mod.py and its
results are preserved in results/. v2 writes to results/v2/ and never touches
them.

BASELINE
    Every option below defaults to the frozen specification. `--v1-compat`
    additionally reproduces two v1 DEFECTS bit for bit, so the golden fixture
    stays reproducible; see CHANGELOG_msvsd_v2.md.

    python bt_xau_msvsd.py --tag baseline
    python bt_xau_msvsd.py --tag v1_golden --v1-compat

Run `--help` for the full flag list, or read README_msvsd_v2.md for worked
commands covering stop replay, historical carry, campaign bootstraps, Pine
reconciliation, challenger modes and cost stress tests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                            # noqa: E402
import pandas as pd                                           # noqa: E402

from msvsd import __version__                                 # noqa: E402
from msvsd.campaigns import build_campaigns, daily_frames     # noqa: E402
from msvsd.config import (BASELINE_ATR_MULT, BASELINE_CAPITAL, BASELINE_LOT_STEP,
                          BASELINE_MAX_NOTIONAL_X, BASELINE_RISK_PCT,
                          DIRECTION_MODES, EVENT_MODES, RunConfig,
                          SLEEVE_MODES, SLEEVE_MODE_MEMBERS, STOP_MODES,
                          SWAP_MISSING_POLICIES, SWAP_MODELS, SWAP_SCENARIOS,
                          BASELINE_SWAP_LONG, BASELINE_SWAP_SHORT,
                          BASELINE_COMMISSION_PER_LOT_RT)
from msvsd.dataio import DataError                           # noqa: E402
from msvsd.financing import FinancingError                    # noqa: E402
from msvsd.reporting import print_summary, write_outputs      # noqa: E402
from msvsd.run import build_and_run                           # noqa: E402
from msvsd.statistics import full_report                      # noqa: E402


def _sleeve_mode(value: str) -> str:
    """Accept the v2 mode names and the v1 comma list."""
    v = value.strip().lower()
    if v in SLEEVE_MODES:
        return v
    members = tuple(sorted(x.strip() for x in v.split(",") if x.strip()))
    for mode, m in SLEEVE_MODE_MEMBERS.items():
        if members == tuple(sorted(m)):
            return mode
    raise argparse.ArgumentTypeError(
        "--sleeves must be one of %s (or the equivalent comma list); got %r"
        % (list(SLEEVE_MODES), value))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bt_xau_msvsd.py",
        description="XAU Multi-Speed Donchian backtester v%s" % __version__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    d = p.add_argument_group("data")
    d.add_argument("--h4-file", default=None,
                   help="H4 OHLC file (.csv/.pkl/.parquet, UTC). Default: repo GOLD_H4 cache")
    d.add_argument("--ltf-file", default=None,
                   help="lower-timeframe file for stop replay, or M1/M5/M15 to use the repo cache")
    d.add_argument("--swap-file", default=None, help="historical financing CSV")
    d.add_argument("--events-file", default=None, help="scheduled-event CSV")
    d.add_argument("--date-from", default=None)
    d.add_argument("--date-to", default=None)

    a = p.add_argument_group("account")
    a.add_argument("--capital", type=float, default=BASELINE_CAPITAL)
    a.add_argument("--risk", type=float, default=BASELINE_RISK_PCT,
                   help="percent of equity risked per sleeve")
    a.add_argument("--lot-step", type=float, default=BASELINE_LOT_STEP)
    a.add_argument("--contract-oz", type=float, default=None,
                   help="ounces per lot (100 = standard, 10 = micro). Swap and "
                        "commission are quoted PER STANDARD LOT and are scaled "
                        "pro rata automatically.")
    a.add_argument("--max-notional", type=float, default=BASELINE_MAX_NOTIONAL_X)

    s = p.add_argument_group("strategy / challenger modes")
    s.add_argument("--sleeves", type=_sleeve_mode, default="all",
                   help="sleeve configuration: %s" % list(SLEEVE_MODES))
    s.add_argument("--direction", choices=DIRECTION_MODES, default="symmetric")
    s.add_argument("--atr-mult", type=float, default=BASELINE_ATR_MULT)
    s.add_argument("--exit-scale", type=float, default=1.0,
                   help="multiplier on the 10/20/40 exit windows; entries unchanged")
    s.add_argument("--no-friday", action="store_true")
    s.add_argument("--friday-basis", choices=("open", "close"), default="open",
                   help="'close' matches the Pine; 'open' is v1 behaviour and the default")

    c = p.add_argument_group("costs")
    c.add_argument("--cost-scale", type=float, default=1.0,
                   help="multiplies spread AND slippage (stress test: 2 or 3)")
    c.add_argument("--swap-model", choices=SWAP_MODELS, default="flat")
    c.add_argument("--swap-scenario", choices=SWAP_SCENARIOS, default="base")
    c.add_argument("--swap-missing-policy", choices=SWAP_MISSING_POLICIES,
                   default="error")
    c.add_argument("--no-swap", action="store_true",
                   help="TradingView comparison only; never a deployable result")
    c.add_argument("--no-costs", action="store_true")

    e = p.add_argument_group("execution")
    e.add_argument("--stop-mode", choices=STOP_MODES, default="h4")
    e.add_argument("--event-mode", choices=EVENT_MODES, default="report-only")
    e.add_argument("--financing-timing", choices=("post-fill", "pre-fill"),
                   default="post-fill")
    e.add_argument("--log-open-at-end", action="store_true",
                   help="log sleeves still open on the last bar as trades")

    x = p.add_argument_group("audits (never a result)")
    x.add_argument("--lookahead", action="store_true")
    x.add_argument("--same-bar-fill", action="store_true")
    x.add_argument("--v1-compat", action="store_true",
                   help="reproduce the two known v1 defects exactly")

    t = p.add_argument_group("statistics / output")
    t.add_argument("--seed", type=int, default=20260901)
    t.add_argument("--bootstrap", type=int, default=20000)
    t.add_argument("--n-configs-tested", type=int, default=1,
                   help="multiple-testing count fed to the Deflated Sharpe Ratio")
    t.add_argument("--tag", default="baseline")
    t.add_argument("--outdir", default=os.path.join("results", "v2"))
    t.add_argument("--quiet", action="store_true")

    p.add_argument("--reconcile", default=None, metavar="TRADINGVIEW_EXPORT.CSV",
                   help="compare this Pine debug export against the Python run, bar by bar")
    return p


def config_from_args(args) -> RunConfig:
    kw = dict(
        h4_file=args.h4_file, ltf_file=args.ltf_file, swap_file=args.swap_file,
        events_file=args.events_file,
        capital=args.capital, risk_pct=args.risk, lot_step=args.lot_step,
        max_notional_x=args.max_notional,
        sleeve_mode=args.sleeves, direction_mode=args.direction,
        atr_mult=args.atr_mult, exit_scale=args.exit_scale,
        friday_filter=not args.no_friday, friday_basis=args.friday_basis,
        cost_scale=args.cost_scale, swap_model=args.swap_model,
        swap_scenario=args.swap_scenario,
        swap_missing_policy=args.swap_missing_policy,
        use_costs=not args.no_costs,
        stop_mode=args.stop_mode, event_mode=args.event_mode,
        financing_timing=args.financing_timing,
        log_open_sleeves_at_end=args.log_open_at_end,
        lookahead_audit=args.lookahead, same_bar_fill_audit=args.same_bar_fill,
        v1_compat=args.v1_compat,
        seed=args.seed, bootstrap_n=args.bootstrap,
        n_configs_tested=args.n_configs_tested,
        tag=args.tag, outdir=args.outdir)
    if args.contract_oz is not None:
        # Swap and commission are per STANDARD lot. A micro contract must carry
        # them pro rata or the carry is inflated by 100 / contract_oz - which
        # looks like the strategy losing money rather than the model being wrong.
        sc = args.contract_oz / 100.0
        kw["contract_oz"] = args.contract_oz
        kw["swap_long_flat"] = BASELINE_SWAP_LONG * sc
        kw["swap_short_flat"] = BASELINE_SWAP_SHORT * sc
        kw["commission_per_lot_rt"] = BASELINE_COMMISSION_PER_LOT_RT * sc
    if args.no_swap:
        kw["swap_model"] = "none"
    if args.date_from:
        kw["date_from"] = args.date_from
    if args.date_to:
        kw["date_to"] = args.date_to
    return RunConfig(**kw)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    try:
        cfg.validate()
    except ValueError as ex:
        print("configuration error:\n%s" % ex, file=sys.stderr)
        return 2

    if not args.quiet:
        print("== XAU MS-VSD backtest v%s ==" % __version__)
    try:
        res = build_and_run(cfg, verbose=not args.quiet)
    except (DataError, FinancingError, ValueError) as ex:
        # These are refusals, not crashes: the engine declines to guess at
        # missing data rather than substituting something plausible.
        print("\nrun refused:\n  %s" % str(ex).replace("\n", "\n  "),
              file=sys.stderr)
        return 3

    camps = build_campaigns(res.bars, res.trades, res.fills, res.financing,
                            cfg.contract_oz)
    _daily_eq, daily_ret = daily_frames(res.equity)
    stats = full_report(res.trades, camps, res.equity, daily_ret["ret"], res.bars,
                        res.diagnostics, cfg.capital, cfg.bootstrap_n, cfg.seed,
                        n_trials=cfg.n_configs_tested)

    paths = write_outputs(res, camps, stats, cfg)

    if args.reconcile:
        from msvsd.reconcile import reconcile
        rep = reconcile(res, args.reconcile, cfg.outdir, cfg.tag)
        with open(os.path.join(cfg.outdir, "%s_reconcile.json" % cfg.tag),
                  "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print("\n== Pine / Python reconciliation ==")
        print("   overlapping bars : %s" % rep.get("overlapping_bars"))
        print("   mismatches       : %s" % rep.get("mismatch_count"))
        print("   max price diff   : %s" % rep.get("max_price_diff"))
        print("   max qty diff     : %s" % rep.get("max_qty_diff"))
        print("   RESULT           : %s" % rep.get("result"))
        print("   %s" % rep.get("statement", rep.get("reason", "")))
        if rep.get("first_mismatch"):
            print("   first mismatch   : %s" % rep["first_mismatch"])

    if not args.quiet:
        print_summary(res, camps, stats, cfg)
        print("  outputs -> %s" % os.path.join(cfg.outdir, cfg.tag + "_*"))
        for k in sorted(paths):
            print("     %s" % paths[k])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
