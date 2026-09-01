# -*- coding: utf-8 -*-
"""Simulation engine.

Bars are BID (the MT5 convention this repo uses everywhere): a buy fills at
open + spread, a sell fills at open. Slippage is adverse on both sides.

The engine keeps two books that must reconcile:
  * VIRTUAL   - three sleeves, each with its own direction, size, frozen entry
                ATR and immovable stop. Gross P&L is measured at reference
                prices (the raw open, or the stop price for a replayed stop).
  * ACTUAL    - one netted broker position. Only the delta between the current
                position and the new net target is ever traded, so opposing
                sleeves offset internally and no artificial spread is paid.
Execution costs and financing are booked on the ACTUAL position and then
attributed back to sleeves pro rata. The residual between the two books is
reported rather than hidden - it is lot-step rounding on the net target.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import REASON_CODES
from .config import RunConfig
from .dataio import index_ltf
from .financing import ChargeEvent, FinancingModel, attribute as attribute_financing
from .indicators import donchian, floor_step, pine_atr
from .sizing import CostModel, decide as size_decide, total_open_risk
from .sleeves import (EV_ENTRY_LONG, EV_ENTRY_SHORT, EV_EXIT_CHANNEL,
                      EV_EXIT_DIRECTION, EV_EXIT_END, EV_EXIT_STOP,
                      EV_EXIT_STOP_GAP, EV_NONE, Sleeve, phase_exit_entry,
                      phase_fill, phase_stop)

SLEEVE_ORDER = ("fast", "medium", "slow")


# --------------------------------------------------------------------------
@dataclass
class Fill:
    time: object
    kind: str                 # open_order | intrabar_stop | final_close
    ref_price: float
    fill_price: float
    delta_oz: float
    spread_slip_usd: float
    commission_usd: float
    attribution: Dict[str, float] = field(default_factory=dict)
    note: str = ""


class Ledger:
    """Cash and position accounting for the single netted broker position."""

    def __init__(self, capital: float, contract_oz: float,
                 commission_per_lot_rt: float, use_costs: bool):
        self.capital = float(capital)
        self.contract_oz = float(contract_oz)
        self.comm_per_oz_side = (commission_per_lot_rt / 2.0) / contract_oz
        self.use_costs = use_costs
        self.pos_oz = 0.0
        self.avg_px = 0.0
        self.realized = 0.0
        self.cost_spread_slip = 0.0
        self.cost_commission = 0.0
        self.cost_swap = 0.0
        self.turnover_oz = 0.0
        self.turnover_notional = 0.0
        self.fills: List[Fill] = []
        self.sleeve_costs: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------
    def _accrue_sleeve(self, name: str, key: str, amount: float) -> None:
        self.sleeve_costs.setdefault(
            name, {"spread_slip": 0.0, "commission": 0.0, "swap": 0.0})[key] += amount

    def execute(self, when, delta_oz: float, ref_price: float, spread_price: float,
                slip_price: float, attribution: Dict[str, float],
                kind: str = "open_order", note: str = "") -> Optional[Fill]:
        """Trade `delta_oz` ounces against `ref_price`. Returns the Fill."""
        if delta_oz == 0.0:
            return None
        qty = abs(delta_oz)
        if self.use_costs:
            fill = (ref_price + spread_price + slip_price if delta_oz > 0
                    else ref_price - slip_price)
        else:
            fill = ref_price
        exec_cost = abs(fill - ref_price) * qty
        comm = self.comm_per_oz_side * qty if self.use_costs else 0.0

        if self.use_costs:
            self.cost_spread_slip += exec_cost
            self.cost_commission += comm
            self.realized -= comm

        # position / average price
        if self.pos_oz == 0.0 or np.sign(self.pos_oz) == np.sign(delta_oz):
            self.avg_px = ((self.avg_px * abs(self.pos_oz) + fill * qty)
                           / (abs(self.pos_oz) + qty))
        else:
            closed = min(abs(self.pos_oz), qty)
            self.realized += closed * (fill - self.avg_px) * np.sign(self.pos_oz)
            if qty > abs(self.pos_oz):
                self.avg_px = fill
        self.pos_oz += delta_oz
        if abs(self.pos_oz) < 1e-9:
            self.pos_oz = 0.0
            self.avg_px = 0.0

        self.turnover_oz += qty
        self.turnover_notional += qty * ref_price

        # attribute execution cost to the sleeves that caused the change
        total_w = sum(abs(v) for v in attribution.values())
        if total_w > 0:
            for nm, w in attribution.items():
                share = abs(w) / total_w
                self._accrue_sleeve(nm, "spread_slip", exec_cost * share)
                self._accrue_sleeve(nm, "commission", comm * share)
        elif exec_cost or comm:
            self._accrue_sleeve("unattributed", "spread_slip", exec_cost)
            self._accrue_sleeve("unattributed", "commission", comm)

        f = Fill(time=when, kind=kind, ref_price=ref_price, fill_price=fill,
                 delta_oz=delta_oz, spread_slip_usd=exec_cost,
                 commission_usd=comm, attribution=dict(attribution), note=note)
        self.fills.append(f)
        return f

    def equity(self, mark_price: float) -> float:
        return self.capital + self.realized + self.pos_oz * (mark_price - self.avg_px)


# --------------------------------------------------------------------------
def net_target(sleeves: List[Sleeve], equity: float, price: float,
               cfg: RunConfig) -> Tuple[float, float, float, bool]:
    """Signed sum of sleeve lots -> notional cap -> floor to the lot step."""
    raw = sum(s.dir * s.lots for s in sleeves)
    cap_lots = ((equity * cfg.max_notional_x) / (cfg.contract_oz * price)
                if price > 0 else 0.0)
    clipped = min(abs(raw), cap_lots)
    net = np.sign(raw) * floor_step(clipped, cfg.lot_step)
    capped = abs(raw) - abs(net) > cfg.lot_step / 2
    return float(net), float(raw), float(cap_lots), bool(capped)


# --------------------------------------------------------------------------
@dataclass
class EngineResult:
    config: RunConfig
    bars: pd.DataFrame
    trades: pd.DataFrame
    fills: pd.DataFrame
    financing: pd.DataFrame
    sleeve_financing: pd.DataFrame
    sizing: pd.DataFrame
    equity: pd.Series
    diagnostics: Dict
    sleeve_costs: Dict


def run_engine(h4: pd.DataFrame, cfg: RunConfig,
               fin: FinancingModel,
               ltf: Optional[pd.DataFrame] = None,
               events: Optional[pd.DataFrame] = None) -> EngineResult:
    cfg.validate()
    if cfg.stop_mode == "ltf" and ltf is None:
        raise ValueError("stop_mode='ltf' requires lower-timeframe bars; none were "
                         "supplied to run_engine()")
    t = h4["time"].to_numpy()
    t_utc = pd.DatetimeIndex(h4["time_utc"])
    t_srv = pd.DatetimeIndex(h4["time_server"])
    o = h4["open"].to_numpy(float)
    hi = h4["high"].to_numpy(float)
    lo = h4["low"].to_numpy(float)
    c = h4["close"].to_numpy(float)
    spread_pts = h4["spread"].to_numpy(float)
    n = len(h4)

    atr = pine_atr(hi, lo, c, cfg.atr_len)
    defs = cfg.sleeve_defs()
    sleeves = [Sleeve(name=nm, entry_len=e, exit_len=x) for nm, e, x in defs]
    by_name = {s.name: s for s in sleeves}
    order = [nm for nm in SLEEVE_ORDER if nm in by_name]

    levels = {}
    for s in sleeves:
        levels[(s.name, "ent")] = donchian(hi, lo, s.entry_len, cfg.lookahead_audit)
        levels[(s.name, "exit")] = donchian(hi, lo, s.exit_len, cfg.lookahead_audit)

    # ---- calendar filters ------------------------------------------------
    basis = t_utc
    if cfg.friday_basis == "close":
        # the signal is produced at the bar's CLOSE, which is what the Pine
        # tests. v1 tested the bar's OPEN; see CHANGELOG entry FRIDAY-BASIS.
        width = pd.Timedelta(seconds=int(np.median(np.diff(t).astype("timedelta64[s]")
                                                   .astype(float)))) if n > 1 else pd.Timedelta(hours=4)
        basis = t_utc + width
    ny = basis.tz_localize("UTC").tz_convert("America/New_York")
    friday_block = (np.asarray((ny.dayofweek == 4) & (ny.hour >= cfg.friday_hour))
                    if cfg.friday_filter else np.zeros(n, dtype=bool))

    event_block = np.zeros(n, dtype=bool)
    event_name = np.array([""] * n, dtype=object)
    if events is not None and len(events):
        tv = t_utc.to_numpy()
        for r in events.itertuples():
            m = (tv >= np.datetime64(r.start_utc)) & (tv <= np.datetime64(r.end_utc))
            event_block |= m
            event_name[m] = r.event_name
    entry_event_block = event_block if cfg.event_mode == "block-new-entries" else np.zeros(n, bool)

    # ---- lower-timeframe index ------------------------------------------
    ltf_start = ltf_end = None
    coverage = None
    if cfg.stop_mode == "ltf" and ltf is not None:
        ltf_start, ltf_end, coverage = index_ltf(h4, ltf)
        lo_o = ltf["open"].to_numpy(float)
        lo_h = ltf["high"].to_numpy(float)
        lo_l = ltf["low"].to_numpy(float)
        lo_t = ltf["time"].to_numpy()
        lo_sp = ltf["spread"].to_numpy(float)

    led = Ledger(cfg.capital, cfg.contract_oz, cfg.commission_per_lot_rt, cfg.use_costs)
    pending_delta = 0.0
    pending_attr: Dict[str, float] = {}
    trades: List[dict] = []
    sizing_log: List[dict] = []
    charges: List[ChargeEvent] = []
    charge_sleeve_rows: List[dict] = []

    warm = max([s.entry_len for s in sleeves] + [cfg.atr_len]) + 2
    cost_scale = cfg.cost_scale
    slip_price = cfg.slippage_points * cfg.point * cost_scale
    ltf_slip = cfg.ltf_stop_slippage_points * cfg.point * cost_scale

    # per-bar debug / reconciliation frame
    dbg = {k: np.full(n, np.nan) for k in
           ("atr", "net_target_lots", "net_raw_lots", "cap_lots", "position_oz",
            "equity", "unrealized", "swap_cum")}
    for nm in SLEEVE_ORDER:
        dbg["state_" + nm] = np.zeros(n)
        dbg["qty_" + nm] = np.zeros(n)
        dbg["stop_" + nm] = np.full(n, np.nan)
        dbg["ent_hi_" + nm] = np.full(n, np.nan)
        dbg["ent_lo_" + nm] = np.full(n, np.nan)
        dbg["exit_hi_" + nm] = np.full(n, np.nan)
        dbg["exit_lo_" + nm] = np.full(n, np.nan)
        dbg["reason_" + nm] = np.zeros(n)
    dbg_capped = np.zeros(n, bool)

    diag = dict(cap_binding_bars=0, lot_rounding_loss_lots=0.0,
                sizing_intended_lots=0.0, sizing_executed_lots=0.0,
                sizing_rounding_loss_lots=0.0, entries_refused_unsizable=0,
                intended_exposure_lots=0.0, executed_exposure_lots=0.0,
                override_accepted=0, override_rejected_sleeve=0,
                override_rejected_portfolio=0, override_disabled_skips=0,
                stops_h4_approx=0, stops_ltf_exact=0, stops_ltf_gap=0,
                ltf_ambiguous_events=0, ltf_missing_h4_bars=0,
                ambiguity_log=[], trades_near_events=0)

    def sleeve_lots_map():
        return {s.name: s.dir * s.lots for s in sleeves}

    def close_sleeve(s: Sleeve, snap: dict, exit_px: float, exit_time,
                     reason: int, i: int, ambiguous: bool = False):
        stop_dist = snap["atr_ent"] * cfg.atr_mult
        pts = (exit_px - snap["entry_px"]) * snap["dir"]
        trades.append(dict(
            sleeve=s.name,
            direction="long" if snap["dir"] == 1 else "short",
            entry_time=snap["entry_time"], exit_time=exit_time,
            entry_price=snap["entry_px"], exit_price=exit_px,
            lots=snap["lots"], atr_at_entry=snap["atr_ent"],
            stop_price=snap["stop_px"], stop_dist=stop_dist,
            reason=REASON_CODES[reason], reason_code=reason,
            points=pts, gross=pts * snap["lots"] * cfg.contract_oz,
            r_multiple=pts / stop_dist if stop_dist > 0 else np.nan,
            entry_bar=snap["entry_bar"], exit_bar=i, ambiguous=ambiguous,
            event_window=str(event_name[i]) if event_name[i] else ""))
        if event_name[i]:
            diag["trades_near_events"] += 1

    # ======================================================================
    for i in range(n):
        pos_before = led.pos_oz
        spread_price = spread_pts[i] * cfg.point * cost_scale

        # ---- financing, charged on the position held overnight -----------
        def charge_financing(position_oz: float):
            if position_oz == 0.0 or i == 0 or not cfg.use_costs:
                return
            direction = 1 if position_oz > 0 else -1
            lots = abs(position_oz) / cfg.contract_oz
            evs = fin.charge(t_srv[i - 1], t_srv[i], lots, direction)
            for e in evs:
                led.realized += e.amount
                led.cost_swap += e.amount
                charges.append(e)
            if evs:
                # virtual-sleeve attribution: what each sleeve WOULD have paid
                # standalone. Reconciled against the actual charge below.
                for s in sleeves:
                    if s.dir == 0 or s.lots == 0:
                        continue
                    for e in evs:
                        rate, _src = fin.rate_for(e.date, s.dir, count=False)
                        amt = rate * s.lots
                        led._accrue_sleeve(s.name, "swap", amt)
                        charge_sleeve_rows.append(dict(
                            date=e.date, sleeve=s.name, direction=s.dir,
                            lots=s.lots, rate_per_lot=rate, amount=amt,
                            lot_nights=s.lots))

        if cfg.financing_timing == "pre-fill":
            charge_financing(pos_before)

        # ---- 1. execute the order queued on the previous bar's close ------
        if pending_delta != 0.0:
            led.execute(t[i], pending_delta, o[i], spread_price, slip_price,
                        pending_attr, kind="open_order")
            pending_delta = 0.0
            pending_attr = {}

        # ---- 2. register fills for sleeves that entered last bar ----------
        for nm in order:
            phase_fill(by_name[nm], o[i], t[i], i, cfg.atr_mult)

        if cfg.financing_timing != "pre-fill":
            charge_financing(led.pos_oz)

        eq = led.equity(c[i])
        dbg["equity"][i] = eq
        dbg["swap_cum"][i] = led.cost_swap
        dbg["position_oz"][i] = led.pos_oz
        dbg["unrealized"][i] = led.pos_oz * (c[i] - led.avg_px)
        dbg["atr"][i] = atr[i]

        if i < warm or not np.isfinite(atr[i]):
            for s in sleeves:
                dbg["state_" + s.name][i] = s.dir
                dbg["qty_" + s.name][i] = s.dir * s.lots
            continue

        # ---- 3. slow-sleeve confirmation snapshot -------------------------
        slow = by_name.get("slow")
        slow_confirmed_short = bool(slow is not None and slow.dir == -1 and slow.confirmed)

        # ---- 4. lower-timeframe stop replay -------------------------------
        if cfg.stop_mode == "ltf" and ltf_start is not None:
            a, b = int(ltf_start[i]), int(ltf_end[i])
            if b <= a:
                diag["ltf_missing_h4_bars"] += 1
            else:
                for j in range(a, b):
                    touched = []
                    for nm in order:
                        s = by_name[nm]
                        if s.dir == 0 or not np.isfinite(s.stop_px):
                            continue
                        hit = (lo_l[j] <= s.stop_px) if s.dir == 1 else (lo_h[j] >= s.stop_px)
                        if hit:
                            touched.append(nm)
                    if not touched:
                        continue
                    ambiguous = len(touched) > 1
                    if ambiguous:
                        diag["ltf_ambiguous_events"] += 1
                        diag["ambiguity_log"].append(dict(
                            time=str(pd.Timestamp(lo_t[j])), sleeves=list(touched),
                            rule="all filled at their own stop price, adverse "
                                 "slippage, fixed order fast->medium->slow"))
                    for nm in touched:
                        s = by_name[nm]
                        gapped = ((lo_o[j] <= s.stop_px) if s.dir == 1
                                  else (lo_o[j] >= s.stop_px))
                        ref = lo_o[j] if gapped else s.stop_px
                        reason = EV_EXIT_STOP_GAP if gapped else EV_EXIT_STOP
                        diag["stops_ltf_gap" if gapped else "stops_ltf_exact"] += 1
                        snap = s.snapshot()
                        s.reset()
                        close_sleeve(s, snap, ref, lo_t[j], reason, i, ambiguous)
                        dbg["reason_" + nm][i] = reason
                        eq_now = led.equity(ref)
                        tgt, raw, cap, capped = net_target(sleeves, eq_now, ref, cfg)
                        delta = tgt * cfg.contract_oz - led.pos_oz
                        if abs(delta) >= cfg.contract_oz * cfg.lot_step * 0.5:
                            led.execute(lo_t[j], delta, ref,
                                        lo_sp[j] * cfg.point * cost_scale, ltf_slip,
                                        {nm: snap["lots"]}, kind="intrabar_stop",
                                        note=REASON_CODES[reason])

        # ---- 5. close-of-bar evaluation -----------------------------------
        # The NORMAL target. The override caps never feed this number.
        risk_cash = eq * cfg.effective_target_risk_pct() / 100.0
        cost_model = CostModel(
            spread_price=spread_price,
            entry_slip_price=slip_price,
            stop_slip_price=cfg.stop_exit_slippage_points * cfg.point * cost_scale,
            commission_per_oz_side=(led.comm_per_oz_side if cfg.use_costs else 0.0))

        def make_decider(bar_i, equity_now):
            def _decide(name, direction, atr_now, rcash):
                # gross open risk EXCLUDING this sleeve - it is flat or about to
                # be replaced, so counting its old position would double-count
                others = [x for x in sleeves if x.name != name]
                d = size_decide(
                    sleeve_name=name, direction=direction, atr=atr_now,
                    atr_mult=cfg.atr_mult, equity=equity_now, risk_cash=rcash,
                    price=c[bar_i], contract_oz=cfg.contract_oz,
                    lot_step=cfg.lot_step, minimum_lot=cfg.minimum_lot,
                    costs=cost_model, sleeves=others,
                    enable_override=cfg.enable_min_lot_override,
                    override_max_risk_pct=cfg.override_max_risk_pct_per_sleeve,
                    max_total_open_risk_pct=cfg.max_total_open_risk_pct,
                    tick_size=cfg.tick_size, tick_value=cfg.tick_value,
                    when=t[bar_i])
                row = d.to_dict()
                row["bar"] = bar_i
                sizing_log.append(row)
                if d.reason == "ORDER_ACCEPTED_MINIMUM_OVERRIDE":
                    diag["override_accepted"] += 1
                elif d.reason == "OVERRIDE_SLEEVE_RISK_EXCEEDED":
                    diag["override_rejected_sleeve"] += 1
                elif d.reason == "PORTFOLIO_OPEN_RISK_EXCEEDED":
                    diag["override_rejected_portfolio"] += 1
                elif d.reason == "OVERRIDE_DISABLED":
                    diag["override_disabled_skips"] += 1
                return d
            return _decide

        decider = make_decider(i, eq)
        blocked = bool(friday_block[i] or entry_event_block[i])
        for nm in order:
            s = by_name[nm]
            eh, el = levels[(nm, "ent")]
            xh, xl = levels[(nm, "exit")]
            dbg["ent_hi_" + nm][i], dbg["ent_lo_" + nm][i] = eh[i], el[i]
            dbg["exit_hi_" + nm][i], dbg["exit_lo_" + nm][i] = xh[i], xl[i]

            prior = EV_NONE
            if cfg.stop_mode == "h4":
                prior = phase_stop(s, lo[i], hi[i])
                if prior == EV_EXIT_STOP:
                    diag["stops_h4_approx"] += 1
            snap = s.snapshot()
            xev, nev = phase_exit_entry(
                s, c[i], eh[i], el[i], xh[i], xl[i], atr[i], risk_cash,
                cfg.contract_oz, cfg.lot_step, cfg.atr_mult, blocked,
                cfg.allow_reversal, cfg.direction_mode, slow_confirmed_short, prior,
                v1_compat=cfg.v1_compat, decide_fn=decider)

            # DEFECT-V1-TRADELOG (fixed here, reproducible with v1_compat):
            # v1 snapshotted the sleeve BEFORE registering this bar's fill, so a
            # position that opened at this bar's open and closed at its close was
            # executed by the ledger but never written to the trade log.
            v1_drop = cfg.v1_compat and snap["entry_bar"] == i
            if (xev != EV_NONE and snap["dir"] != 0
                    and np.isfinite(snap["entry_px"]) and not v1_drop):
                px_out = c[i] if cfg.same_bar_fill_audit else (o[i + 1] if i + 1 < n else c[i])
                tm_out = t[i] if cfg.same_bar_fill_audit else (t[i + 1] if i + 1 < n else t[i])
                close_sleeve(s, snap, px_out, tm_out, xev, i)
            if xev != EV_NONE:
                dbg["reason_" + nm][i] = xev
            if nev != EV_NONE:
                dbg["reason_" + nm][i] = nev
                # sleeve-level sizing quantisation: the lot step always rounds
                # DOWN, so every entry gives up a fraction of its risk budget
                diag["sizing_intended_lots"] += s.raw_lots
                diag["sizing_executed_lots"] += s.lots
                diag["sizing_rounding_loss_lots"] += max(0.0, s.raw_lots - s.lots)

        # ---- 6. netting ---------------------------------------------------
        tgt, raw, cap_lots, capped = net_target(sleeves, eq, c[i], cfg)
        dbg["net_target_lots"][i] = tgt
        dbg["net_raw_lots"][i] = raw
        dbg["cap_lots"][i] = cap_lots
        dbg_capped[i] = capped
        if capped:
            diag["cap_binding_bars"] += 1
        diag["intended_exposure_lots"] += abs(raw)
        diag["executed_exposure_lots"] += abs(tgt)
        diag["lot_rounding_loss_lots"] += max(
            0.0, min(abs(raw), cap_lots) - abs(tgt))

        for s in sleeves:
            dbg["state_" + s.name][i] = s.dir
            dbg["qty_" + s.name][i] = s.dir * s.lots
            dbg["stop_" + s.name][i] = s.stop_px

        delta = tgt * cfg.contract_oz - led.pos_oz
        if abs(delta) >= cfg.contract_oz * cfg.lot_step * 0.5:
            # Execution cost is allocated by each sleeve's share of the gross
            # sleeve exposure that produced this target. A flat sleeve
            # contributes nothing and is charged nothing.
            attr = {s.name: abs(s.dir * s.lots) for s in sleeves if s.dir != 0}
            attr = attr or {"unattributed": 1.0}
            if cfg.same_bar_fill_audit:
                led.execute(t[i], delta, c[i], spread_price, slip_price, attr,
                            kind="open_order", note="same_bar_fill_audit")
            else:
                pending_delta = delta
                pending_attr = attr

    # ---- close whatever is still open ------------------------------------
    if led.pos_oz != 0.0:
        ref = c[-1]
        attr = {s.name: abs(s.dir * s.lots) for s in sleeves if s.dir != 0} or {"unattributed": 1.0}
        if cfg.close_final_position_with_costs:
            led.execute(t[-1], -led.pos_oz, ref,
                        spread_pts[-1] * cfg.point * cost_scale, slip_price,
                        attr, kind="final_close", note="end_of_data")
        else:
            led.realized += led.pos_oz * (ref - led.avg_px)
            led.pos_oz = 0.0
            led.avg_px = 0.0
        dbg["equity"][-1] = led.capital + led.realized
    if cfg.log_open_sleeves_at_end:
        for s in sleeves:
            if s.dir != 0 and np.isfinite(s.entry_px):
                close_sleeve(s, s.snapshot(), c[-1], t[-1], EV_EXIT_END, n - 1)
                s.reset()

    # ---- assemble ---------------------------------------------------------
    bars = pd.DataFrame({"time": t, "time_utc": t_utc, "time_server": t_srv,
                         "open": o, "high": hi, "low": lo, "close": c,
                         "spread": spread_pts, "friday_block": friday_block,
                         "event_block": event_block, "event_name": event_name,
                         "cap_binding": dbg_capped})
    for k, v in dbg.items():
        bars[k] = v

    # A run that takes no trades must still emit the columns - an empty CSV with
    # no header is unreadable downstream, and "no trades" is a legitimate and
    # important result (it is what a $10k account does at 0.10 % risk).
    TRADE_COLS = ["sleeve", "direction", "entry_time", "exit_time", "entry_price",
                  "exit_price", "lots", "atr_at_entry", "stop_price", "stop_dist",
                  "reason", "reason_code", "points", "gross", "r_multiple",
                  "entry_bar", "exit_bar", "ambiguous", "event_window"]
    tr = pd.DataFrame(trades, columns=None if trades else TRADE_COLS)
    if len(tr):
        tr = tr.sort_values(["exit_time", "sleeve"]).reset_index(drop=True)

    fills_df = pd.DataFrame([{
        "time": f.time, "kind": f.kind, "ref_price": f.ref_price,
        "fill_price": f.fill_price, "delta_oz": f.delta_oz,
        "delta_lots": f.delta_oz / cfg.contract_oz,
        "spread_slip_usd": f.spread_slip_usd, "commission_usd": f.commission_usd,
        "attribution": ";".join("%s=%.4f" % (k, v) for k, v in sorted(f.attribution.items())),
        "note": f.note} for f in led.fills])

    SIZING_COLS = ["time", "bar", "sleeve", "direction", "equity", "atr",
                   "entry_price", "stop_price", "stop_distance", "raw_lots",
                   "rounded_lots", "final_lots", "minimum_lot", "lot_step",
                   "override_considered", "override_used", "price_stop_loss",
                   "estimated_entry_cost", "estimated_exit_cost",
                   "estimated_costs", "actual_stop_risk", "actual_stop_risk_pct",
                   "total_open_risk_before", "total_open_risk_after",
                   "total_open_risk_pct_before", "total_open_risk_pct_after",
                   "condition", "reason"]
    sizing_df = pd.DataFrame(sizing_log, columns=None if sizing_log else SIZING_COLS)
    if len(sizing_df):
        sizing_df = sizing_df[[c for c in SIZING_COLS if c in sizing_df.columns]]

    fin_df = attribute_financing(charges)
    sleeve_fin = pd.DataFrame(charge_sleeve_rows)

    eq_series = pd.Series(bars["equity"].to_numpy(), index=t_utc).ffill().dropna()

    diag.update(
        final_equity=float(led.capital + led.realized),
        net_profit=float(led.realized),
        cost_spread_slip=float(led.cost_spread_slip),
        cost_commission=float(led.cost_commission),
        cost_swap=float(led.cost_swap),
        turnover_oz=float(led.turnover_oz),
        turnover_notional=float(led.turnover_notional),
        n_fills=int(len(led.fills)),
        financing_coverage=fin.coverage_report(),
        ltf_coverage=coverage.to_dict() if coverage else None,
        labels=cfg.labels(),
    )
    diag["ambiguity_log"] = diag["ambiguity_log"][:200]

    return EngineResult(config=cfg, bars=bars, trades=tr, fills=fills_df,
                        financing=fin_df, sleeve_financing=sleeve_fin,
                        sizing=sizing_df,
                        equity=eq_series, diagnostics=diag,
                        sleeve_costs=dict(led.sleeve_costs))
