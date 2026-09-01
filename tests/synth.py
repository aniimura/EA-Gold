# -*- coding: utf-8 -*-
"""Small hand-built datasets whose expected results can be worked out on paper.

Every price path here is deliberate. If a test fails, the fixture is short
enough to read and the expected answer short enough to recompute by hand,
which is the only kind of test worth having for an execution engine.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvsd.dataio import _add_time_columns  # noqa: E402

H4 = pd.Timedelta(hours=4)


def make_h4(rows, start="2024-01-01 00:00:00", spread_points=15.0,
            basis="server") -> pd.DataFrame:
    """rows: iterable of (open, high, low, close). One H4 bar each, contiguous.

    2024-01-01 is a Monday, so bar index 30 lands on Friday - handy for the
    weekly-close filter tests.
    """
    rows = list(rows)
    t0 = pd.Timestamp(start)
    df = pd.DataFrame({
        "time": [t0 + i * H4 for i in range(len(rows))],
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "spread": [spread_points] * len(rows),
    })
    return _add_time_columns(df, basis).reset_index(drop=True)


def flat_then(path, level=2000.0, n_warm=200, band=1.0, **kw) -> pd.DataFrame:
    """`n_warm` quiet bars in a tight band, then the supplied (o,h,l,c) rows.

    The quiet prefix fills every Donchian window and seeds ATR at a known, tiny
    value, so the interesting bars are the only thing driving behaviour.
    """
    warm = [(level, level + band, level - band, level) for _ in range(n_warm)]
    return make_h4(warm + list(path), **kw)


def constant_ltf(h4: pd.DataFrame, sub=4) -> pd.DataFrame:
    """Lower-timeframe bars that simply retrace their parent H4 bar.

    Each H4 bar is split into `sub` equal slices: open -> high -> low -> close,
    so the parent's extremes are reachable but the path is fully determined.
    """
    out = []
    step = H4 / sub
    for r in h4.itertuples():
        pts = [r.open, r.high, r.low, r.close][:sub]
        for k in range(sub):
            px = pts[k] if k < len(pts) else r.close
            nxt = pts[k + 1] if k + 1 < len(pts) else r.close
            out.append({"time": r.time + k * step,
                        "open": px, "high": max(px, nxt), "low": min(px, nxt),
                        "close": nxt, "spread": r.spread})
    df = pd.DataFrame(out)
    return _add_time_columns(df, "server").reset_index(drop=True)


def ltf_from_paths(h4: pd.DataFrame, paths) -> pd.DataFrame:
    """Explicit lower-timeframe path per H4 bar.

    `paths` maps an H4 bar index to a list of (open, high, low, close) tuples.
    Bars not listed get a single LTF bar equal to the H4 bar, so a test only
    has to spell out the bar it cares about.
    """
    out = []
    for i, r in enumerate(h4.itertuples()):
        sub = paths.get(i)
        if sub is None:
            out.append({"time": r.time, "open": r.open, "high": r.high,
                        "low": r.low, "close": r.close, "spread": r.spread})
            continue
        step = H4 / max(len(sub), 1)
        for k, (o, h, l, c) in enumerate(sub):
            out.append({"time": r.time + k * step, "open": o, "high": h,
                        "low": l, "close": c, "spread": r.spread})
    df = pd.DataFrame(out)
    return _add_time_columns(df, "server").reset_index(drop=True)


def swap_table(dates, long_rate, short_rate) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(list(dates)).normalize(),
        "long_swap_usd_per_lot": [long_rate] * len(list(dates)),
        "short_swap_usd_per_lot": [short_rate] * len(list(dates)),
    })


def breakout_up(n_warm=200, level=2000.0, jump=60.0, hold=30):
    """Quiet, then a decisive upside breakout held for `hold` bars.

    The jump clears the 120-bar entry high by a wide margin, so all three
    sleeves fire on the same bar.
    """
    path = [(level, level + jump, level, level + jump)]
    for k in range(hold):
        p = level + jump + k * 2.0
        path.append((p, p + 2.0, p - 0.5, p + 2.0))
    return flat_then(path, level=level, n_warm=n_warm)
