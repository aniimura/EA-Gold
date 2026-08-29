# -*- coding: utf-8 -*-
"""Environment paths.

Override any of these with environment variables (FXT_MT5_EXE, FXT_METAEDITOR,
FXT_MT5_DATA, FXT_PYTHON) or by editing ``config.local.py`` next to this file.
"""
from __future__ import annotations

import glob
import os

# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(ROOT, "build")
RESULTS_DIR = os.path.join(ROOT, "results")
DATA_DIR = os.path.join(ROOT, "data")
STRATEGY_DIR = os.path.join(ROOT, "strategies")

_APPDATA = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
_MQ_ROOT = os.path.join(_APPDATA, "MetaQuotes", "Terminal")

MT5_EXE = os.environ.get(
    "FXT_MT5_EXE", r"C:\Program Files\FxPro - MetaTrader 5\terminal64.exe")
METAEDITOR = os.environ.get(
    "FXT_METAEDITOR", r"C:\Program Files\FxPro - MetaTrader 5\metaeditor64.exe")
PYTHON = os.environ.get(
    "FXT_PYTHON", r"C:\Users\Aruta\miniforge3\envs\py39env\python.exe")


def _autodetect_data_dir():
    """Pick the terminal data folder that actually has an MQL5 tree."""
    env = os.environ.get("FXT_MT5_DATA")
    if env and os.path.isdir(env):
        return env
    best, best_mtime = None, -1.0
    for d in glob.glob(os.path.join(_MQ_ROOT, "*")):
        if not os.path.isdir(d) or os.path.basename(d) in ("Common", "Community", "Help"):
            continue
        if not os.path.isdir(os.path.join(d, "MQL5", "Experts")):
            continue
        m = os.path.getmtime(d)
        if m > best_mtime:
            best, best_mtime = d, m
    return best


MT5_DATA = _autodetect_data_dir() or os.path.join(
    _MQ_ROOT, "0148BD5691B65B0F2157627A4231F3DE")

EXPERTS_DIR = os.path.join(MT5_DATA, "MQL5", "Experts")
TESTER_INI = os.path.join(MT5_DATA, "tester.ini")
TESTER_LOG_DIR = os.path.join(MT5_DATA, "Tester", "logs")
TESTER_CACHE_DIR = os.path.join(MT5_DATA, "Tester", "cache")
TESTER_PROFILE_DIR = os.path.join(MT5_DATA, "MQL5", "Profiles", "Tester")
COMMON_FILES = os.path.join(_MQ_ROOT, "Common", "Files")

# optional local overrides
try:                                             # pragma: no cover
    from .config_local import *                  # noqa: F401,F403
except ImportError:
    pass


def describe():
    rows = [
        ("MT5 terminal", MT5_EXE, os.path.isfile(MT5_EXE)),
        ("MetaEditor", METAEDITOR, os.path.isfile(METAEDITOR)),
        ("Python", PYTHON, os.path.isfile(PYTHON)),
        ("MT5 data dir", MT5_DATA, os.path.isdir(MT5_DATA)),
        ("Experts dir", EXPERTS_DIR, os.path.isdir(EXPERTS_DIR)),
        ("Common\\Files", COMMON_FILES, os.path.isdir(COMMON_FILES)),
    ]
    out = []
    for label, path, ok in rows:
        out.append("  %-14s %-6s %s" % (label, "OK" if ok else "MISS", path))
    return "\n".join(out)
