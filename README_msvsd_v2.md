# XAU Multi-Speed Volatility-Scaled Donchian — v2 research framework

Three independent Donchian sleeves (20/10, 55/20, 120/40) netted into one
XAUUSD H4 position, sized at 0.10 % of equity per sleeve behind a 2.5 × ATR
stop. v1 answered "does this have a pulse?". v2 exists to test the assumptions
that answer rested on: one flat swap rate across 4.7 years, protective stops
approximated from H4 bars, and a t-test over overlapping sleeve trades.

**Nothing here is a recommendation to trade.** No parameter was tuned against
the result, and no variation is claimed to beat the baseline.

```
msvsd/            engine package        experiments.py    declared grid runner
  config.py         frozen spec + every optional switch
  dataio.py         load & validate; never repairs, never downloads
  indicators.py     Pine-semantics ATR and Donchian
  sleeves.py        the virtual sleeve state machine
  engine.py         netting, ledger, LTF stop replay
  financing.py      historical / scenario carry with an explicit missing policy
  campaigns.py      trend campaigns — the unit of independent evidence
  statistics.py     bootstraps, Lo-adjusted Sharpe, Deflated Sharpe
  reconcile.py      Pine ↔ Python bar-by-bar
  reporting.py      tables, exports, console summary
bt_xau_msvsd.py   CLI            tests/          71 tests, stdlib unittest
schemas/          CSV schemas with clearly fake example data
results/v2/       all v2 output (results/ still holds the untouched v1 run)
```

Interpreter: `C:\Users\aruta\.conda\envs\py39env\python.exe`. Dependencies are
numpy, pandas and scipy — all already present. `pytest` is **not** installed,
so tests use stdlib `unittest`.

---

## Commands

### Frozen baseline

```bash
# v2 default: the specification, with the two v1 defects fixed
python bt_xau_msvsd.py --tag baseline

# bit-exact reproduction of the frozen v1 fixture (reinstates both defects)
python bt_xau_msvsd.py --tag v1_golden --v1-compat

# the corrected-modelling variant: Pine-faithful Friday basis, pre-fill carry,
# open sleeves logged so the two books reconcile to 0.00
python bt_xau_msvsd.py --tag corrected \
    --friday-basis close --financing-timing pre-fill --log-open-at-end
```

### Lower-timeframe stop replay

```bash
python bt_xau_msvsd.py --tag ltf_m1 --stop-mode ltf --ltf-file M1
python bt_xau_msvsd.py --tag ltf_m5 --stop-mode ltf --ltf-file M5
python bt_xau_msvsd.py --tag ltf_csv --stop-mode ltf --ltf-file /path/to/M1.csv

# side-by-side comparison table (H4 approximation vs M5 vs M1)
python experiments.py --suite stops
```

`--ltf-file M1|M5|M15` uses the repo's MT5 cache; anything else is treated as a
path. Without lower-timeframe bars the run keeps the H4 approximation and is
labelled `APPROXIMATE_INTRABAR_STOPS` — it never silently substitutes one for
the other.

### Historical financing

```bash
# refuses to guess: errors on the first rollover the file does not cover
python bt_xau_msvsd.py --tag hist --swap-model historical \
    --swap-file schemas/swap_rates_EXAMPLE.csv

# full-period run against a synthetic table in the historical schema
python bt_xau_msvsd.py --tag hist_base --swap-model historical \
    --swap-file schemas/swap_rates_SYNTHETIC_base.csv

# explicit fallback policies (each one labels the result as non-historical)
python bt_xau_msvsd.py --tag hist_ff --swap-model historical \
    --swap-file schemas/swap_rates_EXAMPLE.csv --swap-missing-policy forward-fill

# scenario carry, signals untouched
python bt_xau_msvsd.py --tag swap_high --swap-model scenario --swap-scenario high
```

Schema — one row per rollover, holding the **actual cash** charged per lot at
that rollover, so triple-swap nights are encoded in the file and nothing
multiplies by three:

```csv
date,long_swap_usd_per_lot,short_swap_usd_per_lot
2024-01-02,-40.00,18.00
2024-01-03,-120.00,54.00      # Wednesday: the triple is already in the row
```

### Campaign bootstrap

```bash
python bt_xau_msvsd.py --tag boot --bootstrap 20000 --seed 20260901
```

Writes `campaigns.csv`, `sleeve_trades.csv`, `daily_equity.csv`,
`daily_returns.csv` and a `summary.json` carrying the IID trade bootstrap
(SECONDARY), the campaign bootstrap in R and USD (PRIMARY), and monthly and
quarterly block bootstraps (PRIMARY).

### Pine reconciliation

1. Open the strategy in TradingView on XAUUSD 4H.
2. Settings → **Debug export (reconciliation)** → *Enable debug export series*.
3. Chart menu → **Export chart data…** → CSV.
4. Run:

```bash
python bt_xau_msvsd.py --tag recon --friday-basis close \
    --reconcile /path/to/tradingview_export.csv
```

`--friday-basis close` is **required**: the Pine tests the bar close against
13:00 New York and v1 tested the bar open. Without it every Friday-boundary bar
mismatches. The command prints mismatch count, first mismatch, max price and
quantity differences, and PASS/FAIL, and writes
`results/v2/recon_reconcile_mismatches.csv`.

### Challenger experiments

```bash
python experiments.py --suite axes    # one axis at a time (16 cells)
python experiments.py --suite grid    # full cross product (720 cells, ~15 min)
python experiments.py --suite all     # axes + costs + stops

# individual challengers
python bt_xau_msvsd.py --tag sleeves_medslow --sleeves medium-slow
python bt_xau_msvsd.py --tag sleeves_slow    --sleeves slow-only
python bt_xau_msvsd.py --tag dir_longonly    --direction long-only
python bt_xau_msvsd.py --tag dir_slowconf    --direction slow-confirmed-shorts
python bt_xau_msvsd.py --tag atr_3.0         --atr-mult 3.0
python bt_xau_msvsd.py --tag exits_1.25      --exit-scale 1.25
python bt_xau_msvsd.py --tag risk_0.25       --risk 0.25
```

`slow-confirmed-shorts`, stated exactly: the slow sleeve's own short breakout
is unrestricted; fast and medium may hold a short only while the slow sleeve is
in a **confirmed** short — short *and* its entry fill already registered — as
of the start of that bar's evaluation. The snapshot is taken once per bar for
all sleeves, so the outcome does not depend on iteration order. When
confirmation disappears, fast and medium shorts close with reason
`EXIT_DIRECTION_MODE` at the next bar's open. Long behaviour is unchanged.

Exit-window scaling multiplies **exit** windows only (10/20/40), rounded to the
nearest whole bar with a floor of 1. Entry windows are never scaled.

### Cost and execution stress

```bash
python experiments.py --suite costs --swap-file schemas/swap_rates_SYNTHETIC_base.csv

python bt_xau_msvsd.py --tag cost2x   --cost-scale 2
python bt_xau_msvsd.py --tag cost3x   --cost-scale 3
python bt_xau_msvsd.py --tag lot001   --lot-step 0.001
python bt_xau_msvsd.py --tag cap1m    --capital 1000000
python bt_xau_msvsd.py --tag noswap   --no-swap        # TradingView comparison ONLY
```

### Scheduled events

```bash
# default: identify trades and fills near events, block nothing
python bt_xau_msvsd.py --tag ev_report --events-file schemas/events_EXAMPLE.csv

# suppress new entries; exits and stops are never suppressed
python bt_xau_msvsd.py --tag ev_block --events-file schemas/events_EXAMPLE.csv \
    --event-mode block-new-entries
```

```csv
timestamp,event_name,blackout_before_minutes,blackout_after_minutes
2024-01-10T13:30:00Z,EXAMPLE_CPI,30,60
```

`schemas/events_EXAMPLE.csv` contains four fabricated windows. It is **not** an
economic calendar and must be replaced with one from a real feed.

### Tests

```bash
python tests/run_all.py                      # all 71
python -m unittest tests.test_execution -v   # one module
```

---

## Audits (never results)

```bash
python bt_xau_msvsd.py --tag audit_lookahead --lookahead
python bt_xau_msvsd.py --tag audit_samebar   --same-bar-fill
```

The lookahead audit puts the current bar back into the Donchian window and
produces **zero trades** — `close > highest(high, n)` cannot fire when a bar's
own high is in the window. That is the correct failure signature of the bug and
confirms the `[1]` offsets are load-bearing rather than decorative.

---

## Run labels

Every run stamps warning labels into `summary.json` and the console header:

| Label | Meaning |
|---|---|
| `APPROXIMATE_INTRABAR_STOPS` | H4 stop approximation; supply `--ltf-file` to replay properly |
| `NO_CARRY_MODELLED__NOT_DEPLOYABLE` | `--no-swap`; a TradingView comparison, never performance |
| `FLAT_CARRY_ASSUMPTION` | one swap pair across the whole history |
| `SCENARIO_CARRY__LOW/BASE/HIGH` | assumed rates, not observed |
| `CARRY_GAPS_FILLED__*` | a non-`error` missing policy was used |
| `V1_COMPAT__REPRODUCES_KNOWN_DEFECTS` | the two v1 defects are deliberately reinstated |
| `FRIDAY_FILTER_ON_BAR_OPEN__DIVERGES_FROM_PINE` | v1 basis; use `close` to reconcile |
| `MULTIPLE_TESTING__N_CONFIGS` | pass `--n-configs-tested` after a grid run |
| `AUDIT_*__NOT_A_RESULT` | a deliberately wrong configuration |

---

## Reading the statistics

The trade-level t-test is retained and labelled **SECONDARY**. It is wrong in
two ways at once: sleeve trades overlap in time (three sleeves ride one move,
so they are not independent draws), and the distribution has a long right tail.
Both violations push the same way — they make the edge look more certain than
the evidence supports.

Read the **campaign** bootstrap and the **block** bootstraps instead. And read
both campaign statistics: the equal-weighted mean R and the risk-weighted R
disagree in sign for this strategy, because a three-sleeve campaign risks about
three times what a one-sleeve campaign risks and the winners are the big ones.
That disagreement is a finding, not an artefact.

A confidence interval that excludes zero is not proof of a durable edge. One
instrument, one broker cost model, one regime.

---

## MT5 cross-check (third implementation)

`build/XauMsvsd.mq5` is the same specification written a third time, for the
MT5 Strategy Tester. It is hand-written: `codegen/mq5gen.py` emits one position
driven by one entry expression, which cannot express three netted sleeves.

```bash
python run_mt5_msvsd.py                  # compile, test, compare
python run_mt5_msvsd.py --compile-only
python run_mt5_msvsd.py --compare-only   # reuse the last tester output
python run_mt5_msvsd.py --clear-cache    # force tick regeneration (~9 min)
```

No credentials are used or stored. The tester runs against the history the
terminal already holds; the terminal reports "not synchronized with trade
server" and that is fine. If a login is ever needed, do it interactively.

**Why MT5 and not just Pine.** TradingView models no overnight carry at all, so
it cannot check the largest assumption in the Python engine. MT5 charges the
broker's own historical swap, the spread carried in the tick data, and real
order handling. That makes it the only available independent test of the cost
model, not just the signal logic.

**How to read the output.** The comparison separates two classes of field:

- *Signal fields* (ATR, all twelve Donchian levels, sleeve states, reason
  codes) are pure functions of price and **must** agree exactly.
- *Cost-dependent fields* (equity, sleeve sizes, net target, position) are fed
  by equity, which the two engines compute from different swap, spread and
  commission models. They cannot agree, and a PASS was never available.

A NaN on one side is a **coverage** difference, not a disagreement — MT5 holds
more history than the H4 cache, so it can price a 120-bar channel on bars where
Python has none. Those are counted and reported separately.

**Reconciling requires matching the modelling choices on both sides.** The EA
implements the H4 stop approximation, the Friday filter on the bar open, and
logs sleeves still open at the end, so the comparison runs the Python side with
`stop_mode="h4"`, `friday_basis="open"`, `log_open_sleeves_at_end=True`. The
`InpAtrSeedFrom` input anchors `ta.atr`'s recursion to the first bar of the
Python cache — without it the two RMAs converge but never become equal, which
shows up as a small early ATR difference and a handful of differently-sized
entries.

### Account size and leverage

Leverage is **1:100** in the MT5 tester (`TesterSpec.leverage`); the Pine sets
`margin_long/short = 0` and the Python engine models no margin at all. It never
binds: peak exposure is 0.20 lots, about $90,000 notional, needing $900 of
margin on a $100,000 account. Exposure is governed by the 1.5× equity notional
cap, which also never binds at these sizes.

**$10,000 does not work at the specified 0.10 % per sleeve.** The broker's
minimum order (0.01 lots = 1 oz on a 100 oz contract) already risks about
`2.5 × ATR` dollars — roughly 0.33 % of a $10,000 account at median ATR — so
every entry rounds down to zero and the strategy takes no trades. Raising the
risk percentage to compensate turns it into a fixed-size strategy: at 0.25 %
every position is the minimum lot and `corr(size, 1/ATR)` falls to 0.00.

```bash
# reproduce the whole ladder
python bt_xau_msvsd.py --tag k10_r010 --capital 10000               # zero trades
python bt_xau_msvsd.py --tag k10_r025 --capital 10000 --risk 0.25   # scaling dead
python bt_xau_msvsd.py --tag k10_fine --capital 10000 --lot-step 0.001

# MT5 side
python run_mt5_msvsd.py --deposit 10000
```

To run $10,000 faithfully you need finer granularity, not more risk: a 0.001
lot step, or a 10 oz micro gold contract. Either reproduces the $100,000
baseline exactly. If you use a micro contract, scale `swap_long_flat`,
`swap_short_flat` and `commission_per_lot_rt` by `contract_oz / 100` — those
are quoted per standard lot.
