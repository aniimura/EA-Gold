# -*- coding: utf-8 -*-
"""Robust statistics.

The ordinary t-test over sleeve trades is kept, and kept LABELLED SECONDARY,
because it is wrong in two ways at once: sleeve trades overlap in time (three
sleeves ride the same move, so they are not independent draws) and the return
distribution has a long right tail (one 21R trade in 304). Both violations
push the same way - they make the edge look more certain than the evidence
supports.

The primary evidence is the campaign bootstrap and the block bootstraps over
daily returns, which resample contiguous chunks and so preserve the
autocorrelation the naive test throws away.

Nothing in this module decides whether an edge exists. A confidence interval
that excludes zero over one instrument in one regime is not a durable edge; it
is one sample.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329
BARS_PER_YEAR_H4 = 6 * 252.0
DAYS_PER_YEAR = 252.0


# --------------------------------------------------------------------------
def _ci(samples: np.ndarray, level: float) -> List[float]:
    a = (1.0 - level) / 2.0
    return [float(np.percentile(samples, 100 * a)),
            float(np.percentile(samples, 100 * (1 - a)))]


def _summarise(samples: np.ndarray, point: float, n_eff: int, method: str,
               units: str, assumptions: str) -> Dict:
    return {
        "method": method,
        "units": units,
        "point_estimate": float(point),
        "bootstrap_mean": float(np.mean(samples)),
        "standard_error": float(np.std(samples, ddof=1)),
        "ci90": _ci(samples, 0.90),
        "ci95": _ci(samples, 0.95),
        "ci99": _ci(samples, 0.99),
        "p_mean_le_zero": float(np.mean(samples <= 0.0)),
        "effective_observations": int(n_eff),
        "resamples": int(len(samples)),
        "assumptions": assumptions,
    }


def iid_bootstrap(values: np.ndarray, n_boot: int, seed: int,
                  units: str = "R per sleeve trade") -> Optional[Dict]:
    v = np.asarray([x for x in values if np.isfinite(x)], float)
    if len(v) < 5:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), size=(n_boot, len(v)))
    means = v[idx].mean(axis=1)
    return _summarise(
        means, v.mean(), len(v), "iid_sleeve_trade_bootstrap", units,
        "SECONDARY / FOR COMPARISON ONLY. Assumes sleeve trades are independent "
        "draws. They are not: up to three sleeves hold the same directional move "
        "at once, so this interval is too narrow and the p-value too small.")


def campaign_bootstrap(values: np.ndarray, n_boot: int, seed: int,
                       units: str = "net R per campaign") -> Optional[Dict]:
    v = np.asarray([x for x in values if np.isfinite(x)], float)
    if len(v) < 5:
        return None
    rng = np.random.default_rng(seed + 1)
    idx = rng.integers(0, len(v), size=(n_boot, len(v)))
    means = v[idx].mean(axis=1)
    return _summarise(
        means, v.mean(), len(v), "campaign_bootstrap", units,
        "PRIMARY. One campaign is one bet: it opens when the first sleeve leaves "
        "flat and closes when all are flat again, so overlapping sleeve trades "
        "collapse into a single observation. Campaigns are still not strictly "
        "independent - consecutive trends in one instrument share a regime - but "
        "they are far closer to it than sleeve trades.")


def block_bootstrap(daily_ret: pd.Series, n_boot: int, seed: int,
                    freq: str = "M") -> Optional[Dict]:
    """Resample contiguous calendar blocks of daily returns, with replacement."""
    r = daily_ret.dropna()
    if len(r) < 60:
        return None
    keys = (r.index.to_period("M") if freq == "M" else r.index.to_period("Q"))
    blocks = [g.to_numpy() for _, g in r.groupby(keys)]
    blocks = [b for b in blocks if len(b)]
    if len(blocks) < 8:
        return None
    rng = np.random.default_rng(seed + (2 if freq == "M" else 3))
    nb = len(blocks)
    means = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.integers(0, nb, size=nb)
        means[i] = np.concatenate([blocks[j] for j in pick]).mean()
    name = "monthly" if freq == "M" else "quarterly"
    return _summarise(
        means, float(r.mean()), nb, "%s_block_bootstrap" % name,
        "mean daily return",
        "PRIMARY. Resamples whole %s blocks so serial dependence inside a block "
        "is preserved. The effective sample is the number of blocks (%d), not "
        "the number of days (%d) - which is the honest count and the reason the "
        "interval is wide." % (name, nb, len(r)))


# --------------------------------------------------------------------------
def lo_adjusted_sharpe(daily_ret: pd.Series, q: float = DAYS_PER_YEAR,
                       max_lag: int = 20) -> Dict:
    """Annualised Sharpe with Lo (2002) autocorrelation correction.

    Naive annualisation multiplies by sqrt(q) and assumes returns are serially
    independent. A trend follower's returns are not - they persist - so the
    naive figure overstates. The correction divides by the autocorrelation
    factor computed from the first `max_lag` lags.
    """
    r = daily_ret.dropna().to_numpy(float)
    out = {"n_days": int(len(r))}
    if len(r) < 30 or r.std(ddof=1) == 0:
        out.update(sharpe_naive=np.nan, sharpe_lo_adjusted=np.nan,
                   autocorr_factor=np.nan)
        return out
    sr_p = r.mean() / r.std(ddof=1)
    naive = sr_p * math.sqrt(q)
    lag = int(min(max_lag, len(r) // 4))
    rho = [float(pd.Series(r).autocorr(lag=k)) for k in range(1, lag + 1)]
    rho = [x if np.isfinite(x) else 0.0 for x in rho]
    denom = q + 2.0 * sum((q - k) * rho[k - 1] for k in range(1, lag + 1))
    factor = math.sqrt(q) / math.sqrt(denom) if denom > 0 else np.nan
    out.update(sharpe_period=float(sr_p), sharpe_naive=float(naive),
               autocorr_factor=float(factor) if np.isfinite(factor) else np.nan,
               sharpe_lo_adjusted=float(sr_p * math.sqrt(q) * factor)
               if np.isfinite(factor) else np.nan,
               autocorr_lags=lag, first_five_rho=[round(x, 4) for x in rho[:5]])
    return out


def deflated_sharpe(sr_period: float, n_obs: int, skew: float, kurt: float,
                    n_trials: int, sr_trial_std_period: Optional[float]) -> Dict:
    """Bailey & Lopez de Prado Deflated Sharpe Ratio.

    Answers: given that `n_trials` configurations were examined, how likely is a
    Sharpe this high under the null of no skill? Requires the dispersion of the
    trial Sharpes, which only the experiment runner has - a single run reports
    `insufficient_inputs` rather than a fabricated number.

    UNITS MATTER, and getting them wrong makes this estimator useless rather
    than merely imprecise: `sr_period` and `sr_trial_std_period` must both be
    PER-OBSERVATION Sharpes matching `n_obs` (daily here), never annualised.
    Feeding an annual Sharpe alongside a daily observation count inflates z
    by sqrt(252) and pins the answer at 1.0.
    """
    sr_annual = sr_period
    sr_trial_std = sr_trial_std_period
    if n_trials < 2 or sr_trial_std is None or not np.isfinite(sr_trial_std) \
            or sr_trial_std <= 0 or n_obs < 30 or not np.isfinite(sr_annual):
        return {"deflated_sharpe": None,
                "reason": "insufficient_inputs: needs >= 2 trials, a positive "
                          "spread of trial Sharpes and >= 30 observations. Run "
                          "experiments.py, which supplies all three."}
    k = float(n_trials)
    e_max = sr_trial_std * ((1 - EULER_GAMMA) * sps.norm.ppf(1 - 1.0 / k)
                            + EULER_GAMMA * sps.norm.ppf(1 - 1.0 / (k * math.e)))
    denom = 1.0 - skew * sr_annual + (kurt - 1.0) / 4.0 * sr_annual ** 2
    if denom <= 0:
        return {"deflated_sharpe": None,
                "reason": "variance term non-positive; skew/kurtosis make the "
                          "estimator undefined here"}
    z = (sr_annual - e_max) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return {"deflated_sharpe": float(sps.norm.cdf(z)),
            "expected_max_sharpe_under_null_per_period": float(e_max),
            "observed_sharpe_per_period": float(sr_period),
            "n_observations": int(n_obs),
            "n_trials": int(n_trials),
            "sr_trial_std_per_period": float(sr_trial_std),
            "note": "Probability the true Sharpe exceeds zero AFTER deflating for "
                    "the number of configurations examined. Below 0.95 is not "
                    "evidence of skill."}


# --------------------------------------------------------------------------
def risk_metrics(equity: pd.Series, daily_ret: pd.Series, bars: pd.DataFrame,
                 diagnostics: Dict, capital: float) -> Dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {}
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    peak = eq.cummax()
    dd = (peak - eq) / peak
    r = daily_ret.dropna()
    downside = r[r < 0]
    net = float(eq.iloc[-1] - capital)
    cagr = ((eq.iloc[-1] / capital) ** (1.0 / years) - 1.0) * 100.0
    maxdd_pct = float(dd.max() * 100.0)
    ann_vol = float(r.std(ddof=1) * math.sqrt(DAYS_PER_YEAR) * 100.0) if len(r) > 2 else np.nan
    in_mkt = np.abs(np.nan_to_num(bars["position_oz"].to_numpy(float))) > 0
    mean_eq = float(eq.mean())
    return {
        "years": round(years, 4),
        "initial_capital": capital,
        "final_equity": float(eq.iloc[-1]),
        "net_profit": net,
        "return_pct": 100.0 * net / capital,
        "cagr_pct": float(cagr),
        "max_dd_pct": maxdd_pct,
        "max_dd_money": float((peak - eq).max()),
        "ann_vol_pct": ann_vol,
        "sortino": (float(r.mean() / downside.std(ddof=1) * math.sqrt(DAYS_PER_YEAR))
                    if len(downside) > 2 and downside.std(ddof=1) > 0 else np.nan),
        "calmar": float(cagr / maxdd_pct) if maxdd_pct > 0 else np.nan,
        "exposure_pct": float(100.0 * in_mkt.mean()),
        "turnover_notional": diagnostics.get("turnover_notional", np.nan),
        "turnover_x_equity_per_year": (
            float(diagnostics.get("turnover_notional", np.nan) / mean_eq / years)
            if mean_eq else np.nan),
        "skew_daily": float(sps.skew(r)) if len(r) > 8 else np.nan,
        "kurtosis_daily": float(sps.kurtosis(r, fisher=False)) if len(r) > 8 else np.nan,
    }


def profit_factor(values: np.ndarray) -> float:
    v = np.asarray([x for x in values if np.isfinite(x)], float)
    w, l = v[v > 0].sum(), abs(v[v < 0].sum())
    return float(w / l) if l > 0 else float("inf")


def t_test(values: np.ndarray) -> Dict:
    v = np.asarray([x for x in values if np.isfinite(x)], float)
    if len(v) < 3:
        return {}
    t, p = sps.ttest_1samp(v, 0.0)
    return {"method": "one_sample_t_test",
            "status": "SECONDARY - retained for reference only",
            "n": int(len(v)), "mean": float(v.mean()),
            "sd": float(v.std(ddof=1)), "t": float(t), "p_two_sided": float(p),
            "limitation": "Assumes independent, roughly symmetric observations. "
                          "Sleeve trades overlap and the distribution is strongly "
                          "right-skewed, so this p-value is optimistic. Use the "
                          "campaign and block bootstraps."}


# --------------------------------------------------------------------------
def full_report(trades: pd.DataFrame, camps: pd.DataFrame, equity: pd.Series,
                daily_ret: pd.Series, bars: pd.DataFrame, diagnostics: Dict,
                capital: float, n_boot: int, seed: int, n_trials: int = 1,
                sr_trial_std: Optional[float] = None) -> Dict:
    out: Dict = {"seed": seed, "resamples": n_boot}
    out["risk"] = risk_metrics(equity, daily_ret, bars, diagnostics, capital)

    if len(trades):
        R = trades["r_multiple"].to_numpy(float)
        out["trade_level"] = {
            "n": int(len(trades)),
            "mean_R": float(np.nanmean(R)),
            "median_R": float(np.nanmedian(R)),
            "win_rate_pct": float(100.0 * (trades["gross"] > 0).mean()),
            "profit_factor_gross": profit_factor(trades["gross"].to_numpy(float)),
            "t_test": t_test(R),
        }
        out["bootstrap_iid_trades"] = iid_bootstrap(R, n_boot, seed)
    if len(camps):
        cR = camps["net_R"].to_numpy(float)
        usd = camps["net_pnl"].to_numpy(float)
        risk = float(camps["risk_cash"].sum())
        out["campaign_level"] = {
            "n": int(len(camps)),
            "mean_net_R_equal_weighted": float(np.nanmean(cR)),
            "median_net_R": float(np.nanmedian(cR)),
            "risk_weighted_net_R": float(usd.sum() / risk) if risk > 0 else np.nan,
            "mean_net_usd": float(np.nanmean(usd)),
            "median_net_usd": float(np.nanmedian(usd)),
            "win_rate_pct": float(100.0 * (camps["net_pnl"] > 0).mean()),
            "profit_factor_net": profit_factor(usd),
            "median_days": float(camps["days"].median()),
            "size_effect_note":
                "The equal-weighted mean R and the risk-weighted R disagree in sign "
                "for this strategy, and that disagreement is a finding rather than an "
                "artefact. Position size is not constant by design: a campaign in "
                "which all three sleeves align risks roughly three times what a "
                "single-sleeve campaign risks. Winners are the big-risk campaigns and "
                "losers the small ones, so equal-weighting understates the money and "
                "risk-weighting understates how often the strategy is wrong. Read "
                "both, and treat the USD bootstrap as the test of whether the average "
                "campaign makes money.",
        }
        out["bootstrap_campaigns"] = campaign_bootstrap(cR, n_boot, seed)
        bu = campaign_bootstrap(usd, n_boot, seed + 7, units="net USD per campaign")
        if bu:
            bu["method"] = "campaign_bootstrap_usd"
            bu["assumptions"] = (
                "PRIMARY. Resamples campaign net P&L in dollars, so a campaign that "
                "risked three sleeves counts for what it actually made rather than "
                "being normalised away. This is the test of whether the average "
                "campaign is profitable after every modelled cost.")
        out["bootstrap_campaigns_usd"] = bu
    out["bootstrap_block_monthly"] = block_bootstrap(daily_ret, n_boot, seed, "M")
    out["bootstrap_block_quarterly"] = block_bootstrap(daily_ret, n_boot, seed, "Q")

    lo = lo_adjusted_sharpe(daily_ret)
    out["sharpe"] = lo
    # per-observation units on both sides; see deflated_sharpe's docstring
    out["deflated_sharpe"] = deflated_sharpe(
        lo.get("sharpe_period", np.nan), lo.get("n_days", 0),
        out["risk"].get("skew_daily", 0.0) or 0.0,
        out["risk"].get("kurtosis_daily", 3.0) or 3.0,
        n_trials, sr_trial_std)

    from .campaigns import concentration
    if len(trades):
        out["concentration_trades"] = concentration(
            trades["gross"].to_numpy(float), "trade_gross")
    if len(camps):
        out["concentration_campaigns"] = concentration(
            camps["net_pnl"].to_numpy(float), "campaign_net")
    out["interpretation_warning"] = (
        "A confidence interval that excludes zero is not proof of a durable edge. "
        "This is one instrument, one broker cost model and one regime. Treat every "
        "figure here as a description of this sample, not a forecast.")
    return out
