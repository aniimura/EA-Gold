# FxTrade_202608

A reproduction framework for the loop

```
    develop in Python  ->  backtest in Python  ->  generate MQ5
                                                        |
    iterate in Python  <-  PASS  <-  reconcile  <-  backtest in MT5
```

The goal is not "compare two backtests". It is to make the two agree so
reliably that, once a strategy has passed reconciliation **once**, you can keep
iterating inside Python alone and trust the numbers.

---

## Why the two normally disagree

Every divergence this project has actually hit, and what the framework does
about it:

| Cause | What it looked like | Handled by |
|---|---|---|
| `iATR` uses Wilder smoothing, `rolling().mean()` does not | 64.9% ATR difference; a whole backtest invalidated | no built-in indicators - both sides are generated from one formula |
| `iStdDev` is population, `pandas.std()` is sample | silent, small, wrong | `StdDev(..., ddof=)` is explicit and emitted into MQL5 |
| reading bar index 0 (still forming) | look-ahead; Python profits MT5 cannot reproduce | shift 0 **is** the last completed bar, everywhere; negative shifts are rejected at compile time |
| D1/H4 bars incomplete intraday | the #1 feature was look-ahead | same rule - there is no way to express "current bar" |
| `round()` is banker's, `NormalizeDouble` is half-away-from-zero | 1-point SL differences | `mt5_round()` replicates NormalizeDouble's arithmetic |
| pandas rolling keeps a running sum, MQL5 re-adds the window | 1-ulp differences that flip a rounding boundary | `_seq_sum` accumulates in the same order as the emitted loop |
| MT5 rejects stops closer than STOPLEVEL | 96% of orders silently dropped in MT5 only | both sides apply the same STOPLEVEL guard |
| Python enters at close, MT5 at ask | every entry off by the spread | entry is `open + spread`; `--replay-spread` feeds MT5's own per-bar spread back in |
| swap and commission | Python +10.42 vs MT5 -1.41 - the sign flipped | `Costs(...)`, calibrated by the reconciler |
| EA counts its own warmup bars | MT5 starts trading ~600 bars late | warmup is "are enough bars available", never "how long has the EA run" |
| a trailing stop moved to the wrong side of the market | MT5 rejects the modify, Python "exits" at a level that never existed, then every later trade shifts | both engines check the stop against bid/ask before moving it |
| the broker's stop fires on a bar's opening tick | MT5 re-enters on that bar, Python (and Pine) do not | the EA reads the closing deal's timestamp and blocks re-entry on that bar |
| a closing notification arrives after the next entry | the CSV logs the wrong entry price for that trade | trade rows are keyed by position id, not by "current" globals |
| `copy_rates_range` stops short of the requested end | Python silently loses the last day the tester used | the end date is over-requested, then clipped by the window mask |
| the current HTF bar is still forming | using this hour's EMA on an M1 bar knows how the hour ends | `HTF()` only ever exposes the last CLOSED higher-timeframe bar |
| broker server time vs UTC, across DST | session filters drift by an hour for half the year | `utc_hour` / `utc_minute_of_day`, converted by one EET/EEST rule mirrored in both languages |

---

## Quick start

```bash
py=C:\Users\Aruta\miniforge3\envs\py39env\python.exe

$py cli.py env                                          # check paths
$py cli.py all strategies/rsi_revert.py --bars --replay-spread
```

`all` runs: Python backtest -> generate `.mq5` -> compile -> MT5 Strategy
Tester -> reconcile, and prints a verdict.

Individual steps: `pybt`, `gen`, `mt5bt`, `recon`, plus `show` to print the
spec and the generated MQL5.

`cli.py chart <strategy.py>` draws the Strategy Tester's "Performance" panel -
cumulative P/L, per-trade run-up and drawdown, and a win/loss strip - from the
Python run alone, into `results/<name>_performance.png`. Both the net and the
gross curve are plotted: when they separate, costs are eating the edge.

`cli.py web <strategy.py>` writes the same panel as a self-contained
`index.html` for GitHub Pages - trade data embedded, chart drawn inline, no CDN
and no reference to `results/`, so publishing needs that one file and nothing
else. Re-run it after a backtest and commit; a hand-edited copy would be stale
the next time `pybt` runs.

---

## Writing a strategy

One file, one object. It is the **only** place the logic exists - the MQL5 is
generated from it.

```python
from core.indicators import ATR, EMA, RSI, Expr
from core.spec import Costs, Exits, Strategy

STRATEGY = Strategy(
    name="RsiRevert", symbol="GBPUSD", timeframe="H1",

    indicators={
        "atr":     ATR(14, method="sma"),
        "rsi":     RSI("close", 14, method="wilder"),
        "ema100":  EMA("close", 100),
        "stretch": Expr("(ema100 - close) / atr"),
    },

    entry_long="rsi < 30.0 and close < ema100 and stretch > 0.5",

    exits=Exits(sl_atr=2.0, tp_atr=3.0, atr_name="atr",
                max_hold_bars=72, exit_long="rsi > 60.0"),

    costs=Costs(commission_per_lot=7.93, swap_long_per_lot_night=-2.55),
    min_bars_between=12, lot=0.01, magic=20260802,
    date_from="2024-06-01", date_to="2026-03-01",
)
```

### Expression language

A restricted subset of Python that compiles to NumPy **and** MQL5:

```
name            value on the signal bar (the last completed one)
name[k]         value k bars before that              (k >= 0 only)
open high low close volume
+ - * /  unary -
> < >= <= == !=      chains allowed:  30.0 < rsi < 70.0
and or not
abs() min() max() sqrt()
```

`name[-1]` is rejected: look-ahead is a syntax error, not a bug you find later.

### Calendar fields

Available in any expression, alongside the price fields:

```
hour  minute_of_day  dow          broker server time
utc_hour  utc_minute_of_day       converted, DST-aware
```

Use the `utc_*` ones for anything specified in UTC (a TradingView script
always is). The conversion applies `broker_gmt_offset` plus the EU summer-time
rule, implemented identically in `core/timeutil.py` and in the generated MQL5 -
`TimeGMTOffset()` is unusable because inside the tester it reports the offset
of the real clock, not of the simulated bar.

### Indicators

`SMA` `EMA` `StdDev` `ATR` `RSI` `Highest` `Lowest` `Sum` `Expr` `HTF`

`HTF(inner, "H1")` evaluates an indicator on a higher timeframe and exposes
the value of the last **fully closed** higher-timeframe bar - the same thing
`request.security(..., [1], lookahead_on)` means in Pine. There is no way to
ask for the bar still forming.

`EMA`, `ATR(method='wilder')` and `RSI(method='wilder')` are recursive, so they
are seeded from an SMA a fixed `window` back rather than from the start of
history - otherwise the tester, which starts mid-history, could never match.
The default window is 6x (EMA) or 10x (ATR/RSI) the period.

Adding an indicator means writing one class with `compute()` and `mq5_body()`
in `core/indicators.py`. Keep the two arithmetically identical, including the
order of additions.

---

### Sizing and trailing stops

```python
sizing=Sizing(mode="risk", risk_money=5.0, lot_step=0.01, lot_min=0.01)
trail=Trail(start_money=3.0, step_money=1.0)
exits=Exits(sl_atr=3.0, sl_min_points=50, atr_name="atr")
```

`mode="risk"` sizes each trade so a stop-out costs about `risk_money`:
`floor(risk / (stop_distance x contract_size) / lot_step) x lot_step`, and the
trade is skipped when that falls below `lot_min`.

`Trail` locks in `peak - step_money` once open profit reaches `start_money`,
measured at the best price of a **closed** bar. The stop only moves in the
profitable direction, never past break-even, and never onto the wrong side of
the market - the last of those is what a broker enforces, so both engines do.

## Execution model

Both engines are restricted to what MT5 can reproduce exactly:

- one position at a time (fixed lot, or risk-sized)
- signal from the last completed bar, fill at the **next bar's open**
  (long pays the spread, short does not)
- SL/TP attached to the position, triggered intrabar from the bar's high/low
- time exit and signal exit at a bar open, evaluated before any new entry
- no re-entry on the same bar as an exit; `min_bars_between` bars between entries
- SL/TP normalised with `mt5_round`, rejected if closer than STOPLEVEL

When one bar contains **both** the SL and the TP, OHLC cannot say which came
first. The engine takes the SL and marks the trade `ambiguous`, so the
reconciler can tell path uncertainty apart from a real disagreement.

---

## Reading the reconciliation report

Two levels, because they fail for different reasons. **Check indicators
first** - if a formula was translated wrongly, no amount of trade-level
tweaking will help.

```
-- 1. indicators (bar by bar) -----
   indicator          n   max_abs_diff   max_rel_diff   n_bad  verdict
   atr            10791      4.286e-11      8.439e-08       0  MATCH
```

Every trade difference is classified rather than merely counted:

| class | meaning | action |
|---|---|---|
| `MATCH` | identical bar, price, stops, exit | none |
| `EOD` | the run ended with a position open; each side closed it at its own last price | none |
| `BOUNDARY` | a stop landed exactly on a .5-tick boundary, where one ulp decides the rounding | none - inherent |
| `SPREAD` | entry differs by about the spread | re-run with `--replay-spread` |
| `ROUNDING` | sub-point price difference | usually benign; investigate if frequent |
| `AMBIGUOUS` | SL and TP were both inside one bar | none - inherent |
| `LOGIC` | the two engines disagreed about **whether to trade** | a real bug - fix it |

Only `LOGIC` counts against the verdict. `PASS` means zero logic divergences.

### Section 2b: where the money went

The Python engine prices movement only; MT5 also books swap and commission.
Gross must match; costs explain the rest. The report prints a calibrated cost
block ready to paste into the spec:

```
    costs=Costs(
        commission_per_lot=7.93,
        swap_long_per_lot_night=-2.55,
        swap_short_per_lot_night=-2.55,
    ),
```

Do this once per symbol/broker. Skipping it is not cosmetic - on the bundled
demo strategy the costs are larger than the edge and the sign of the result
flips.

---

## Verified result

All three bundled strategies, FxPro-MT5 Demo:

| | RsiRevert | VolDip | ScalpGoldM1 |
|---|---|---|---|
| symbol / timeframe | GBPUSD H1 | GBPUSD H1 | GOLD M1 |
| period | 2024-06 .. 2026-03 | 2024-06 .. 2026-03 | 2025-09 .. 2026-08 |
| bars compared | 10,791 | 10,791 | 352,279 |
| indicators matching | 5 / 5 | 5 / 5 | 6 / 6 |
| entry signals matching | yes | yes | yes |
| trades python / MT5 | 116 / 116 | 17 / 17 | 761 / 761 |
| exact trade matches | 115 (+1 `EOD`) | 16 (+1 `BOUNDARY`) | 757 (+4 `ROUNDING`) |
| logic divergences | **0** | **0** | **0** |
| net profit py / MT5 | -1.30 / -1.41 | +40.20 / +40.21 | -42.73 / -56.70 |
| win rate py / MT5 | 42.2414 / 42.24 | 58.8235 / 58.82 | 54.01 / 53.48 |

`ScalpGoldM1` is a port of `Scalp_Gold_M1_fixed.pine` and exercises every
feature at once: a higher-timeframe filter, UTC session and news-blackout
windows, Wilder ATR, risk-based sizing and a money-denominated trailing stop.
Over a full year, 757 of its 761 trades match entry bar, entry price, stop
level, exit reason and exit price exactly; the other four differ by a
sub-point rounding on the stop and are classed `ROUNDING`, not `LOGIC`.

Reaching a year of M1 needs one terminal setting: `[Charts] MaxBars` in
`config/common.ini` caps the chart series, and at its default of 100000 -
below the ~370k bars a year of M1 contains - `copy_rates_range` returns
nothing at all rather than an error. Raise it (2000000 is ample) and restart
the terminal. The broker history itself is already on disk under
`bases/<server>/history/`; only the cap is in the way.

The result is a strategy whose gross P/L drifts up to +100 over the year
while its net never leaves negative territory: 761 trades cost 65.34 in
commission alone. The published chart plots both curves for exactly this
reason.

MT5's `Sharpe Ratio` is computed on the equity curve and will not equal the
per-trade Sharpe the Python engine reports. Compare trades, win rate, profit
factor and net profit; treat Sharpe as indicative only.

---

## Layout

```
core/       expr.py       expression -> NumPy + MQL5 (one source, two targets)
            indicators.py paired implementations, order-matched arithmetic
            spec.py       Strategy / Exits / Costs - the single source of truth
            backtest.py   the Python engine
            data.py       bars and symbol properties, pulled from MT5
            types.py      Trade, SymbolInfo, mt5_round
            config.py     paths (override via FXT_* env vars or config_local.py)
codegen/    mq5gen.py     Strategy -> .mq5
runner/     mt5run.py     compile, drive the tester, read the EA's CSVs
reconcile/  compare.py    bar-level and trade-level diff + verdict
report/     chart.py      the tester's Performance panel, from the Python run
            web.py        the same panel as a self-contained index.html
core/       timeutil.py   server-time -> UTC, one DST rule for both languages
strategies/ voldip.py, rsi_revert.py, scalp_gold_m1.py
build/      generated .mq5   (regenerated - never edit by hand)
data/       cached bars, symbol info
results/    trades, reports, reconciliation output
```

The generated EA writes its own trade CSV (and, with `--bars`, a per-bar
indicator CSV) into the terminal's `Common\Files`. Reconciliation reads those
directly rather than scraping the tester log, which is why it does not break
when MT5 changes its log format or localisation.

---

## Day-to-day workflow

1. Write or edit `strategies/<name>.py`.
2. `cli.py pybt strategies/<name>.py` - iterate here, it takes seconds.
3. When a candidate looks good, once:
   `cli.py all strategies/<name>.py --bars --replay-spread`
4. `PASS` -> paste the calibrated `Costs(...)` into the spec and go back to
   step 2 with confidence.
   `FAIL` -> fix the indicator that diverges, or the logic divergence, first.

Re-run step 3 whenever you add a new indicator, change the exit rules, or
switch symbol/timeframe/broker. Those are the things that can break the
correspondence; threshold tuning is not.

## Limits worth knowing

- One symbol, one open position. Higher timeframes are read-only filters.
  No pyramiding, no hedging, no pending orders, no partial closes.
- `pnl_money` assumes the quote currency is the account currency (true for
  GBPUSD/EURUSD on a USD account). For other pairs compare `points` and
  `pnl_atr`, and take the money figure from MT5.
- Costs are a flat per-lot model. Brokers vary swaps over time; recalibrate if
  the period moves a lot.
- The tester's end-of-run forced close is not fully observable from an EA, so
  the last trade is booked at the last known price on both sides and reported
  as `EOD`.
