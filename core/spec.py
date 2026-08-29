# -*- coding: utf-8 -*-
"""Strategy specification - the single source of truth.

A ``Strategy`` object is consumed by BOTH the Python backtester
(``core.backtest``) and the MQL5 code generator (``codegen.mq5gen``).  There is
no second place to edit, so the two implementations cannot diverge.

Execution semantics (deliberately restricted so that MT5 can match exactly):

  * One position at a time, fixed lot.
  * Signals are read from the last COMPLETED bar; the order fills at the OPEN
    of the following bar.
  * SL/TP are attached to the position and triggered broker-side (intrabar).
  * Time exit and signal exit are evaluated at a bar open, before any new
    entry is considered.
  * After ANY exit, no re-entry on the same bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .expr import PRICE_FIELDS, TIME_FIELDS, compile_expr
from .indicators import HTF, Indicator
from .timeutil import DST_EU
from .types import TIMEFRAMES

LONG = "long"
SHORT = "short"


class SpecError(ValueError):
    pass


@dataclass
class Costs:
    """Execution costs the Python engine should book, per 1.0 lot.

    Leave at zero during exploration, then run one reconciliation pass: the
    report prints the values MT5 actually charged, ready to paste back here.
    Without them a Python-only P/L can differ from MT5 by more than the edge
    itself - on the bundled demo strategy the sign of the result flips.
    """
    commission_per_lot: float = 0.0        # round trip, account currency
    swap_long_per_lot_night: float = 0.0   # signed; usually negative
    swap_short_per_lot_night: float = 0.0
    triple_swap_weekday: int = 2           # pandas weekday, 2 == Wednesday

    def any(self) -> bool:
        return bool(self.commission_per_lot or self.swap_long_per_lot_night
                    or self.swap_short_per_lot_night)


@dataclass
class Trail:
    """Profit-locking trailing stop, expressed in account currency.

    Once open profit measured at the best price of a CLOSED bar reaches
    ``start_money``, the stop is moved to lock in ``peak - step_money``.  It
    only ever moves in the profitable direction and never below break-even.

    Because the trigger is a money amount, it depends on position size - which
    is exactly how a risk-sized scalper is usually specified.
    """
    start_money: float = 0.0
    step_money: float = 0.0

    def active(self) -> bool:
        return self.start_money > 0.0


@dataclass
class Sizing:
    """Position size.

    ``mode='fixed'``  - always ``Strategy.lot``.
    ``mode='risk'``   - size so that being stopped out costs ``risk_money``:
                        lots = floor(risk / (sl_distance * contract_size) / step) * step
                        and the trade is skipped when that is below ``lot_min``.
    """
    mode: str = "fixed"
    risk_money: float = 0.0
    lot_step: float = 0.01
    lot_min: float = 0.01
    lot_max: float = 100.0

    def is_risk(self) -> bool:
        return self.mode == "risk"


@dataclass
class Exits:
    """Stop loss / take profit / time exit definition.

    Distances are expressed either as a multiple of an ATR-style indicator
    (``sl_atr`` / ``tp_atr`` referencing ``atr_name``) or as fixed broker
    points (``sl_points`` / ``tp_points``).  Exactly one style per side.
    ``sl_min_points`` puts a floor under an ATR-derived stop.
    """
    sl_atr: Optional[float] = None
    tp_atr: Optional[float] = None
    sl_points: Optional[int] = None
    tp_points: Optional[int] = None
    sl_min_points: Optional[int] = None    # floor for an ATR-derived stop
    atr_name: str = "atr"
    max_hold_bars: int = 0                 # 0 = disabled
    exit_long: Optional[str] = None        # optional expression
    exit_short: Optional[str] = None

    def uses_atr(self) -> bool:
        return self.sl_atr is not None or self.tp_atr is not None


@dataclass
class Strategy:
    """Complete, self-contained strategy definition."""

    name: str
    symbol: str
    timeframe: str
    indicators: Dict[str, Indicator] = field(default_factory=dict)

    entry_long: Optional[str] = None
    entry_short: Optional[str] = None
    exits: Exits = field(default_factory=Exits)
    costs: Costs = field(default_factory=Costs)
    sizing: Sizing = field(default_factory=Sizing)
    trail: Trail = field(default_factory=Trail)

    # Broker clock, used by the utc_* expression fields.  EET/EEST (offset 2 +
    # EU summer time) covers FxPro and most MT5 brokers.
    broker_gmt_offset: int = 2
    broker_dst: str = DST_EU

    min_bars_between: int = 0
    lot: float = 0.01
    magic: int = 20260801

    date_from: str = "2024-06-01"
    date_to: str = "2026-08-01"

    deposit: float = 10000.0
    currency: str = "USD"
    leverage: int = 100

    # Spread assumed by the Python engine, in broker points.  ``None`` means
    # "ask the runner to measure it"; reconciliation can later replay the real
    # per-bar spread reported by MT5.
    spread_points: Optional[float] = None

    # Extra bars of history required before the first trade may be taken.
    extra_warmup: int = 0

    comment: str = ""

    # ---------------------------------------------------------------- checks
    def __post_init__(self):
        if self.timeframe not in TIMEFRAMES:
            raise SpecError("unknown timeframe %r (choose from %s)"
                            % (self.timeframe, sorted(TIMEFRAMES)))
        if not self.entry_long and not self.entry_short:
            raise SpecError("at least one of entry_long / entry_short is required")
        for nm in self.indicators:
            if nm in PRICE_FIELDS or nm in TIME_FIELDS:
                raise SpecError("indicator name %r collides with a built-in field" % nm)
            if not nm.isidentifier():
                raise SpecError("indicator name %r is not a valid identifier" % nm)
        if self.sizing.is_risk() and self.sizing.risk_money <= 0:
            raise SpecError("sizing mode 'risk' needs a positive risk_money")
        e = self.exits
        if e.sl_atr is not None and e.sl_points is not None:
            raise SpecError("give sl_atr OR sl_points, not both")
        if e.tp_atr is not None and e.tp_points is not None:
            raise SpecError("give tp_atr OR tp_points, not both")
        if e.uses_atr() and e.atr_name not in self.indicators:
            raise SpecError("exits reference indicator %r which is not defined"
                            % e.atr_name)
        if (e.sl_atr is None and e.sl_points is None
                and e.tp_atr is None and e.tp_points is None
                and not e.max_hold_bars and not e.exit_long and not e.exit_short):
            raise SpecError("strategy has no exit at all - add SL/TP, "
                            "max_hold_bars, or an exit expression")
        self.order = _topo_sort(self.indicators)
        self._compile_expressions()

    def htf_timeframes(self):
        """Higher timeframes this strategy needs, in ascending bar length."""
        out = {}
        for nm, ind in self.indicators.items():
            if isinstance(ind, HTF):
                out.setdefault(ind.timeframe, 0)
                out[ind.timeframe] = max(out[ind.timeframe], ind.inner_warmup())
        return out

    def _compile_expressions(self):
        known = set(self.indicators) | set(PRICE_FIELDS) | set(TIME_FIELDS)
        self.compiled = {}
        for label in ("entry_long", "entry_short"):
            src = getattr(self, label)
            self.compiled[label] = self._compile_one(label, src, known)
        for label in ("exit_long", "exit_short"):
            src = getattr(self.exits, label)
            self.compiled[label] = self._compile_one(label, src, known)

    def _compile_one(self, label, src, known):
        if not src:
            return None
        c = compile_expr(src)
        unknown = c.names - known
        if unknown:
            raise SpecError("%s references undefined name(s): %s"
                            % (label, ", ".join(sorted(unknown))))
        return c

    # ------------------------------------------------------------- warmup
    def warmup_bars(self) -> int:
        """Bars of history the strategy needs before its first decision."""
        need = 0
        cache = {}

        def depth(nm):
            if nm in PRICE_FIELDS or nm in TIME_FIELDS:
                return 0
            if nm in cache:
                return cache[nm]
            cache[nm] = 0                      # cycle guard (already rejected)
            ind = self.indicators[nm]
            d = ind.warmup() + max([depth(x) for x in ind.deps()] or [0])
            cache[nm] = d
            return d

        for nm in self.indicators:
            need = max(need, depth(nm))
        for c in self.compiled.values():
            if c is not None:
                need = max(need, c.max_shift + 1)
        # +2 covers the forming bar plus the previous-close term inside ATR
        return int(need + self.max_expr_shift() + self.extra_warmup + 2)

    def max_expr_shift(self) -> int:
        return max([c.max_shift for c in self.compiled.values() if c is not None]
                   or [0])

    # -------------------------------------------------------------- misc
    def directions(self) -> List[str]:
        out = []
        if self.entry_long:
            out.append(LONG)
        if self.entry_short:
            out.append(SHORT)
        return out

    def summary(self) -> str:
        lines = ["Strategy %s  [%s %s]" % (self.name, self.symbol, self.timeframe)]
        lines.append("  period      : %s .. %s" % (self.date_from, self.date_to))
        lines.append("  indicators  :")
        for nm in self.order:
            lines.append("      %-14s %s" % (nm, self.indicators[nm].describe()))
        if self.entry_long:
            lines.append("  entry_long  : %s" % self.entry_long)
        if self.entry_short:
            lines.append("  entry_short : %s" % self.entry_short)
        e = self.exits
        sl = ("%.4g x %s" % (e.sl_atr, e.atr_name)) if e.sl_atr is not None else (
            "%s pts" % e.sl_points if e.sl_points is not None else "-")
        tp = ("%.4g x %s" % (e.tp_atr, e.atr_name)) if e.tp_atr is not None else (
            "%s pts" % e.tp_points if e.tp_points is not None else "-")
        if e.sl_min_points:
            sl += " (floor %d pts)" % e.sl_min_points
        lines.append("  SL / TP     : %s  /  %s" % (sl, tp))
        if self.sizing.is_risk():
            lines.append("  sizing      : risk %.2f per trade, lot step %.2f, min %.2f"
                         % (self.sizing.risk_money, self.sizing.lot_step,
                            self.sizing.lot_min))
        if self.trail.active():
            lines.append("  trailing    : start %.2f, lock peak-%.2f"
                         % (self.trail.start_money, self.trail.step_money))
        htf = self.htf_timeframes()
        if htf:
            lines.append("  higher TF   : %s (last CLOSED bar only)"
                         % ", ".join("%s warmup %d" % (k, v) for k, v in htf.items()))
        if e.max_hold_bars:
            lines.append("  max hold    : %d bars" % e.max_hold_bars)
        if e.exit_long:
            lines.append("  exit_long   : %s" % e.exit_long)
        if e.exit_short:
            lines.append("  exit_short  : %s" % e.exit_short)
        if self.min_bars_between:
            lines.append("  re-entry gap: %d bars" % self.min_bars_between)
        c = self.costs
        if c.any():
            lines.append("  costs       : commission %.2f/lot RT, swap %.2f/%.2f per lot-night"
                         % (c.commission_per_lot, c.swap_long_per_lot_night,
                            c.swap_short_per_lot_night))
        else:
            lines.append("  costs       : NOT MODELLED (run `recon` to calibrate)")
        lines.append("  warmup      : %d bars" % self.warmup_bars())
        return "\n".join(lines)


def _topo_sort(indicators: Dict[str, Indicator]) -> List[str]:
    """Order indicators so every dependency is computed first."""
    order, temp, done = [], set(), set()

    def visit(nm, stack):
        if nm in PRICE_FIELDS or nm in TIME_FIELDS or nm in done:
            return
        if nm in temp:
            raise SpecError("circular indicator dependency: %s"
                            % " -> ".join(stack + [nm]))
        if nm not in indicators:
            raise SpecError("indicator %r references undefined name %r"
                            % (stack[-1] if stack else "?", nm))
        temp.add(nm)
        for dep in indicators[nm].deps():
            visit(dep, stack + [nm])
        temp.discard(nm)
        done.add(nm)
        order.append(nm)

    for nm in indicators:
        visit(nm, [])
    return order


def load_strategy(path) -> Strategy:
    """Import a .py file and return the ``STRATEGY`` object it defines."""
    import importlib.util
    import os

    path = os.path.abspath(str(path))
    spec = importlib.util.spec_from_file_location("_fx_strategy", path)
    if spec is None or spec.loader is None:
        raise SpecError("cannot import strategy file: %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obj = getattr(mod, "STRATEGY", None)
    if obj is None:
        for v in vars(mod).values():
            if isinstance(v, Strategy):
                obj = v
                break
    if not isinstance(obj, Strategy):
        raise SpecError("%s does not define a Strategy named STRATEGY" % path)
    return obj
