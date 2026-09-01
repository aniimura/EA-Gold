# -*- coding: utf-8 -*-
"""The virtual sleeve state machine.

v1 had this as one `step()` function. v2 splits it into explicit phases,
because the lower-timeframe stop replay has to run *between* the fill
registration and the close-of-bar evaluation, and because the
`slow-confirmed-shorts` direction mode needs a snapshot of slow-sleeve state
taken at a well-defined moment rather than whenever the loop happens to reach
that sleeve.

Phase order per H4 bar, identical in effect to v1:
    1. phase_fill          register the fill of an order sent last bar close
    2. (engine) snapshot slow-sleeve confirmation
    3. (engine) lower-timeframe stop replay, if enabled
    4. phase_stop          H4 stop approximation, if LTF replay is off
    5. phase_exit_entry    channel exit, direction gate, then entry
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .indicators import floor_step

EV_NONE = 0
EV_ENTRY_LONG = 1
EV_ENTRY_SHORT = 2
EV_EXIT_CHANNEL = 3
EV_EXIT_STOP = 4
EV_EXIT_DISABLED = 5
EV_EXIT_DIRECTION = 6
EV_EXIT_END = 7
EV_EXIT_STOP_GAP = 8


@dataclass
class Sleeve:
    """One virtual position. Never holds more than one; never averages in."""
    name: str
    entry_len: int
    exit_len: int
    dir: int = 0
    lots: float = 0.0
    entry_px: float = np.nan
    stop_px: float = np.nan
    atr_ent: float = np.nan
    pending: int = 0
    entry_time: object = None
    entry_bar: int = -1

    def reset(self) -> None:
        self.pending = 0
        self.dir = 0
        self.lots = 0.0
        self.entry_px = np.nan
        self.stop_px = np.nan
        self.atr_ent = np.nan
        self.entry_time = None
        self.entry_bar = -1

    def snapshot(self) -> dict:
        return dict(dir=self.dir, lots=self.lots, entry_px=self.entry_px,
                    stop_px=self.stop_px, atr_ent=self.atr_ent,
                    entry_time=self.entry_time, entry_bar=self.entry_bar)

    @property
    def confirmed(self) -> bool:
        """In a position whose fill has actually been registered."""
        return self.dir != 0 and self.pending == 0 and np.isfinite(self.entry_px)


# --------------------------------------------------------------------------
def phase_fill(sl: Sleeve, open_px: float, bar_time, bar_index: int,
               atr_mult: float) -> None:
    """Register the fill of an entry submitted on the previous bar's close.

    The fill price is this bar's open, which is known and final - no lookahead.
    The stop is set here, from the ATR frozen at the signal, and is never
    touched again for the life of the position.
    """
    if sl.pending != 1:
        return
    sl.entry_px = open_px
    sl.stop_px = (open_px - sl.atr_ent * atr_mult if sl.dir == 1
                  else open_px + sl.atr_ent * atr_mult)
    sl.entry_time = bar_time
    sl.entry_bar = bar_index
    sl.pending = 0


def phase_stop(sl: Sleeve, low: float, high: float) -> int:
    """H4 stop approximation: breach detected on the completed bar's range.

    The exit itself is filled by the engine at the next bar's open, which is
    the conservative reading - gap risk beyond the stop is absorbed in full and
    no favourable intrabar fill is ever assumed.
    """
    if sl.dir == 0 or not np.isfinite(sl.stop_px):
        return EV_NONE
    breached = (low <= sl.stop_px) if sl.dir == 1 else (high >= sl.stop_px)
    return EV_EXIT_STOP if breached else EV_NONE


def short_allowed(sleeve_name: str, direction_mode: str,
                  slow_short_confirmed: bool) -> bool:
    """Direction-mode gate for SHORT exposure. Long behaviour never changes.

    slow-confirmed-shorts state logic, stated exactly:
      * the slow sleeve's own short breakout is unrestricted;
      * fast and medium may hold a short only while the slow sleeve is in a
        CONFIRMED short - short, and its entry fill already registered, as of
        the start of this bar's evaluation (after phase_fill, before any exit
        or entry). A slow short that has only signalled and not yet filled is
        not confirmation;
      * the snapshot is taken once per bar for all sleeves, so the result does
        not depend on the order sleeves are iterated in;
      * when confirmation disappears, a fast or medium short is closed with
        reason EXIT_DIRECTION_MODE at the next eligible execution point, which
        is the next bar's open - the same point any other close-of-bar exit
        fills at.
    """
    if direction_mode == "symmetric":
        return True
    if direction_mode == "long-only":
        return False
    if direction_mode == "slow-confirmed-shorts":
        return True if sleeve_name == "slow" else bool(slow_short_confirmed)
    raise ValueError("unknown direction mode %r" % direction_mode)


def phase_direction_gate(sl: Sleeve, direction_mode: str,
                         slow_short_confirmed: bool) -> int:
    """Close a short the direction mode no longer permits."""
    if sl.dir != -1:
        return EV_NONE
    if short_allowed(sl.name, direction_mode, slow_short_confirmed):
        return EV_NONE
    return EV_EXIT_DIRECTION


def phase_exit_entry(sl: Sleeve, close: float, ent_hi: float, ent_lo: float,
                     ex_hi: float, ex_lo: float, atr_now: float,
                     risk_cash: float, contract_oz: float, lot_step: float,
                     atr_mult: float, entries_blocked: bool, allow_rev: bool,
                     direction_mode: str, slow_short_confirmed: bool,
                     prior_exit_ev: int, v1_compat: bool = False) -> (int, int):
    """Channel exit, direction gate, then entry. Returns (exit_ev, entry_ev).

    DEFECT-V1-EXIT-SWALLOW (fixed here, reproducible with v1_compat=True):
    v1 wrote `if new_dir != 0: ... else: ...` / `elif exit_ev != 0: sl.reset()`.
    Python evaluates the `if` once, so when a bar produced BOTH an exit and a
    reversal whose size floored to zero lots, v1 entered the first branch,
    zeroed new_dir inside it, and never reached the `elif` - leaving the sleeve
    holding a position it had just been told to close. It happened on 22 bars
    of the 2022-2026 record, always in high-ATR conditions where
    risk_cash / (2.5 * ATR * 100) fell below one lot increment."""
    exit_ev = prior_exit_ev

    # (C) Donchian channel exit
    if exit_ev == EV_NONE and sl.dir != 0 and np.isfinite(ex_hi) and np.isfinite(ex_lo):
        if (close < ex_lo) if sl.dir == 1 else (close > ex_hi):
            exit_ev = EV_EXIT_CHANNEL

    # (C2) direction-mode withdrawal - never blocks or delays a real exit
    if exit_ev == EV_NONE:
        exit_ev = phase_direction_gate(sl, direction_mode, slow_short_confirmed)

    # (D) entry. Flat sleeves only; never adds to an existing position.
    can_enter = ((sl.dir == 0 or exit_ev != EV_NONE)
                 and not entries_blocked
                 and np.isfinite(ent_hi) and np.isfinite(ent_lo)
                 and np.isfinite(atr_now) and atr_now > 0
                 and (exit_ev == EV_NONE or allow_rev))
    new_dir = 0
    if can_enter:
        if close > ent_hi:
            new_dir = 1
        elif close < ent_lo:
            new_dir = -1
        if new_dir == -1 and not short_allowed(sl.name, direction_mode,
                                               slow_short_confirmed):
            new_dir = 0

    entry_ev = EV_NONE
    unsized = False
    if new_dir != 0:
        stop_dist = atr_now * atr_mult
        risk_per_lot = stop_dist * contract_oz
        raw = (risk_cash / risk_per_lot) if risk_per_lot > 0 else 0.0
        lots = floor_step(raw, lot_step)
        if lots <= 0:
            new_dir = 0          # risk budget below one lot increment
            unsized = True       # see DEFECT-V1-EXIT-SWALLOW above
        else:
            sl.reset()
            sl.dir = new_dir
            sl.lots = lots
            sl.raw_lots = raw
            sl.atr_ent = atr_now
            sl.pending = 1
            entry_ev = EV_ENTRY_LONG if new_dir == 1 else EV_ENTRY_SHORT
    if new_dir == 0 and exit_ev != EV_NONE and not (v1_compat and unsized):
        sl.reset()
    return exit_ev, entry_ev
