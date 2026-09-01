# -*- coding: utf-8 -*-
"""Run configuration: the frozen baseline, plus every optional v2 switch.

BASELINE_* below is the specification. It is not tunable state - the
experiment runner builds *new* RunConfig objects, it never mutates these.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# The frozen specification. Changing any value here changes the baseline and
# invalidates tests/golden/. That is the point of keeping them in one block.
# --------------------------------------------------------------------------
BASELINE_SLEEVES: List[Tuple[str, int, int]] = [
    ("fast", 20, 10),
    ("medium", 55, 20),
    ("slow", 120, 40),
]
BASELINE_RISK_PCT = 0.10
BASELINE_ATR_LEN = 20
BASELINE_ATR_MULT = 2.5
BASELINE_CONTRACT_OZ = 100.0
BASELINE_LOT_STEP = 0.01
BASELINE_MAX_NOTIONAL_X = 1.5
BASELINE_CAPITAL = 100000.0
BASELINE_FRIDAY_HOUR = 13                 # America/New_York
BASELINE_POINT = 0.01
BASELINE_COMMISSION_PER_LOT_RT = 7.85     # USD, round turn
BASELINE_SLIPPAGE_POINTS = 5.0            # per side, on top of spread
BASELINE_SWAP_LONG = -52.40               # USD per lot per night
BASELINE_SWAP_SHORT = 23.58
BASELINE_TRIPLE_WEEKDAY = 2               # Wednesday
BASELINE_SYMBOL = "GOLD"
BASELINE_TIMEFRAME = "H4"
BASELINE_DATE_FROM = "2022-01-01"
BASELINE_DATE_TO = "2026-08-31"
BASELINE_WARMUP_BARS = 364

SLEEVE_MODES = ("all", "medium-slow", "slow-only")
DIRECTION_MODES = ("symmetric", "long-only", "slow-confirmed-shorts")
SWAP_MODELS = ("flat", "historical", "scenario", "none")
SWAP_MISSING_POLICIES = ("error", "zero", "forward-fill", "scenario-rate")
SWAP_SCENARIOS = ("low", "base", "high")
STOP_MODES = ("h4", "ltf")
EVENT_MODES = ("report-only", "block-new-entries")
FRIDAY_BASES = ("open", "close")

# Scenario carry multipliers applied to the baseline flat rates. These are
# assumptions for stress testing, not observed rates; any run using them is
# labelled SCENARIO_CARRY in the report.
SWAP_SCENARIO_MULT = {"low": 0.5, "base": 1.0, "high": 1.75}

SLEEVE_MODE_MEMBERS = {
    "all": ("fast", "medium", "slow"),
    "medium-slow": ("medium", "slow"),
    "slow-only": ("slow",),
}


@dataclass
class RunConfig:
    """Everything one simulation needs. Serialised verbatim into every result."""

    # ---- data ------------------------------------------------------------
    h4_file: Optional[str] = None          # None -> repo GOLD_H4 cache
    ltf_file: Optional[str] = None         # M1/M5 for stop replay
    swap_file: Optional[str] = None
    events_file: Optional[str] = None
    date_from: str = BASELINE_DATE_FROM
    date_to: str = BASELINE_DATE_TO
    symbol: str = BASELINE_SYMBOL
    timeframe: str = BASELINE_TIMEFRAME

    # ---- account ---------------------------------------------------------
    capital: float = BASELINE_CAPITAL
    risk_pct: float = BASELINE_RISK_PCT
    contract_oz: float = BASELINE_CONTRACT_OZ
    lot_step: float = BASELINE_LOT_STEP
    max_notional_x: float = BASELINE_MAX_NOTIONAL_X

    # ---- strategy --------------------------------------------------------
    sleeve_mode: str = "all"
    direction_mode: str = "symmetric"
    atr_len: int = BASELINE_ATR_LEN
    atr_mult: float = BASELINE_ATR_MULT
    exit_scale: float = 1.0                # multiplies the 10/20/40 exit windows
    allow_reversal: bool = True
    friday_filter: bool = True
    friday_hour: int = BASELINE_FRIDAY_HOUR
    friday_basis: str = "open"             # v1 used bar OPEN; Pine uses close
    lookahead_audit: bool = False
    same_bar_fill_audit: bool = False

    # ---- costs -----------------------------------------------------------
    point: float = BASELINE_POINT
    commission_per_lot_rt: float = BASELINE_COMMISSION_PER_LOT_RT
    slippage_points: float = BASELINE_SLIPPAGE_POINTS
    cost_scale: float = 1.0                # scales spread AND slippage (1x/2x/3x)
    use_costs: bool = True
    swap_model: str = "flat"
    swap_scenario: str = "base"
    swap_missing_policy: str = "error"
    swap_long_flat: float = BASELINE_SWAP_LONG
    swap_short_flat: float = BASELINE_SWAP_SHORT
    triple_weekday: int = BASELINE_TRIPLE_WEEKDAY

    # ---- execution -------------------------------------------------------
    stop_mode: str = "h4"
    ltf_stop_slippage_points: float = BASELINE_SLIPPAGE_POINTS
    # --- v1 compatibility quirks. Defaults reproduce the frozen baseline; the
    # alternative value in each case is the corrected behaviour. See CHANGELOG.
    close_final_position_with_costs: bool = False   # v1 closed it cost-free
    financing_timing: str = "post-fill"             # v1 charged after the open fill
    log_open_sleeves_at_end: bool = False           # v1 dropped still-open sleeves
    v1_compat: bool = False                         # reproduce two v1 DEFECTS exactly

    # ---- events ----------------------------------------------------------
    event_mode: str = "report-only"

    # ---- statistics ------------------------------------------------------
    seed: int = 20260901
    bootstrap_n: int = 20000
    n_configs_tested: int = 1              # feeds the Deflated Sharpe Ratio

    # ---- output ----------------------------------------------------------
    tag: str = "baseline"
    outdir: str = os.path.join("results", "v2")

    # ---- provenance ------------------------------------------------------
    label_flags: List[str] = field(default_factory=list)

    # ----------------------------------------------------------------------
    def sleeve_defs(self) -> List[Tuple[str, int, int]]:
        """Active sleeves with the exit-window scaling applied.

        Entry windows are never scaled - the exit-scaling experiment is
        explicitly about exits only.
        """
        members = SLEEVE_MODE_MEMBERS[self.sleeve_mode]
        out = []
        for name, ent, ex in BASELINE_SLEEVES:
            if name not in members:
                continue
            scaled = max(1, int(round(ex * self.exit_scale)))
            out.append((name, ent, scaled))
        return out

    def validate(self) -> None:
        bad = []
        if self.sleeve_mode not in SLEEVE_MODES:
            bad.append("sleeve_mode=%r not in %s" % (self.sleeve_mode, SLEEVE_MODES))
        if self.direction_mode not in DIRECTION_MODES:
            bad.append("direction_mode=%r not in %s" % (self.direction_mode, DIRECTION_MODES))
        if self.swap_model not in SWAP_MODELS:
            bad.append("swap_model=%r not in %s" % (self.swap_model, SWAP_MODELS))
        if self.swap_missing_policy not in SWAP_MISSING_POLICIES:
            bad.append("swap_missing_policy=%r not in %s"
                       % (self.swap_missing_policy, SWAP_MISSING_POLICIES))
        if self.swap_scenario not in SWAP_SCENARIOS:
            bad.append("swap_scenario=%r not in %s" % (self.swap_scenario, SWAP_SCENARIOS))
        if self.stop_mode not in STOP_MODES:
            bad.append("stop_mode=%r not in %s" % (self.stop_mode, STOP_MODES))
        if self.event_mode not in EVENT_MODES:
            bad.append("event_mode=%r not in %s" % (self.event_mode, EVENT_MODES))
        if self.friday_basis not in FRIDAY_BASES:
            bad.append("friday_basis=%r not in %s" % (self.friday_basis, FRIDAY_BASES))
        if self.financing_timing not in ("post-fill", "pre-fill"):
            bad.append("financing_timing=%r not in ('post-fill','pre-fill')"
                       % self.financing_timing)
        if self.swap_model == "historical" and not self.swap_file:
            bad.append("swap_model=historical requires --swap-file")
        if self.risk_pct < 0:
            bad.append("risk_pct must be >= 0")
        if self.lot_step <= 0:
            bad.append("lot_step must be > 0")
        if self.atr_mult <= 0:
            bad.append("atr_mult must be > 0")
        if self.exit_scale <= 0:
            bad.append("exit_scale must be > 0")
        if self.capital <= 0:
            bad.append("capital must be > 0")
        if self.bootstrap_n < 100:
            bad.append("bootstrap_n must be >= 100")
        if bad:
            raise ValueError("invalid configuration:\n  - " + "\n  - ".join(bad))

    def labels(self) -> List[str]:
        """Prominent warning labels that must appear on every report built
        from this run. These are the claims a reader would otherwise have to
        reconstruct from the flags."""
        out = list(self.label_flags)
        if self.stop_mode == "h4":
            out.append("APPROXIMATE_INTRABAR_STOPS")
        if self.swap_model == "none" or not self.use_costs:
            out.append("NO_CARRY_MODELLED__NOT_DEPLOYABLE")
        if self.swap_model == "flat":
            out.append("FLAT_CARRY_ASSUMPTION")
        if self.swap_model == "scenario":
            out.append("SCENARIO_CARRY__%s" % self.swap_scenario.upper())
        if self.swap_model == "historical" and self.swap_missing_policy != "error":
            out.append("CARRY_GAPS_FILLED__%s" % self.swap_missing_policy.upper().replace("-", "_"))
        if not self.use_costs:
            out.append("NO_TRANSACTION_COSTS")
        if self.lookahead_audit:
            out.append("AUDIT_LOOKAHEAD__NOT_A_RESULT")
        if self.same_bar_fill_audit:
            out.append("AUDIT_SAME_BAR_FILL__NOT_A_RESULT")
        if self.friday_basis == "open":
            out.append("FRIDAY_FILTER_ON_BAR_OPEN__DIVERGES_FROM_PINE")
        if self.financing_timing == "post-fill":
            out.append("CARRY_CHARGED_AFTER_OPEN_FILL__V1_COMPAT")
        if self.v1_compat:
            out.append("V1_COMPAT__REPRODUCES_KNOWN_DEFECTS")
        if self.n_configs_tested > 1:
            out.append("MULTIPLE_TESTING__%d_CONFIGS" % self.n_configs_tested)
        return out

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["labels"] = self.labels()
        d["sleeve_defs"] = self.sleeve_defs()
        return d

    def fingerprint(self) -> str:
        """Stable hash of the configuration, for run provenance."""
        d = asdict(self)
        d.pop("tag", None)
        d.pop("outdir", None)
        return hashlib.sha1(
            json.dumps(d, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]

    def replace(self, **kw) -> "RunConfig":
        return dataclasses.replace(self, **kw)


def code_version() -> str:
    """Hash of the msvsd package source, recorded with every run so a result
    can be tied to the code that produced it."""
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha1()
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            with open(os.path.join(here, name), "rb") as fh:
                h.update(name.encode("utf-8"))
                h.update(fh.read())
    return h.hexdigest()[:12]
