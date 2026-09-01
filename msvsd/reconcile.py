# -*- coding: utf-8 -*-
"""Pine <-> Python bar-by-bar reconciliation.

Until this passes, the two implementations are two independent readings of one
specification that happen to agree on a summary number. That is not the same
as being reconciled, and the report says so.

INPUT
    A CSV exported from TradingView with the strategy in debug mode (see the
    Pine's `DEBUG EXPORT` section). Column names are matched case-insensitively
    and TradingView's plot-title prefixes are tolerated, so a column called
    "XAU MS-VSD: dbg_state_fast" resolves to `state_fast`.

TOLERANCES  (documented, not tuned to make a run pass)
    prices      1e-4 absolute - well inside one XAUUSD tick (0.01)
    quantities  1e-9 lots     - float noise only
    ATR         1e-4 absolute
    states      exact integer match
    reason      exact integer match
    equity      0.01 USD, compared EXCLUDING financing because Pine models none
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

TOL = {"price": 1e-4, "qty": 1e-9, "atr": 1e-4, "equity": 0.01}

PRICE_FIELDS = ["stop_fast", "stop_medium", "stop_slow",
                "ent_hi_fast", "ent_lo_fast", "exit_hi_fast", "exit_lo_fast",
                "ent_hi_medium", "ent_lo_medium", "exit_hi_medium", "exit_lo_medium",
                "ent_hi_slow", "ent_lo_slow", "exit_hi_slow", "exit_lo_slow"]
QTY_FIELDS = ["qty_fast", "qty_medium", "qty_slow", "net_target_lots", "position_lots"]
INT_FIELDS = ["state_fast", "state_medium", "state_slow",
              "reason_fast", "reason_medium", "reason_slow"]
ATR_FIELDS = ["atr"]
EQ_FIELDS = ["equity_ex_financing"]

ALL_FIELDS = PRICE_FIELDS + QTY_FIELDS + INT_FIELDS + ATR_FIELDS + EQ_FIELDS


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def _find(cols: Dict[str, str], field: str) -> Optional[str]:
    if field in cols:
        return cols[field]
    for norm, orig in cols.items():
        if norm.endswith("_" + field) or norm.endswith(field):
            return orig
    return None


def load_pine_export(path: str) -> pd.DataFrame:
    if not os.path.isfile(path):
        raise FileNotFoundError("TradingView export not found: %s" % path)
    df = pd.read_csv(path)
    cols = {_norm(c): c for c in df.columns}
    # Prefer an explicit UTC column. TradingView exports one time column in UTC;
    # MT5 stamps bars in BROKER SERVER time and the EA therefore writes both,
    # so picking "time" blindly would silently compare stamps 2-3 hours apart
    # and report zero overlapping bars.
    tcol = (cols.get("time_utc") or _find(cols, "time")
            or _find(cols, "date") or _find(cols, "datetime"))
    if tcol is None:
        raise ValueError("no time column in %s (looked for time_utc/time/date/datetime)"
                         % path)
    out = pd.DataFrame()
    ts = df[tcol]
    if np.issubdtype(ts.dtype, np.number):
        # TradingView exports UNIX seconds for the bar OPEN
        out["time_utc"] = pd.to_datetime(ts.astype("int64"), unit="s")
    else:
        out["time_utc"] = pd.to_datetime(ts, utc=True, errors="coerce"
                                         ).dt.tz_convert("UTC").dt.tz_localize(None)
    found, missing = [], []
    for f in ALL_FIELDS:
        c = _find(cols, f)
        if c is None:
            missing.append(f)
            continue
        out[f] = pd.to_numeric(df[c], errors="coerce")
        found.append(f)
    out.attrs["found"] = found
    out.attrs["missing"] = missing
    return out.dropna(subset=["time_utc"]).sort_values("time_utc").reset_index(drop=True)


def python_frame(res) -> pd.DataFrame:
    b = res.bars
    out = pd.DataFrame({"time_utc": pd.DatetimeIndex(b["time_utc"])})
    for f in ALL_FIELDS:
        if f == "position_lots":
            out[f] = b["position_oz"].to_numpy(float) / res.config.contract_oz
        elif f == "equity_ex_financing":
            out[f] = (b["equity"].to_numpy(float)
                      - b["swap_cum"].to_numpy(float)) if "swap_cum" in b else np.nan
        elif f in b.columns:
            out[f] = b[f].to_numpy(float)
        else:
            out[f] = np.nan
    return out


def _tol_for(field: str) -> float:
    if field in PRICE_FIELDS:
        return TOL["price"]
    if field in QTY_FIELDS:
        return TOL["qty"]
    if field in ATR_FIELDS:
        return TOL["atr"]
    if field in EQ_FIELDS:
        return TOL["equity"]
    return 0.0


def reconcile(res, pine_path: str, outdir: str, tag: str) -> Dict:
    pine = load_pine_export(pine_path)
    py = python_frame(res)
    j = py.merge(pine, on="time_utc", how="inner", suffixes=("_py", "_tv"))
    report: Dict = {
        "pine_export": os.path.abspath(pine_path),
        "pine_rows": int(len(pine)),
        "python_rows": int(len(py)),
        "overlapping_bars": int(len(j)),
        "fields_present": pine.attrs.get("found", []),
        "fields_missing_from_export": pine.attrs.get("missing", []),
        "tolerances": TOL,
    }
    if not len(j):
        report["result"] = "FAIL"
        report["reason"] = ("no overlapping bars. TradingView exports bar-open time "
                            "in UTC; check the export covers the same window.")
        return report

    rows: List[dict] = []
    per_field = {}
    for f in pine.attrs.get("found", []):
        a = j[f + "_py"].to_numpy(float)
        b = j[f + "_tv"].to_numpy(float)
        both = np.isfinite(a) & np.isfinite(b)
        one = np.isfinite(a) ^ np.isfinite(b)
        d = np.zeros(len(j))
        d[both] = np.abs(a[both] - b[both])
        tol = _tol_for(f)
        bad = (both & (d > tol)) | one
        per_field[f] = {"compared": int(both.sum()),
                        "mismatches": int(bad.sum()),
                        "max_abs_diff": float(d[both].max()) if both.any() else 0.0,
                        "tolerance": tol,
                        "nan_disagreements": int(one.sum())}
        for i in np.where(bad)[0]:
            rows.append({"time_utc": j["time_utc"].iloc[i], "field": f,
                         "python": a[i], "tradingview": b[i],
                         "abs_diff": d[i], "tolerance": tol})

    detail = pd.DataFrame(rows).sort_values(["time_utc", "field"]) if rows else pd.DataFrame(
        columns=["time_utc", "field", "python", "tradingview", "abs_diff", "tolerance"])
    os.makedirs(outdir, exist_ok=True)
    detail_path = os.path.join(outdir, "%s_reconcile_mismatches.csv" % tag)
    detail.to_csv(detail_path, index=False, encoding="utf-8")

    report["per_field"] = per_field
    report["mismatch_count"] = int(len(detail))
    report["first_mismatch"] = (
        {k: str(v) for k, v in detail.iloc[0].to_dict().items()} if len(detail) else None)
    report["max_price_diff"] = max(
        [v["max_abs_diff"] for k, v in per_field.items() if k in PRICE_FIELDS] or [0.0])
    report["max_qty_diff"] = max(
        [v["max_abs_diff"] for k, v in per_field.items() if k in QTY_FIELDS] or [0.0])
    report["detail_csv"] = detail_path
    report["result"] = "PASS" if len(detail) == 0 else "FAIL"
    report["statement"] = (
        "Pine and Python are RECONCILED bar by bar within the documented tolerances."
        if report["result"] == "PASS" else
        "NOT RECONCILED. %d field-bar mismatches. Do not describe the two "
        "implementations as agreeing until this is zero." % len(detail))
    if pine.attrs.get("missing"):
        report["statement"] += (
            "  Note: %d debug fields were absent from the export and were not "
            "compared - a PASS covers only the fields present."
            % len(pine.attrs["missing"]))
    return report
