# -*- coding: utf-8 -*-
"""Trend campaigns - the unit of independent evidence.

Three sleeves entering the same gold rally are not three pieces of evidence.
They are one bet expressed three times, and a t-test over 304 overlapping
sleeve trades quietly assumes otherwise. A campaign is the smallest window in
which the strategy is either in the market or not, so campaigns are much
closer to independent than sleeve trades are - and there are far fewer of
them, which is the honest cost of saying so.

DEFINITION (as specified)
    A campaign starts when the first sleeve goes from flat to non-flat and
    ends when every sleeve is flat again. If the aggregate net position
    crosses from long to short without passing through flat, the campaign is
    closed at the last bar of the old sign and a new one opens on the bar the
    sign changes.

COST AND P&L ATTRIBUTION
    Gross is the sum of the gross of every sleeve trade whose ENTRY falls in
    the campaign window. Execution costs come from the fills timestamped in
    the window, financing from the rollovers dated in it. Campaign R is net
    P&L divided by the campaign's total initial risk - the sum of
    stop_distance x lots x contract_size over its constituent trades - so an
    R of +1 means the campaign made back exactly what it had put at risk.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

SLEEVE_COLS = ("state_fast", "state_medium", "state_slow")


def build_campaigns(bars: pd.DataFrame, trades: pd.DataFrame,
                    fills: pd.DataFrame, financing: pd.DataFrame,
                    contract_oz: float = 100.0) -> pd.DataFrame:
    present = [c for c in SLEEVE_COLS if c in bars.columns]
    states = bars[present].to_numpy(float)
    active = (np.abs(states) > 0).any(axis=1)
    net = bars["net_target_lots"].to_numpy(float)
    net = np.nan_to_num(net)
    t = pd.DatetimeIndex(bars["time"])
    eq = bars["equity"].to_numpy(float)
    n = len(bars)

    spans: List[Dict] = []
    start: Optional[int] = None
    cur_sign = 0
    for i in range(n):
        s = int(np.sign(net[i]))
        if start is None:
            if active[i]:
                start = i
                cur_sign = s
            continue
        # direction flip without passing through flat -> boundary
        if s != 0 and cur_sign != 0 and s != cur_sign:
            spans.append({"start": start, "end": i - 1, "reason": "reversal"})
            start, cur_sign = i, s
            continue
        if s != 0:
            cur_sign = s
        if not active[i]:
            spans.append({"start": start, "end": i, "reason": "flat"})
            start, cur_sign = None, 0
    if start is not None:
        spans.append({"start": start, "end": n - 1, "reason": "end_of_data"})

    tr_entry = pd.DatetimeIndex(trades["entry_time"]) if len(trades) else pd.DatetimeIndex([])
    fl_time = pd.DatetimeIndex(fills["time"]) if len(fills) else pd.DatetimeIndex([])
    fi_date = pd.DatetimeIndex(financing["date"]) if len(financing) else pd.DatetimeIndex([])

    rows = []
    for k, sp in enumerate(spans):
        a, b = sp["start"], sp["end"]
        t0, t1 = t[a], t[b]
        seg = slice(a, b + 1)

        if len(trades):
            m = (tr_entry >= t0) & (tr_entry <= t1)
            tsub = trades[m]
        else:
            tsub = trades
        gross = float(tsub["gross"].sum()) if len(tsub) else 0.0
        risk_cash = float((tsub["stop_dist"] * tsub["lots"] * contract_oz).sum()) if len(tsub) else 0.0

        if len(fills):
            fm = (fl_time >= t0) & (fl_time <= t1)
            exec_cost = float(fills.loc[fm, "spread_slip_usd"].sum())
            comm = float(fills.loc[fm, "commission_usd"].sum())
        else:
            exec_cost = comm = 0.0
        if len(financing):
            fim = (fi_date >= t0.normalize()) & (fi_date <= t1.normalize())
            swap = float(financing.loc[fim, "amount"].sum())
            lot_nights = float(financing.loc[fim, "lot_nights"].sum())
        else:
            swap = lot_nights = 0.0

        net_pnl = gross - exec_cost - comm + swap
        eqseg = eq[seg]
        eqseg = eqseg[np.isfinite(eqseg)]
        base = eqseg[0] if len(eqseg) else np.nan
        exc = eqseg - base if len(eqseg) else np.array([0.0])

        sleeves_in = sorted({c.replace("state_", "") for c in present
                             if np.abs(states[seg, present.index(c)]).max() > 0})
        gross_expo = np.abs(np.nan_to_num(
            bars.loc[seg, [c.replace("state_", "qty_") for c in present]].to_numpy(float))).sum(axis=1)

        rows.append(dict(
            campaign_id=k,
            start=t0, end=t1, close_reason=sp["reason"],
            direction=("long" if np.nanmean(np.sign(net[seg])) > 0 else
                       "short" if np.nanmean(np.sign(net[seg])) < 0 else "mixed"),
            bars=b - a + 1,
            days=float((t1 - t0).total_seconds() / 86400.0),
            sleeves=",".join(sleeves_in), n_sleeves=len(sleeves_in),
            trades=int(len(tsub)),
            max_gross_exposure_lots=float(gross_expo.max()) if len(gross_expo) else 0.0,
            max_net_exposure_lots=float(np.nanmax(np.abs(net[seg]))) if b >= a else 0.0,
            gross_pnl=gross, spread_slip=exec_cost, commission=comm, swap=swap,
            lot_nights=lot_nights, net_pnl=net_pnl,
            risk_cash=risk_cash,
            gross_R=gross / risk_cash if risk_cash > 0 else np.nan,
            net_R=net_pnl / risk_cash if risk_cash > 0 else np.nan,
            mae_usd=float(exc.min()) if len(exc) else 0.0,
            mfe_usd=float(exc.max()) if len(exc) else 0.0,
            mae_R=float(exc.min() / risk_cash) if risk_cash > 0 and len(exc) else np.nan,
            mfe_R=float(exc.max() / risk_cash) if risk_cash > 0 and len(exc) else np.nan,
        ))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
def concentration(values: np.ndarray, label: str) -> Dict:
    """How much of the total comes from the few best, and what is left without them."""
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    total = float(v.sum())
    out = {"%s_n" % label: int(len(v)), "%s_total" % label: total,
           "%s_mean" % label: float(v.mean()) if len(v) else np.nan,
           "%s_median" % label: float(np.median(v)) if len(v) else np.nan}
    if not len(v):
        return out
    srt = np.sort(v)[::-1]
    for k in (1, 3, 5, 10):
        if len(v) >= k:
            top = float(srt[:k].sum())
            out["%s_top%d_sum" % (label, k)] = top
            out["%s_top%d_share_pct" % (label, k)] = (100.0 * top / total) if total else np.nan
            rest = v.sum() - top
            out["%s_excl_top%d_total" % (label, k)] = float(rest)
            out["%s_excl_top%d_mean" % (label, k)] = float(rest / (len(v) - k)) if len(v) > k else np.nan
    return out


def daily_frames(equity: pd.Series) -> (pd.DataFrame, pd.DataFrame):
    """Daily equity and daily returns, indexed on UTC calendar days."""
    eq = equity.dropna()
    daily = eq.resample("1D").last().dropna()
    dret = daily.pct_change().dropna()
    return (daily.to_frame("equity").rename_axis("date"),
            dret.to_frame("ret").rename_axis("date"))
