# -*- coding: utf-8 -*-
"""XAU Multi-Speed Volatility-Scaled Donchian Trend - v2 research framework.

The v1 runner (`bt_xau_msvsd.py`) was a single file that answered one question:
does the premise have a pulse? It did, barely. v2 exists because that answer
rested on assumptions the v1 code could not test - one flat swap rate applied
to 4.7 years, protective stops approximated from H4 bars, and a t-test over
overlapping sleeve trades.

Everything here is additive. With every v2 option left at its default the
engine reproduces the frozen v1 baseline bit for bit; see
`tests/golden/v1_baseline_stats.json` and `tests/test_golden_baseline.py`.
"""
from __future__ import annotations

__version__ = "2.0.0"

# Reason codes shared by the Pine debug export, the Python engine and the
# reconciler. These are a wire format: append, never renumber.
REASON_CODES = {
    0: "NONE",
    1: "ENTRY_LONG",
    2: "ENTRY_SHORT",
    3: "EXIT_CHANNEL",
    4: "EXIT_PROTECTIVE_STOP",
    5: "EXIT_SLEEVE_DISABLED",
    6: "EXIT_DIRECTION_MODE",      # v2: slow-confirmed-shorts withdrew confirmation
    7: "EXIT_END_OF_DATA",
    8: "EXIT_STOP_GAP",            # v2: LTF bar opened through the stop
}
REASON_BY_NAME = {v: k for k, v in REASON_CODES.items()}

SLEEVE_CODES = {0: "FLAT", 1: "LONG", -1: "SHORT"}
