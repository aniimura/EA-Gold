# -*- coding: utf-8 -*-
"""Assemble data + financing + engine from a RunConfig."""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

import pandas as pd

from .config import BASELINE_WARMUP_BARS, RunConfig, code_version
from .dataio import (DataError, load_bars, load_events, load_repo_h4,
                     load_repo_ltf, load_swap_table)
from .engine import EngineResult, run_engine
from .financing import FinancingModel


def _infer_ltf_name(path_or_none: Optional[str]) -> str:
    if not path_or_none:
        return ""
    b = os.path.basename(path_or_none).upper()
    for tf in ("M1", "M5", "M15"):
        if "_%s_" % tf in b or b.startswith(tf):
            return tf
    return "LTF"


def build_inputs(cfg: RunConfig, verbose: bool = True) -> Dict:
    """Load and validate every input the run needs. Never repairs anything."""
    out: Dict = {"quality": {}}

    if cfg.h4_file:
        h4, rep = load_bars(cfg.h4_file, "H4 bars", basis="utc")
    else:
        h4, rep = load_repo_h4(cfg.symbol, cfg.timeframe, cfg.date_from,
                               cfg.date_to, BASELINE_WARMUP_BARS)
    out["quality"]["h4"] = rep.to_dict()

    ltf = None
    if cfg.ltf_file:
        if cfg.ltf_file.upper() in ("M1", "M5", "M15"):
            ltf, lrep = load_repo_ltf(cfg.symbol, cfg.ltf_file.upper(),
                                      cfg.date_from, cfg.date_to)
            out["ltf_timeframe"] = cfg.ltf_file.upper()
        else:
            ltf, lrep = load_bars(cfg.ltf_file, "lower-timeframe bars", basis="utc")
            out["ltf_timeframe"] = _infer_ltf_name(cfg.ltf_file)
        out["quality"]["ltf"] = lrep.to_dict()

    table = None
    if cfg.swap_file:
        table = load_swap_table(cfg.swap_file)
        out["quality"]["swap"] = {
            "rows": int(len(table)),
            "first": str(table["date"].iloc[0].date()),
            "last": str(table["date"].iloc[-1].date()),
        }

    events = None
    if cfg.events_file:
        events = load_events(cfg.events_file)
        out["quality"]["events"] = {"rows": int(len(events))}

    out["h4"] = h4
    out["ltf"] = ltf
    out["swap_table"] = table
    out["events"] = events
    if verbose:
        print("  data   : H4 %d bars  %s .. %s"
              % (len(h4), h4["time"].iloc[0], h4["time"].iloc[-1]))
        if ltf is not None:
            print("  data   : %s %d bars" % (out.get("ltf_timeframe", "LTF"), len(ltf)))
        if table is not None:
            print("  swap   : %d rows %s .. %s" % (len(table),
                  table["date"].iloc[0].date(), table["date"].iloc[-1].date()))
        if events is not None:
            print("  events : %d windows" % len(events))
    return out


def build_and_run(cfg: RunConfig, verbose: bool = True) -> EngineResult:
    cfg.validate()
    if cfg.stop_mode == "ltf" and not cfg.ltf_file:
        raise ValueError(
            "stop_mode=ltf needs lower-timeframe bars: pass --ltf-file with a "
            "path, or M1/M5/M15 to use the repo cache. Without them the engine "
            "would silently fall back to the H4 approximation, which is exactly "
            "the substitution this mode exists to avoid.")
    inp = build_inputs(cfg, verbose=verbose)
    fin = FinancingModel(
        model=("none" if not cfg.use_costs else cfg.swap_model),
        long_flat=cfg.swap_long_flat, short_flat=cfg.swap_short_flat,
        triple_weekday=cfg.triple_weekday, table=inp["swap_table"],
        missing_policy=cfg.swap_missing_policy, scenario=cfg.swap_scenario)
    res = run_engine(inp["h4"], cfg, fin, ltf=inp["ltf"], events=inp["events"])
    res.diagnostics["data_quality"] = inp["quality"]
    res.diagnostics["ltf_timeframe"] = inp.get("ltf_timeframe")
    res.diagnostics["code_version"] = code_version()
    res.diagnostics["config_fingerprint"] = cfg.fingerprint()
    return res
