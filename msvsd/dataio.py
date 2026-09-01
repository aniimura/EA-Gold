# -*- coding: utf-8 -*-
"""Data loading and validation.

Hard rule enforced here: this module never downloads, never interpolates and
never repairs. It loads what exists, tells you precisely what is wrong or
missing, and refuses to continue on anything that would silently corrupt a
result. Coverage gaps are reported as numbers, not smoothed away.

TIMEZONE MODEL
    MT5 stamps bars in BROKER SERVER time (FxPro runs EET/EEST). A CSV handed
    in by a user is assumed UTC unless told otherwise. Both are carried:
      time         as supplied, in the declared basis
      time_utc     used for calendar logic (the New York Friday cut-off)
      time_server  used for broker rollover boundaries (carry is charged at
                   00:00 server time, not 00:00 UTC)
    Getting this wrong shifts every session filter, so it is explicit.
"""
from __future__ import annotations

import os
import pickle
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.timeutil import eu_dst_active, server_to_utc

OHLC = ["open", "high", "low", "close"]
BROKER_WINTER_OFFSET_H = 2
BROKER_DST = "eu"


class DataError(RuntimeError):
    """Raised when the input cannot be trusted. Never downgraded to a warning."""


# --------------------------------------------------------------------------
@dataclass
class QualityReport:
    name: str
    rows: int = 0
    first: Optional[pd.Timestamp] = None
    last: Optional[pd.Timestamp] = None
    duplicates: int = 0
    non_monotonic: int = 0
    nan_ohlc: int = 0
    invalid_bars: int = 0
    nonpositive: int = 0
    notes: List[str] = field(default_factory=list)
    samples: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def fatal(self) -> int:
        return (self.duplicates + self.non_monotonic + self.nan_ohlc
                + self.invalid_bars + self.nonpositive)

    def raise_if_fatal(self) -> None:
        if self.fatal:
            raise DataError(
                "%s failed validation: %d duplicate, %d out-of-order, %d NaN-OHLC, "
                "%d invalid (high<low or close outside range), %d non-positive price.\n"
                "Examples: %s\n"
                "Fix the source data. This loader will not repair it."
                % (self.name, self.duplicates, self.non_monotonic, self.nan_ohlc,
                   self.invalid_bars, self.nonpositive, self.samples))

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "rows": self.rows,
            "first": str(self.first), "last": str(self.last),
            "duplicates": self.duplicates, "non_monotonic": self.non_monotonic,
            "nan_ohlc": self.nan_ohlc, "invalid_bars": self.invalid_bars,
            "nonpositive": self.nonpositive, "fatal": self.fatal,
            "notes": self.notes, "samples": self.samples,
        }


@dataclass
class CoverageReport:
    """How much of the H4 record the lower timeframe actually covers."""
    h4_bars: int = 0
    covered: int = 0
    uncovered: int = 0
    partial: int = 0
    ltf_bars: int = 0
    expected_per_h4: int = 0
    uncovered_samples: List[str] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.covered / self.h4_bars if self.h4_bars else 0.0

    def to_dict(self) -> Dict:
        d = self.__dict__.copy()
        d["coverage_pct"] = round(self.coverage_pct, 4)
        return d


# --------------------------------------------------------------------------
def _add_time_columns(df: pd.DataFrame, basis: str) -> pd.DataFrame:
    """Populate time_utc and time_server from the declared basis."""
    t = pd.to_datetime(df["time"])
    if getattr(t.dtype, "tz", None) is not None:
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
        basis = "utc"
    if basis == "server":
        df["time_server"] = t
        df["time_utc"] = pd.DatetimeIndex(
            server_to_utc(t, winter_offset_hours=BROKER_WINTER_OFFSET_H, dst=BROKER_DST))
    elif basis == "utc":
        df["time_utc"] = t
        off = BROKER_WINTER_OFFSET_H + eu_dst_active(t).astype(int)
        df["time_server"] = t + pd.to_timedelta(off, unit="h")
    else:
        raise DataError("time basis must be 'server' or 'utc', got %r" % basis)
    df["time"] = t
    return df


def _validate_bars(df: pd.DataFrame, name: str) -> QualityReport:
    r = QualityReport(name=name, rows=len(df))
    if not len(df):
        raise DataError("%s is empty" % name)

    missing = [c for c in ["time"] + OHLC if c not in df.columns]
    if missing:
        raise DataError("%s is missing required columns: %s" % (name, missing))

    t = df["time"]
    r.first, r.last = t.iloc[0], t.iloc[-1]

    dup = t.duplicated(keep=False)
    r.duplicates = int(dup.sum())
    if r.duplicates:
        r.samples["duplicates"] = [str(x) for x in t[dup].head(5)]

    order_bad = t.diff().dt.total_seconds().fillna(1.0) <= 0
    order_bad.iloc[0] = False
    r.non_monotonic = int(order_bad.sum())
    if r.non_monotonic:
        r.samples["non_monotonic"] = [str(x) for x in t[order_bad].head(5)]

    o, h, l, c = (df[k].to_numpy(float) for k in OHLC)
    nan_mask = ~np.isfinite(o) | ~np.isfinite(h) | ~np.isfinite(l) | ~np.isfinite(c)
    r.nan_ohlc = int(nan_mask.sum())
    if r.nan_ohlc:
        r.samples["nan_ohlc"] = [str(x) for x in t[nan_mask].head(5)]

    fin = ~nan_mask
    invalid = np.zeros(len(df), bool)
    invalid[fin] = ((h[fin] < l[fin])
                    | (h[fin] < np.maximum(o[fin], c[fin]) - 1e-9)
                    | (l[fin] > np.minimum(o[fin], c[fin]) + 1e-9))
    r.invalid_bars = int(invalid.sum())
    if r.invalid_bars:
        r.samples["invalid_bars"] = [str(x) for x in t[invalid].head(5)]

    nonpos = np.zeros(len(df), bool)
    nonpos[fin] = (o[fin] <= 0) | (h[fin] <= 0) | (l[fin] <= 0) | (c[fin] <= 0)
    r.nonpositive = int(nonpos.sum())
    if r.nonpositive:
        r.samples["nonpositive"] = [str(x) for x in t[nonpos].head(5)]

    return r


def _read_any(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pkl":
        with open(path, "rb") as fh:
            return pickle.load(fh)
    if ext in (".csv", ".txt"):
        return pd.read_csv(path)
    if ext == ".parquet":
        return pd.read_parquet(path)
    raise DataError("unsupported bar file type %r (use .csv, .pkl or .parquet)" % ext)


def load_bars(path: str, name: str, basis: str = "server",
              default_spread_points: float = 15.0) -> (pd.DataFrame, QualityReport):
    """Load and validate an OHLC file. Never repairs; raises on fatal problems."""
    if not os.path.isfile(path):
        raise DataError(
            "%s not found: %s\nSupply the file. This tool does not download "
            "market data." % (name, path))
    df = _read_any(path).copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "time" not in df.columns:
        for alt in ("datetime", "timestamp", "date"):
            if alt in df.columns:
                df = df.rename(columns={alt: "time"})
                break
    df = _add_time_columns(df, basis)
    if "spread" not in df.columns:
        df["spread"] = float(default_spread_points)
        name_note = "spread column absent - flat %.1f points assumed" % default_spread_points
    else:
        name_note = None
    rep = _validate_bars(df, name)
    if name_note:
        rep.notes.append(name_note)
    rep.raise_if_fatal()
    keep = ["time", "time_utc", "time_server"] + OHLC + ["spread"]
    return df[keep].reset_index(drop=True), rep


def load_repo_h4(symbol: str, timeframe: str, date_from: str, date_to: str,
                 warmup_bars: int) -> (pd.DataFrame, QualityReport):
    """The repo's own MT5 cache, so the default run needs no arguments."""
    from core import config as core_config, data as datamod
    df = datamod.load_rates(symbol, timeframe, date_from, date_to,
                            warmup_bars=warmup_bars, refresh=False,
                            terminal_path=core_config.MT5_EXE, verbose=False)
    df = _add_time_columns(df.copy(), "server")
    rep = _validate_bars(df, "%s %s (repo cache)" % (symbol, timeframe))
    rep.raise_if_fatal()
    keep = ["time", "time_utc", "time_server"] + OHLC + ["spread"]
    return df[keep].reset_index(drop=True), rep


def load_repo_ltf(symbol: str, timeframe: str, date_from: str, date_to: str
                  ) -> (pd.DataFrame, QualityReport):
    from core import config as core_config, data as datamod
    df = datamod.load_rates(symbol, timeframe, date_from, date_to,
                            warmup_bars=204, refresh=False,
                            terminal_path=core_config.MT5_EXE, verbose=False)
    df = _add_time_columns(df.copy(), "server")
    rep = _validate_bars(df, "%s %s (repo cache)" % (symbol, timeframe))
    rep.raise_if_fatal()
    keep = ["time", "time_utc", "time_server"] + OHLC + ["spread"]
    return df[keep].reset_index(drop=True), rep


# --------------------------------------------------------------------------
def index_ltf(h4: pd.DataFrame, ltf: pd.DataFrame) -> (np.ndarray, np.ndarray, CoverageReport):
    """Map each H4 bar to its slice of lower-timeframe bars.

    Returns (start_idx, end_idx) arrays such that ltf[start[i]:end[i]] are the
    LTF bars belonging to H4 bar i, plus a coverage report. An H4 bar with no
    LTF bars gets start == end; the caller must fall back to the H4
    approximation for that bar and the run is labelled accordingly.
    """
    ht = h4["time"].to_numpy("datetime64[ns]")
    lt = ltf["time"].to_numpy("datetime64[ns]")
    n = len(ht)
    # bar i spans [t_i, t_{i+1}); the last bar uses the H4 nominal width
    if n >= 2:
        width = np.median(np.diff(ht).astype("timedelta64[s]").astype(float))
    else:
        width = 4 * 3600.0
    edges = np.concatenate([ht, [ht[-1] + np.timedelta64(int(width), "s")]])
    start = np.searchsorted(lt, edges[:-1], side="left")
    end = np.searchsorted(lt, edges[1:], side="left")

    counts = end - start
    ltf_width = (np.median(np.diff(lt).astype("timedelta64[s]").astype(float))
                 if len(lt) >= 2 else 60.0)
    expected = max(1, int(round(width / ltf_width)))
    cov = CoverageReport(
        h4_bars=n, ltf_bars=len(lt), expected_per_h4=expected,
        covered=int((counts > 0).sum()), uncovered=int((counts == 0).sum()),
        partial=int(((counts > 0) & (counts < expected * 0.5)).sum()))
    if cov.uncovered:
        bad = np.where(counts == 0)[0][:8]
        cov.uncovered_samples = [str(pd.Timestamp(ht[i])) for i in bad]
    return start, end, cov


# --------------------------------------------------------------------------
SWAP_COLUMNS = ["date", "long_swap_usd_per_lot", "short_swap_usd_per_lot"]


def load_swap_table(path: str) -> pd.DataFrame:
    """Historical financing: one row per rollover date, cash per lot.

    Because each row is the actual cash charged at that rollover, triple-swap
    nights are encoded in the file itself. Nothing here multiplies by three.
    """
    if not os.path.isfile(path):
        raise DataError("swap file not found: %s" % path)
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in SWAP_COLUMNS if c not in df.columns]
    if missing:
        raise DataError("swap file %s is missing columns %s; expected %s"
                        % (path, missing, SWAP_COLUMNS))
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    if df["date"].duplicated().any():
        dups = df.loc[df["date"].duplicated(keep=False), "date"].head(5).tolist()
        raise DataError("swap file has duplicate dates, e.g. %s" % dups)
    for c in SWAP_COLUMNS[1:]:
        v = pd.to_numeric(df[c], errors="coerce")
        if v.isna().any():
            raise DataError("swap file column %r has non-numeric or missing values "
                            "at %s" % (c, df.loc[v.isna(), "date"].head(5).tolist()))
        df[c] = v.astype(float)
    return df.sort_values("date").reset_index(drop=True)


EVENT_COLUMNS = ["timestamp", "event_name", "blackout_before_minutes",
                 "blackout_after_minutes"]


def load_events(path: str) -> pd.DataFrame:
    """Scheduled-event windows. Supplied by the user; never invented here."""
    if not os.path.isfile(path):
        raise DataError("events file not found: %s" % path)
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in EVENT_COLUMNS if c not in df.columns]
    if missing:
        raise DataError("events file %s is missing columns %s; expected %s"
                        % (path, missing, EVENT_COLUMNS))
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    df["timestamp"] = ts
    for c in ("blackout_before_minutes", "blackout_after_minutes"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
        if df[c].isna().any():
            raise DataError("events file column %r has non-numeric values" % c)
    df["start_utc"] = df["timestamp"] - pd.to_timedelta(df["blackout_before_minutes"], unit="m")
    df["end_utc"] = df["timestamp"] + pd.to_timedelta(df["blackout_after_minutes"], unit="m")
    return df.sort_values("timestamp").reset_index(drop=True)
