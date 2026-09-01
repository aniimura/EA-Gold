# CHANGELOG — XAU Multi-Speed Volatility-Scaled Donchian, v1 → v2

Every behavioural change is listed here with its measured effect on the
2022-01-01 → 2026-08-28 FxPro GOLD H4 record (7,206 bars, $100,000 account).
Nothing was changed silently, and nothing was tuned against the result.

---

## 0. Repository audit (Phase 1)

### Files found and what they did

| File | Role |
|---|---|
| `XAU_MultiSpeed_VolScaled_Donchian.pine` | Pine v6 strategy. Three sleeve state machines via three call sites of one function (Pine gives each call site its own `var` storage), netted with `strategy.order` deltas. |
| `bt_xau_msvsd.py` | v1 single-file Python simulator, ~470 lines: `pine_atr`, `donchian`, a `Sleeve` class, a `step()` function, one `run()` loop, and a `stats()`/`per_year()` reporting tail. |
| `core/` | Repo-wide framework: `data.py` (MT5 cache), `timeutil.py` (server→UTC with the EU DST rule), `backtest.py` (`count_rollovers`, `summarize`), `spec.py` (declarative single-position `Strategy`). |
| `results/XauMsvsd_*` | v1 outputs for 7 runs. |
| `report_xau_msvsd.html` | v1 published report. |
| — | **No test directory existed.** `pytest` is not installed in the project interpreter. |

### How v1 worked

- **Sleeves.** One `Sleeve` per configuration; `step()` ran four blocks per bar: (A) register the fill of an order sent last bar at this bar's open and set the stop from the ATR frozen at signal time; (B) H4 stop check on the completed bar's low/high; (C) Donchian channel exit on the close; (D) entry for flat sleeves.
- **Netting.** `net_lots_raw = Σ dir×lots` → notional cap at 1.5× equity → floor to the lot step → trade only the delta against `pos_oz`.
- **Execution.** Orders queued at bar *i*'s close, filled at bar *i+1*'s open. Bars are bid: buy at `open + spread + slip`, sell at `open − slip`.
- **Costs.** Commission 7.85 USD/lot round turn charged per side; one flat swap pair (−52.40 long / +23.58 short per lot per night, Wednesday ×3) applied across all 4.7 years.

### Deviations from the specification found in the audit

Five. Two are defects that changed results; three are modelling choices that
diverge from the Pine.

---

## 1. DEFECT-V1-EXIT-SWALLOW — **fixed** (changes results)

**What was wrong.** v1's entry block read:

```python
if new_dir != 0:
    ...
    if lots <= 0:
        new_dir = 0        # cannot size the reversal
    else:
        sl.reset(); sl.dir = new_dir; ...
elif exit_ev != 0:
    sl.reset()
```

Python evaluates `if new_dir != 0:` once. When a bar produced **both** an exit
signal **and** a reversal whose size floored to zero lots, control entered the
first branch, zeroed `new_dir` inside it, and never reached the `elif`. The
sleeve therefore **kept a position it had just been told to close** — a direct
violation of the exit rules in the specification.

**When it fired.** Only in high-ATR conditions, where
`risk_cash / (2.5 × ATR × 100)` falls below one lot increment (ATR > 40 on a
$100k account at 0.10 % risk). **22 bars** of the record; first occurrence
2025-10-21 16:00, during the October 2025 gold break.

**Effect.** Net profit `+$3,546.61 → +$3,624.47` (**+$77.86**). Guarded by
`tests/test_core_mechanics.py::test_exit_is_not_swallowed_by_an_unsizable_reversal`.

## 2. DEFECT-V1-TRADELOG — **fixed** (changes the trade log, not the cash)

**What was wrong.** v1 snapshotted the sleeve *before* block A ran, so a
position that opened at bar *i*'s open and closed at bar *i*'s close had
`entry_px = NaN` at snapshot time and was skipped by the trade-logging guard.
The ledger executed it; the trade log never showed it.

**Effect.** **2 trades** missing from the v1 log (304 → 306), carrying
**−$220.57** of gross. Account P&L was always correct — which is why v1's
commission and spread totals reproduce to machine precision under compat mode.

**Consequence for the v1 report.** The published v1 figure "netting and lot
rounding −$23.58" was mislabelled. The notional cap never binds in the baseline
and the net target is always an exact multiple of the lot step, so lot rounding
contributes **nothing**. That residual was entirely the sleeve still open on the
final bar, which the account closed but the trade log did not record. With
`--log-open-at-end` the two books reconcile to **exactly 0.00**.

### 2b. LOT-ROUNDING was measured in the wrong place

v1 (and v2's first cut) reported "lot rounding" as the loss from flooring the
**net target**. That number is **0.00** and always will be: sleeve sizes are
already multiples of the lot step, so their sum is too. The rounding that
actually costs money happens one level down, when each sleeve sizes itself —
`floor(risk_cash / (2.5 × ATR × 100))` rounds **down**, discarding part of the
risk budget on every single entry.

Measured properly (`sizing_rounding_loss_pct`):

| Account | Lot step | Intended lots | Executed lots | Budget discarded |
|---|---|---:|---:|---:|
| $100,000 | 0.01 | 9.89 | 8.44 | **14.66 %** |
| $100,000 | 0.001 | 10.03 | 9.86 | 1.70 % |
| $1,000,000 | 0.01 | 100.31 | 98.60 | 1.70 % |

Nearly a seventh of the intended risk is thrown away at the baseline account
size, and it is worth **+$932** of net profit to recover (`--lot-step 0.001`:
$3,624 → $4,556). The drag also falls as risk rises — 14.7 % at 0.10 % risk,
6.3 % at 0.25 % — which quietly flatters the higher-risk cells in the grid.

## 3. FRIDAY-BASIS — divergence from the Pine, **default unchanged**

The Pine tests `time_close` against 13:00 New York; v1 tested the bar **open**.
On H4 that shifts the cut-off by one bar. Default remains `open` so the frozen
baseline is untouched; `--friday-basis close` selects the Pine-faithful
behaviour and **is required for reconciliation**. Runs on the v1 basis are
labelled `FRIDAY_FILTER_ON_BAR_OPEN__DIVERGES_FROM_PINE`.

## 4. FINANCING-TIMING — **default unchanged**, corrected option added

v1 charged the overnight rollover on the position held *after* the current
bar's open fill, though the position held overnight was the pre-fill one.
Default stays `post-fill`; `--financing-timing pre-fill` is correct. Labelled
`CARRY_CHARGED_AFTER_OPEN_FILL__V1_COMPAT`.

## 5. FINAL-CLOSE — **default unchanged**, corrected option added

v1 closed the residual position on the last bar at the raw close with no
spread, slippage or commission. `--log-open-at-end` records it as a trade;
`close_final_position_with_costs` charges it.

---

## Baseline reproduction

| Metric | v1 golden fixture | v2 `--v1-compat` | v2 default (defects fixed) |
|---|---:|---:|---:|
| Net profit | 3,546.609850 | **3,546.609850** | 3,624.4719 |
| Sleeve trades | 304 | **304** | 306 |
| Gross P&L | 6,226.400000 | **6,226.400000** | 6,005.8300 |
| Swap | −2,366.305400 | **−2,366.305400** | −2,362.1134 |
| Commission | 66.214750 | **66.214750** | 66.2148 |
| Spread + slippage | 223.690000 | **223.690000** | 223.6900 |

**Tolerances.** `--v1-compat` reproduces the fixture to **≤ 5.5 × 10⁻¹²** on
every money figure and the position path is bit-identical on all 7,206 bars
(max deviation 8.9 × 10⁻¹⁶ oz — IEEE-754 addition-order noise only). The
declared tolerance is **1 × 10⁻⁶ USD absolute**, three orders of magnitude
looser than observed. Enforced by `tests/test_analysis.py::TestGoldenBaseline`.

---

## New capability (all optional, all off by default)

| Area | Added |
|---|---|
| Data | `--h4-file`, `--ltf-file`, `--swap-file`, `--events-file`, `--date-from/--date-to`; UTC/server basis handling; validation for duplicates, ordering, NaN OHLC, `high < low`, close outside range, non-positive prices; H4↔LTF coverage reporting. Fatal problems raise; nothing is repaired or interpolated. |
| Financing | Historical per-rollover CSV; `error` (default) / `zero` / `forward-fill` / `scenario-rate` policies; low/base/high scenarios; per sleeve × direction × year attribution; actual-vs-virtual carry reconciliation. |
| Execution | M1/M5 intrabar stop replay; gap-through-stop fills at the LTF open; never any price improvement on a stop; deterministic rule and an audit log for same-bar multi-sleeve stops. |
| Analysis | Trend campaigns with the reversal-boundary rule; `sleeve_trades.csv`, `campaigns.csv`, `daily_equity.csv`, `daily_returns.csv`; concentration at trade and campaign level. |
| Statistics | Campaign bootstrap (R and USD), monthly and quarterly block bootstraps, Lo autocorrelation-adjusted Sharpe, Sortino, Calmar, Deflated Sharpe; the trade-level t-test retained but labelled SECONDARY. |
| Reconciliation | Pine debug-export mode (28 gated `plot()` series, no state changes); `--reconcile` with per-field tolerances, mismatch CSV and PASS/FAIL. |
| Challengers | 3 sleeve configurations × 3 direction modes × 5 ATR multipliers × 4 exit scalings × 4 risk levels, plus cost and carry stress suites. |

### Deflated Sharpe units bug, found and fixed during development

The first implementation fed an **annualised** Sharpe alongside a **daily**
observation count, inflating *z* by √252 and pinning the DSR at 1.0 — a
reassuring number that meant nothing. Both inputs are now per-observation. The
corrected DSR for the apparent best cell of the stop-mode suite is **0.75**,
below the 0.95 threshold.

---

## Known limitations

1. **The Pine and Python are still not reconciled.** The machinery is built and
   tested, but no TradingView export has been run through it. The report says
   `NOT_RECONCILED` until one is.
2. **Campaign cost attribution is by timestamp.** A trade entering in one
   campaign and exiting after a reversal boundary books its gross in the first
   and its exit cost in the second. Campaign net P&L therefore sums to
   $3,602.56 against an account net of $3,624.47 — a 0.6 % attribution gap,
   reported rather than forced to zero.
3. **Carry feeds back into sizing.** Position size is a fraction of live
   equity, so a heavier swap assumption lowers equity, lowers size, and can
   push a sleeve below one lot increment. Signals are price-only, but trade
   *counts* differ slightly between carry scenarios (306 vs 305 at 1.75×). The
   feedback is negative and self-damping: carry scales by 1.74×, not 1.75×.
4. **`--suite grid` is 720 cells on one sample.** The DSR trial count exists to
   punish exactly the selection it enables.
5. **No historical swap data ships with this repo.** `schemas/swap_rates_SYNTHETIC_*.csv`
   are the flat assumption written into the historical schema, useful for
   testing the code path and for scenario work. They are not observed rates and
   every run using them is labelled.
6. **Sub-H4 entry timing is not replayed.** The LTF engine resolves stops only;
   entries still fill at the next H4 open by design.

---

## Results produced by this build

All figures net of the selected spread, slippage, commission and financing
model. Full outputs in `results/v2/`; `results/` still holds the untouched v1 run.

### Baseline reproduction and corrected variants

| Run | Net USD | Return | Max DD | Sharpe (Lo-adj.) | Campaigns |
|---|---:|---:|---:|---:|---:|
| `v1_golden` (`--v1-compat`) | 3,546.61 | 3.55 % | 2.61 % | 0.379 | 122 |
| `baseline` (defects fixed) | 3,624.47 | 3.62 % | 2.61 % | 0.385 | 122 |
| `corrected` (Pine Friday basis, pre-fill carry) | 3,528.45 | 3.53 % | 2.56 % | 0.378 | 120 |
| `ltf_m1` (real intrabar stops) | **2,795.90** | 2.80 % | 2.71 % | 0.288 | 122 |

### The H4 stop approximation was optimistic, not conservative

v1 described its H4 stop model as conservative. Replaying all 1,649,768 M1 bars
says otherwise: **−$828.57 (−23 % of net profit)**, Sharpe 0.385 → 0.288. The
approximation delays the exit to the next H4 open, and on this data price had
frequently recovered above the stop by then. 123 stops resolved at the stop
price, **0 gapped through**, **21 same-bar multi-sleeve ambiguities** logged.
M5 and M1 agree to within $2, so the effect is not a data-resolution artefact.

### Statistical strength collapses under campaign-level resampling

| Test | Result |
|---|---|
| t-test on 306 sleeve trades (**SECONDARY**) | t = 1.83, p = 0.069 |
| IID trade bootstrap (**SECONDARY**) | mean +0.237 R, 95 % [+0.001, +0.503], P(≤0) = 0.025 |
| **Campaign bootstrap, USD (PRIMARY)** | mean **+$27.31**, 95 % **[−35.77, +104.84]**, **P(≤0) = 0.238** |
| Monthly block bootstrap (PRIMARY) | 56 effective blocks, P(≤0) = 0.181 |
| Quarterly block bootstrap (PRIMARY) | 19 effective blocks, P(≤0) = 0.132 |

Treating 306 overlapping sleeve trades as independent gives p = 0.069. Treating
the 122 campaigns as the unit of evidence — which is what they are — gives
P(mean ≤ 0) = 0.24. The apparent significance was mostly an artefact of
double-counting the same trends.

### Challenger grid — 720 configurations

100 % of cells were profitable, which says more about a market that rose 144 %
than about the strategy. Sharpe ranged 0.220 to 0.894 (median 0.599).

| Direction mode | Mean net | Mean max DD | Mean Sharpe |
|---|---:|---:|---:|
| `long-only` | 8,871 | 2.80 % | 0.705 |
| `slow-confirmed-shorts` | 8,279 | 2.93 % | 0.619 |
| `symmetric` (baseline) | 6,144 | 3.25 % | 0.489 |

**Deflated Sharpe for the apparent best cell** (`all / long-only / ATR 2.0 /
exit ×1.25 / risk 0.25 %`, Sharpe 0.885): **DSR = 0.808**, against a 0.95
threshold. Across 720 trials the expected best-by-chance daily Sharpe is
0.0265; this cell scored 0.0500. **It does not clear deflation.** No
configuration here is recommended, and `long-only` looking best is exactly what
one bull market produces.

### Risk sensitivity — scaling risk does not improve the edge

| Risk/sleeve | Return | Max DD | Sharpe | Cap binding | Sizing budget discarded |
|---|---:|---:|---:|---:|---:|
| 0.10 % | 3.62 % | 2.61 % | 0.385 | 0 bars | 14.66 % |
| 0.15 % | 7.04 % | 4.23 % | 0.422 | 0 bars | 10.83 % |
| 0.20 % | 8.34 % | 5.54 % | 0.386 | 0 bars | 7.93 % |
| 0.25 % | 11.03 % | 7.36 % | 0.395 | 0 bars | 6.31 % |

Return and drawdown scale together; Sharpe stays flat at 0.39 ± 0.02. **Risk
scaling changes the size of the bet, not the quality of it.** The 1.5×
notional cap never binds at these sizes.

### Cost and carry stress

| Scenario | Net USD |
|---|---:|
| Baseline (1× costs, flat carry) | 3,624 |
| 2× spread and slippage | 3,341 |
| 3× spread and slippage | 3,118 |
| Carry scenario low (0.5×) | 4,804 |
| Carry scenario high (1.75×) | 1,843 |
| Lot step 0.001 | 4,556 |
| $1M account | 45,561 |
| No swap — **TradingView comparison only** | 6,061 |

Carry remains the dominant single assumption: halving it adds 33 % to net,
raising it 1.75× removes half. Execution cost is comparatively minor — tripling
spread and slippage costs only $506, because the strategy trades rarely.

### Test results

71 tests, 0 failures, 0 errors (`python tests/run_all.py`, ~16 s):
core mechanics 20 · execution and stops 8 · financing 18 · campaigns and
statistics 15 · Pine reconciliation 10.

---

## MT5 cross-check — the cost model tested independently

`build/XauMsvsd.mq5` (hand-written) plus `run_mt5_msvsd.py`. FxPro GOLD H4,
2022-01-01 → 2026-08-31, model 1 (one-minute OHLC), $100,000, 7,204 bars.

### Signals: exact agreement

| Check | Result |
|---|---|
| Signal-field value mismatches | **0 in 135,018 comparisons** |
| Max price difference, any field | **4.96 × 10⁻⁹** |
| Sleeve trades | 165 / 94 / 48 in **both** engines |
| Entries matched on sleeve + time + direction | **307 of 307** |
| Coverage-only rows (one side has no history yet) | 756 |

The 756 coverage rows are the first 63 bars, where MT5 holds more history than
the Python H4 cache and can price a channel Python cannot. Where both are
defined the values are identical to machine precision.

Two EA defects were found and fixed by this exercise: the debug CSV wrote stops
at 2 decimal places (making every stop look wrong by half a tick), and the
position column was stamped after the bar's own order instead of before it,
shifting the whole series by one bar. Neither affected trading.

### Costs: one assumption held, one did not

| Component | Python model | MT5 (broker) | Difference |
|---|---:|---:|---:|
| Gross price P&L | 6,276.49 | 6,230.17 | −46.32 |
| Swap / carry | −2,362.11 | −2,414.25 | −52.14 (**2.2 %**) |
| Commission | −66.21 | −60.27 | +5.94 |
| Spread + slippage | −223.69 | **−333.06** | **−109.37 (1.49×)** |
| **Net profit** | **3,624.47** | **3,422.59** | **−201.88 (−5.6 %)** |

MT5 also reports profit factor 1.25, Sharpe 0.42, win rate 39.0 %, balance
drawdown 1.82 % and equity drawdown 3.04 % over 282 netted broker trades (the
EA's 307 figure counts virtual sleeve trades, which is the Python unit).

**The carry assumption survives.** One flat rate pair applied across 4.7 years
lands within **2.2 %** of what the broker actually charged. That was the single
largest modelling assumption in the project and it is now measured, not asserted.

**The execution assumption is optimistic by about half.** Real spread cost is
**1.49× the model's** — $333 against $224. The Python engine prices fills off
the H4 cache's spread column (median 15 points) plus a flat 5-point slippage;
the tester's minute data carries the wider spreads actually quoted at bar opens.
That gap is $109.

**Consequence.** The Python figures overstate net profit by about **5.6 %** on
this cost model. The signal engine is verified exactly; the cost engine's
execution term needs per-fill quoted spreads before a headline is called
deployable.

### A stale-report trap, found and fixed

`_find_report()` accepts any report whose mtime is newer than the run start. A
launch that fails leaves the previous report in place, and a later run can then
"finish in 3 seconds" and report the PREVIOUS configuration's P&L as its own.
This happened here: an initial MT5 net of 3,167.37 was read from the first,
buggy EA build and quoted as if it came from the corrected one. `run_tester()`
now deletes the old report before launching and warns loudly when the EA's own
CSVs are absent, because those are the only proof the pass actually ran.

### Account size: $10,000 is not viable at the specified risk

At 0.10 % per sleeve, the position a sleeve wants is
`capital × 0.001 / (2.5 × ATR × 100)` lots. FxPro GOLD has `volume_min = 0.01`
and a 100 oz contract, so the smallest tradeable position is 1 oz, carrying
`2.5 × ATR` dollars of risk — $32 at median ATR.

| Capital | Risk needed to buy one increment | Verdict |
|---|---|---|
| $10,000 | ≈ 0.33 % per sleeve at median ATR | **zero trades at 0.10 %** |
| $100,000 | ≈ 0.033 % | works, but discards 14.7 % of the budget |

Measured over the record, **100 % of bars cannot fund one lot increment at
0.10 % risk on $10,000**, and the strategy takes no trades at all. Raising risk
to compensate destroys the volatility scaling the strategy is named for:

| $10,000 run | Trades | Return | Max DD | Sharpe | Distinct sizes | corr(size, 1/ATR) |
|---|---:|---:|---:|---:|---:|---:|
| 0.10 % | 0 | — | — | — | — | — |
| 0.25 % | 91 | +5.16 % | 4.46 % | 0.40 | **1** | **+0.00** |
| 0.50 % | 242 | +11.45 % | 10.94 % | 0.30 | 3 | +0.88 |
| 1.00 % | 301 | −0.58 % | 32.21 % | 0.09 | 7 | +0.96 |

At 0.25 % every position is the minimum lot and size no longer responds to
volatility at all. The fix is granularity, not risk: with a 0.001 lot step, or a
10 oz micro contract, $10,000 reproduces the $100,000 baseline exactly
(+3.62 %, DD 2.61 %, Sharpe 0.39, corr +0.98).

Note that swap and commission are quoted **per standard lot**; a micro contract
must carry them pro rata (`rate × contract_oz / 100`) or the carry is inflated
by the contract-size ratio.
