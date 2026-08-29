# -*- coding: utf-8 -*-
"""Compile the generated EA and drive the MT5 Strategy Tester.

Deliberately CSV-first: the EA writes its own trade log into Common\\Files, so
reconciliation never depends on scraping the tester's text log.  The HTML
report is still parsed, but only for the headline figures a human wants to see.
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import re
import shutil
import subprocess
import time

import numpy as np
import pandas as pd

from codegen.mq5gen import bars_csv_name, trades_csv_name
from core import config
from core.spec import Strategy


class RunnerError(RuntimeError):
    pass


# --------------------------------------------------------------------------
def kill_mt5(wait=3.0):
    subprocess.run(["taskkill", "/f", "/im", "terminal64.exe"],
                   capture_output=True, timeout=20)
    time.sleep(wait)


def clear_tester_cache():
    d = config.TESTER_CACHE_DIR
    if not os.path.isdir(d):
        return
    for f in glob.glob(os.path.join(d, "*")):
        try:
            os.unlink(f)
        except OSError:
            pass


# --------------------------------------------------------------------------
def compile_ea(mq5_path, verbose=True):
    """Copy the generated .mq5 into MQL5\\Experts and compile it."""
    if not os.path.isfile(mq5_path):
        raise RunnerError("generated EA not found: %s" % mq5_path)
    if not os.path.isfile(config.METAEDITOR):
        raise RunnerError("MetaEditor not found: %s" % config.METAEDITOR)

    name = os.path.splitext(os.path.basename(mq5_path))[0]
    dst = os.path.join(config.EXPERTS_DIR, name + ".mq5")
    os.makedirs(config.EXPERTS_DIR, exist_ok=True)
    shutil.copy2(mq5_path, dst)

    ex5 = os.path.join(config.EXPERTS_DIR, name + ".ex5")
    log = os.path.join(config.EXPERTS_DIR, name + ".log")
    for p in (ex5, log):
        try:
            os.unlink(p)
        except OSError:
            pass

    subprocess.run(
        [config.METAEDITOR, "/compile:%s" % dst,
         "/include:%s" % os.path.join(config.MT5_DATA, "MQL5"), "/log"],
        capture_output=True, text=True, timeout=180)
    time.sleep(2)

    log_text = ""
    if os.path.isfile(log):
        raw = open(log, "rb").read()
        log_text = raw.decode("utf-16-le" if b"\x00" in raw[:80] else "utf-8",
                              errors="replace")

    if not os.path.isfile(ex5):
        raise RunnerError("compilation failed for %s\n%s" % (name, log_text[:4000]))

    warns = [l for l in log_text.splitlines() if " warning " in l.lower()]
    if verbose:
        print("  compile: %s (%.1f KB)%s"
              % (os.path.basename(ex5), os.path.getsize(ex5) / 1024.0,
                 "  [%d warnings]" % len(warns) if warns else ""))
    return ex5, log_text


# --------------------------------------------------------------------------
_TESTER_INI = """[Tester]
Expert={expert}
Symbol={symbol}
Period={period}
Model={model}
Optimization=0
FromDate={dfrom}
ToDate={dto}
ForwardMode=0
Deposit={deposit}
Currency={currency}
ProfitInPips=0
Leverage={leverage}
ExecutionMode=0
Visual=0
ShutdownTerminal=1
ReplaceReport=1
Report={report}
"""


def _clean_outputs(strategy: Strategy, report_name):
    for p in (os.path.join(config.MT5_DATA, report_name + ".htm"),
              os.path.join(config.MT5_DATA, "Tester", report_name + ".htm"),
              os.path.join(config.COMMON_FILES, trades_csv_name(strategy)),
              os.path.join(config.COMMON_FILES, bars_csv_name(strategy))):
        try:
            os.unlink(p)
        except OSError:
            pass
    if os.path.isdir(config.TESTER_PROFILE_DIR):
        for f in glob.glob(os.path.join(config.TESTER_PROFILE_DIR, strategy.name + "*")):
            try:
                os.unlink(f)
            except OSError:
                pass


def _find_report(report_name, started_at):
    cands = [os.path.join(config.MT5_DATA, report_name + ".htm"),
             os.path.join(config.MT5_DATA, "Tester", report_name + ".htm")]
    for c in cands:
        if os.path.isfile(c) and os.path.getsize(c) > 100:
            return c
    for c in glob.glob(os.path.join(config.MT5_DATA, "*.htm")):
        if os.path.getmtime(c) >= started_at and report_name.lower() in os.path.basename(c).lower():
            return c
    return None


def _tester_finished(started_at):
    log = os.path.join(config.TESTER_LOG_DIR,
                       dt.date.today().strftime("%Y%m%d") + ".log")
    if not os.path.isfile(log):
        return False
    try:
        raw = open(log, "rb").read()
    except OSError:
        return False
    text = raw.decode("utf-16-le" if b"\x00" in raw[:80] else "utf-8", errors="replace")
    if "final balance" not in text and "Test passed" not in text:
        return False
    for line in reversed(text.splitlines()):
        if "final balance" in line or "Test passed" in line:
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", line)
            if not m:
                return False
            now = dt.datetime.now()
            stamp = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                second=int(m.group(3)), microsecond=0)
            return stamp.timestamp() >= started_at - 5
    return False


def run_tester(strategy: Strategy, model=1, timeout=3600, verbose=True,
               write_bars=False):
    """Run one Strategy Tester pass.  ``model``: 0=ticks, 1=1-min OHLC, 2=open."""
    report_name = "%s_%s_%s" % (strategy.name, strategy.symbol, strategy.timeframe)
    _clean_outputs(strategy, report_name)

    ini = _TESTER_INI.format(
        expert=strategy.name,
        symbol=strategy.symbol,
        period=strategy.timeframe,
        model=int(model),
        dfrom=pd.Timestamp(strategy.date_from).strftime("%Y.%m.%d"),
        dto=pd.Timestamp(strategy.date_to).strftime("%Y.%m.%d"),
        deposit=int(strategy.deposit),
        currency=strategy.currency,
        leverage=int(strategy.leverage),
        report=report_name,
    )
    if write_bars:
        ini += "\n[TesterInputs]\nInpWriteBars=true\n"
    with open(config.TESTER_INI, "w", encoding="utf-8") as fh:
        fh.write(ini)

    clear_tester_cache()
    kill_mt5()

    started = time.time()
    if verbose:
        print("  tester : %s %s %s  %s .. %s  (model=%d)"
              % (strategy.name, strategy.symbol, strategy.timeframe,
                 strategy.date_from, strategy.date_to, model))
    subprocess.Popen([config.MT5_EXE, "/config:%s" % config.TESTER_INI],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    report_path = None
    for i in range(int(timeout)):
        time.sleep(1)
        report_path = _find_report(report_name, started)
        if report_path:
            break
        if i >= 10 and i % 5 == 0 and _tester_finished(started):
            for _ in range(30):
                time.sleep(2)
                report_path = _find_report(report_name, started)
                if report_path:
                    break
            break
        if verbose and i >= 30 and i % 30 == 0:
            print("           ... %ds" % int(time.time() - started))

    elapsed = time.time() - started
    stats = {}
    if report_path:
        stats = parse_report(report_path)
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        shutil.copy2(report_path, os.path.join(config.RESULTS_DIR, report_name + ".htm"))
    else:
        stats = {"error": "no report produced"}

    kill_mt5(wait=2.0)
    stats["elapsed_s"] = round(elapsed, 1)
    if verbose:
        print("  tester : finished in %.0fs" % elapsed)
    return stats


# --------------------------------------------------------------------------
def _decode_report(path):
    """MT5 writes the report in UTF-16, UTF-8 or the OS codepage depending on
    build and localisation - try them in turn rather than guessing."""
    raw = open(path, "rb").read()
    for enc in ("utf-16-le", "utf-8", "cp932", "cp1252"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "<" in text[:4000]:
            return text
    return raw.decode("utf-8", errors="replace")


def _num(s):
    s = str(s).replace(" ", "").replace("\xa0", "").replace(",", "")
    return float(s)


# label -> (result key, parser).  English and Japanese live in one table so a
# localised terminal does not silently produce an empty report.
_LABELS = [
    (("Total Net Profit", "総損益"), "total_net_profit", _num),
    (("Gross Profit", "総利益"), "gross_profit", _num),
    (("Gross Loss", "総損失"), "gross_loss", _num),
    (("Profit Factor", "プロフィットファクター"), "profit_factor", _num),
    (("Expected Payoff", "期待利得"), "expected_payoff", _num),
    (("Recovery Factor", "リカバリファクター"), "recovery_factor", _num),
    (("Sharpe Ratio", "シャープレシオ"), "sharpe_ratio", _num),
    (("Total Trades", "取引数"), "total_trades", lambda s: int(_num(s))),
    (("Total Deals", "約定数"), "total_deals", lambda s: int(_num(s))),
    (("Bars", "バー"), "bars", lambda s: int(_num(s))),
]

_PCT_LABELS = [
    (("Balance Drawdown Maximal", "残高最大ドローダウン"), "max_dd", "max_dd_pct"),
    (("Equity Drawdown Maximal", "証拠金最大ドローダウン"), "max_eq_dd", "max_eq_dd_pct"),
]

_WIN_LABELS = (("Profit Trades", "勝ちトレード"),)


def parse_report(path):
    """Pull the headline numbers out of the tester's HTML report."""
    text = _decode_report(path)
    cells = [re.sub(r"<[^>]+>", "", c).strip().replace("\xa0", " ")
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", text, re.S)]
    cells = [c for c in cells if c]

    r = {}
    for i, label in enumerate(cells[:-1]):
        value = cells[i + 1]
        for names, key, conv in _LABELS:
            if key in r:
                continue
            if any(label.startswith(n) for n in names):
                try:
                    r[key] = conv(value)
                except ValueError:
                    pass
        for names, k_abs, k_pct in _PCT_LABELS:
            if k_abs in r:
                continue
            if any(label.startswith(n) for n in names):
                m = re.match(r"([-\d\s\xa0,.]+)\s*\(([\d.]+)%\)", value)
                if m:
                    try:
                        r[k_abs] = _num(m.group(1))
                        r[k_pct] = float(m.group(2))
                    except ValueError:
                        pass
        if "win_rate" not in r and any(label.startswith(n) for n in _WIN_LABELS[0]):
            m = re.match(r"(\d+)\s*\(([\d.]+)%\)", value)
            if m:
                r["profit_trades"] = int(m.group(1))
                r["win_rate"] = float(m.group(2))
    return r


# --------------------------------------------------------------------------
_TRADE_COLS = ["idx", "direction", "entry_bar", "entry_time", "entry_price", "sl",
               "tp", "entry_atr", "entry_spread_points", "exit_bar", "exit_time",
               "exit_price", "exit_reason", "bars_held"]


def read_mt5_trades(strategy: Strategy, copy_to_results=True):
    """Load the trade CSV the EA wrote into Common\\Files."""
    src = os.path.join(config.COMMON_FILES, trades_csv_name(strategy))
    if not os.path.isfile(src):
        raise RunnerError(
            "EA trade CSV not found: %s\n"
            "(the EA writes it with FILE_COMMON; check InpWriteTrades and that "
            "the tester actually ran)" % src)
    df = pd.read_csv(src)
    missing = [c for c in _TRADE_COLS if c not in df.columns]
    if missing:
        raise RunnerError("trade CSV is missing columns: %s" % missing)
    for c in ("entry_time", "exit_time"):
        df[c] = pd.to_datetime(df[c], format="%Y.%m.%d %H:%M:%S", errors="coerce")
    for c in ("entry_price", "sl", "tp", "entry_atr", "entry_spread_points",
              "exit_price", "profit", "swap", "commission", "lots", "trailed"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = np.nan
    for c in ("idx", "entry_bar", "exit_bar", "bars_held"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    df["points"] = np.where(df["direction"] == "long",
                            df["exit_price"] - df["entry_price"],
                            df["entry_price"] - df["exit_price"])
    if copy_to_results:
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        shutil.copy2(src, os.path.join(config.RESULTS_DIR,
                                       "%s_mt5_trades.csv" % strategy.name))
    return df


def spread_series_for(strategy: Strategy, df, mt5_bars=None):
    """Per-bar spread (in points) actually seen by the EA, aligned to ``df``.

    The EA records ``SYMBOL_SPREAD`` when a new bar opens but stamps the row
    with the SIGNAL bar's time (one bar earlier), so the series is shifted
    forward by one bar before it is joined onto the Python bar index.  Bars the
    EA never reached fall back to the median.

    Feeding this back into the Python engine turns "entry differs by the
    spread" from an unexplained residual into an exact match.
    """
    from core.types import tf_seconds

    if mt5_bars is None:
        mt5_bars = read_mt5_bars(strategy, copy_to_results=False)
    if mt5_bars is None or "spread" not in mt5_bars.columns:
        return None
    delta = pd.Timedelta(seconds=tf_seconds(strategy.timeframe))
    s = mt5_bars[["time", "spread"]].dropna()
    lookup = pd.Series(pd.to_numeric(s["spread"], errors="coerce").to_numpy(),
                       index=pd.to_datetime(s["time"]) + delta)
    lookup = lookup[~lookup.index.duplicated(keep="first")]
    out = pd.to_datetime(pd.Series(df["time"].to_numpy())).map(lookup)
    med = float(np.nanmedian(out.to_numpy(dtype=float))) if out.notna().any() else 0.0
    return out.fillna(med).to_numpy(dtype=float)


def read_mt5_bars(strategy: Strategy, copy_to_results=True):
    """Load the optional per-bar indicator CSV written by the EA."""
    src = os.path.join(config.COMMON_FILES, bars_csv_name(strategy))
    if not os.path.isfile(src):
        return None
    df = pd.read_csv(src)
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M:%S", errors="coerce")
    if copy_to_results:
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        shutil.copy2(src, os.path.join(config.RESULTS_DIR,
                                       "%s_mt5_bars.csv" % strategy.name))
    return df
