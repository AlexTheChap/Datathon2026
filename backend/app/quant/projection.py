"""Monte Carlo projection of a portfolio's value: a RANGE of possible outcomes under the FP Canada / Institute
of Financial Planning 2026 Projection Assumption Guidelines (PAG). Not a forecast of what markets will do.

Model (monthly steps, multivariate lognormal, documented choices):
- Expected returns: ONLY the rates of backend/app/core/projection_assumptions.yaml, per PAG category (team
  assumptions for gold and crypto, labelled as such). Never estimated from past prices. Individual stocks get
  their region's equity rate (the PAG are asset-class rates) but their own volatility.
- Drift: PAG rates are long-term compound (geometric) annual returns. The monthly LOG drift of asset i is
  m_i = ln(1 + g_i − fee) / 12 and the monthly log-return shock is N(0, Σ_month) with zero mean, so the MEDIAN
  path of a single asset compounds exactly at g_i − fee (the mean ends higher, by the volatility drag).
- Volatility: "garch" = forecast_vol of vol_forecast.csv (SageMaker GARCH job) for year 1, moving linearly to
  long_run_vol_ann by the end of year 2, constant afterwards; "historical" = annualized volatility of the
  daily returns up to as_of (= asset_risk_metrics.csv ann_vol when as_of is at or after its window end);
  "auto" = garch if vol_forecast.csv exists with as_of <= the requested as_of and is at most
  max_forecast_age_days old, else historical. The result always says which one was used and why.
- Correlations: correlation_full.csv restricted to the portfolio (recomputed from returns <= as_of when as_of is
  before the file's window end), repaired to the nearest positive semi-definite matrix by eigenvalue clipping
  if needed (flagged in the output).
- Rebalanced monthly to the target weights (keeps the model simple and honest). A monthly contribution is
  added at the end of each month. Fees are taken from the drift.
- numpy.random.default_rng(seed): same inputs + seed = same output. Only percentiles leave this module,
  never raw paths.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from app.quant.assumptions import (
    Category,
    UnmappedTickerError,
    category_of,
    load_assumptions,
)
from app.quant.garch import TRADING_DAYS

PERCENTILES = (5, 10, 25, 50, 75, 90, 95)
MAX_PATHS = 20_000
LONG_TERM_YEARS = 10
VolSource = Literal["auto", "garch", "historical"]
DEFAULT_CURATED = Path(__file__).resolve().parents[3] / "data" / "curated"


@dataclass
class MarketInputs:
    """Everything the projection reads from the curated folder (injectable in tests)."""

    universe: pd.DataFrame                       # index ticker; asset_class, market, name
    returns: pd.DataFrame                        # daily simple returns, DatetimeIndex
    metrics_vol: pd.Series                       # asset_risk_metrics ann_vol
    metrics_end: pd.Timestamp | None             # end of the asset_risk_metrics / correlation_full window
    corr: pd.DataFrame                           # correlation_full
    vol_forecast: pd.DataFrame | None = None     # vol_forecast.csv (GARCH job), may be absent
    manifest: dict = field(default_factory=dict)  # manifest.json of the job, may be empty


def curated_dir_default() -> Path:
    return Path(os.environ.get("AF_CURATED_DIR") or DEFAULT_CURATED)


def load_inputs(curated_dir: str | Path | None = None) -> MarketInputs:
    from app.tools import curated_store as cs

    d = Path(curated_dir or curated_dir_default())
    uni = cs.read_extended(d, "assets_universe").set_index("ticker")   # + S&P 500 extension when built
    rets = cs.read_extended(d, "returns_daily_cad")
    m = cs.read(d, "asset_risk_metrics")
    if "end" in m:                       # several windows per ticker (full period, 2022): keep the full one
        m = m.sort_values(["ticker", "end", "n_obs"]).groupby("ticker").tail(1)
    m = m.set_index("ticker")
    end = pd.Timestamp(m["end"].min()) if "end" in m else None
    corr = cs.read(d, "correlation_full")
    if "ticker" in corr.columns:
        corr = corr.set_index("ticker")
    vf = cs.read(d, "vol_forecast") if cs.exists(d, "vol_forecast") else None
    man_path = d / "garch_manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    return MarketInputs(uni, rets, m["ann_vol"].astype(float), end, corr.astype(float), vf, manifest)


_CACHE: dict[tuple, MarketInputs] = {}


def cached_inputs(curated_dir: str | Path | None = None) -> MarketInputs:
    """load_inputs, reloaded only when vol_forecast.csv / garch_manifest.json change (a new GARCH run)."""
    d = Path(curated_dir or curated_dir_default())
    stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0
                  for p in (d / "vol_forecast.csv", d / "vol_forecast.parquet", d / "garch_manifest.json"))
    key = (str(d), stamp)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = load_inputs(d)
    return _CACHE[key]


# ---------- building blocks ----------

def nearest_psd(c: np.ndarray, eps: float = 0.0) -> tuple[np.ndarray, bool]:
    """Symmetrize, clip negative eigenvalues to eps, rescale to a unit diagonal. Returns (matrix, repaired)."""
    c = (np.asarray(c, dtype=float) + np.asarray(c, dtype=float).T) / 2.0
    vals, vecs = np.linalg.eigh(c)
    if vals.min() >= -1e-10:
        return c, False
    fixed = (vecs * np.clip(vals, eps, None)) @ vecs.T
    d = np.sqrt(np.clip(np.diag(fixed), 1e-12, None))
    fixed = fixed / np.outer(d, d)
    np.fill_diagonal(fixed, 1.0)
    return fixed, True


def _factor(c: np.ndarray) -> np.ndarray:
    """L with L @ L.T = c for a PSD c (eigen form: works when c is singular, unlike Cholesky)."""
    vals, vecs = np.linalg.eigh(c)
    return vecs * np.sqrt(np.clip(vals, 0.0, None))


def _normalize(weights: dict[str, float]) -> tuple[dict[str, float], bool]:
    w = {t: float(v) for t, v in weights.items() if v and float(v) > 0}
    if not w:
        raise ValueError("weights must contain at least one positive weight")
    total = sum(w.values())
    return {t: v / total for t, v in w.items()}, abs(total - 1.0) > 1e-6


def _hist_vols(inp: MarketInputs, tickers: list[str], as_of: pd.Timestamp) -> tuple[np.ndarray, str]:
    if inp.metrics_end is not None and as_of >= inp.metrics_end and all(t in inp.metrics_vol for t in tickers):
        return inp.metrics_vol.reindex(tickers).to_numpy(float), "asset_risk_metrics.csv ann_vol"
    r = inp.returns.loc[inp.returns.index <= as_of, tickers]
    return (r.std(ddof=1) * math.sqrt(TRADING_DAYS)).to_numpy(float), "daily returns up to as_of (std × sqrt(252))"


def _corr(inp: MarketInputs, tickers: list[str], as_of: pd.Timestamp) -> tuple[np.ndarray, str]:
    if inp.metrics_end is not None and as_of >= inp.metrics_end and all(t in inp.corr.index for t in tickers):
        return inp.corr.loc[tickers, tickers].to_numpy(float), "correlation_full.csv"
    r = inp.returns.loc[inp.returns.index <= as_of, tickers]
    return r.corr().to_numpy(float), "daily returns up to as_of"


def garch_status(inp: MarketInputs, as_of: pd.Timestamp, max_age_days: int) -> tuple[bool, str]:
    """Is vol_forecast.csv usable for this as_of? (usable, plain reason)"""
    vf = inp.vol_forecast
    if vf is None or vf.empty:
        return False, "no GARCH forecast file (vol_forecast.csv)"
    f_as_of = pd.Timestamp(vf["as_of"].max())
    if f_as_of > as_of:
        return False, f"the GARCH forecast is dated {f_as_of.date()}, after the requested as_of {as_of.date()}"
    age = (as_of - f_as_of).days
    if age > max_age_days:
        return False, f"the GARCH forecast of {f_as_of.date()} is {age} days old (limit {max_age_days})"
    return True, f"GARCH forecast of {f_as_of.date()} ({age} days before as_of)"


def _vol_paths(inp, tickers, as_of, hist, months, max_age, vol_source) -> tuple[np.ndarray, dict, list[str]]:
    """Annualized vol per (month, asset), the source actually used, and warnings."""
    warns: list[str] = []
    usable, why = garch_status(inp, as_of, max_age)
    if vol_source == "historical" or (vol_source == "auto" and not usable):
        info = {"used": "historical", "reason": ("requested" if vol_source == "historical" else why)}
        if vol_source == "auto":
            warns.append("Volatility comes from each holding's price history: no recent forecast was available.")
        return np.tile(hist, (months, 1)), info, warns
    if vol_source == "garch" and not usable:
        raise ValueError(f"GARCH volatility requested but not usable: {why}")
    vf = inp.vol_forecast.set_index("ticker")
    start, long_run, per_ticker = hist.copy(), hist.copy(), {}
    for i, t in enumerate(tickers):
        if t not in vf.index or not np.isfinite(float(vf.at[t, "forecast_vol"] or np.nan)):
            warns.append(f"{t}: no forecast available, its past volatility is used.")
            per_ticker[t] = "historical"
            continue
        start[i] = float(vf.at[t, "forecast_vol"])
        lr = vf.at[t, "long_run_vol_ann"] if "long_run_vol_ann" in vf.columns else np.nan
        long_run[i] = float(lr) if lr is not None and np.isfinite(float(lr)) else start[i]
        per_ticker[t] = str(vf.at[t, "model"])
    m = np.arange(1, months + 1)
    frac = np.clip((m - 12) / 12.0, 0.0, 1.0)[:, None]           # 0 in year 1 -> 1 at the end of year 2
    vols = start[None, :] + frac * (long_run - start)[None, :]
    info = {"used": "garch", "reason": why, "forecast_as_of": str(pd.Timestamp(inp.vol_forecast["as_of"].max()).date()),
            "job_name": inp.manifest.get("job_name") or (str(inp.vol_forecast["job_name"].iloc[0])
                                                        if "job_name" in inp.vol_forecast else None),
            "model_by_ticker": per_ticker,
            "schedule": "forecast_vol (21-day GARCH) in year 1, linear to long_run_vol_ann by the end of year 2"}
    return vols, info, warns


def drawdown_threshold(risk_band: str | None, rules_path: str | Path | None = None) -> float | None:
    """Max drawdown limit of the risk band in rules.yaml (risk_bands.<band>.max_drawdown), or None.
    rules.yaml defines none today: the field is then omitted, never invented."""
    if not risk_band:
        return None
    import yaml

    p = Path(rules_path or Path(__file__).resolve().parents[1] / "core" / "rules.yaml")
    rules = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    v = (rules.get("risk_bands") or {}).get(risk_band, {}).get("max_drawdown")
    return float(v) if v is not None else None


def _pcts(x: np.ndarray) -> dict[str, float]:
    q = np.percentile(x, PERCENTILES)
    return {f"p{p}": round(float(v), 2) for p, v in zip(PERCENTILES, q)}


# ---------- the projection ----------

def project(weights: dict[str, float], start_value: float, horizon_years: int, as_of: str,
            vol_source: VolSource = "auto", n_paths: int = 10_000, seed: int = 42,
            monthly_contribution: float = 0.0, annual_fee: float = 0.0, risk_band: str | None = None,
            inputs: MarketInputs | None = None, curated_dir: str | Path | None = None,
            assumptions: dict | None = None) -> dict:
    """See the module docstring. Raises ValueError on bad inputs, UnmappedTickerError / KeyError on tickers."""
    if not (start_value and start_value > 0):
        raise ValueError("start_value must be > 0")
    horizon_years = int(horizon_years)
    if not 1 <= horizon_years <= 50:
        raise ValueError("horizon_years must be between 1 and 50")
    if not 1 <= int(n_paths) <= MAX_PATHS:
        raise ValueError(f"n_paths must be between 1 and {MAX_PATHS}")
    if monthly_contribution < 0 or not 0 <= annual_fee < 0.2:
        raise ValueError("monthly_contribution must be >= 0 and annual_fee in [0, 0.2)")
    cfg = assumptions or load_assumptions()
    inp = inputs or cached_inputs(curated_dir)
    w, renormalized = _normalize(weights)
    tickers = sorted(w)
    unknown = [t for t in tickers if t not in inp.universe.index or t not in inp.returns.columns]
    if unknown:
        raise KeyError(f"unknown tickers: {unknown}")
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts < inp.returns.index.min():
        raise ValueError(f"as_of {as_of} is before the first return date")

    cats: dict[str, Category] = {t: category_of(t, inp.universe.at[t, "asset_class"], inp.universe.at[t, "market"], cfg)
                                 for t in tickers}
    g = np.array([cats[t].rate for t in tickers])
    wv = np.array([w[t] for t in tickers])
    months = horizon_years * 12
    hist, hist_src = _hist_vols(inp, tickers, as_of_ts)
    vols, vol_info, warns = _vol_paths(inp, tickers, as_of_ts, hist, months, int(cfg["max_forecast_age_days"]),
                                       vol_source)
    corr, corr_src = _corr(inp, tickers, as_of_ts)
    corr = np.nan_to_num(corr, nan=0.0)          # a pair with no overlapping data: treated as uncorrelated
    np.fill_diagonal(corr, 1.0)
    corr, repaired = nearest_psd(corr)
    L = _factor(corr)

    drift = np.log1p(g - annual_fee) / 12.0                       # median compounds at g − fee
    sig_m = vols / math.sqrt(12.0)                               # (months, assets)
    rng = np.random.default_rng(seed)
    n = int(n_paths)
    value = np.full(n, float(start_value))
    peak = value.copy()
    worst_dd = np.zeros(n)
    yearly = [value.copy()]
    for t in range(months):
        z = rng.standard_normal((n, len(tickers))) @ L.T
        gross = np.exp(drift[None, :] + sig_m[t][None, :] * z) @ wv  # rebalanced to the target weights
        value = value * gross + monthly_contribution
        peak = np.maximum(peak, value)
        worst_dd = np.minimum(worst_dd, value / peak - 1.0)
        if (t + 1) % 12 == 0:
            yearly.append(value.copy())

    infl = float(cfg["inflation"])
    invested = float(start_value) + monthly_contribution * months
    by_year = []
    for y, v in enumerate(yearly):
        by_year.append({"year": y, "nominal": _pcts(v), "real": _pcts(v / (1 + infl) ** y)})
    final_real = value / (1 + infl) ** horizon_years
    final = {"nominal": _pcts(value), "real": _pcts(final_real),
             "prob_below_start_nominal": round(float((value < start_value).mean()), 4),
             "prob_below_start_real": round(float((final_real < start_value).mean()), 4)}
    if monthly_contribution > 0:
        final["prob_below_contributions"] = round(float((value < invested).mean()), 4)
    dd = {"p50": round(float(np.percentile(worst_dd, 50)), 4), "p90": round(float(np.percentile(worst_dd, 10)), 4),
          "note": "Maximum peak-to-trough fall of the monthly value along each path; p90 = the fall exceeded in "
                  "only 10% of paths. Contributions are included in the value."}
    limit = drawdown_threshold(risk_band)
    if limit is not None:
        dd["threshold"] = -abs(limit)
        dd["prob_drawdown_worse_than"] = round(float((worst_dd < -abs(limit)).mean()), 4)

    if horizon_years < LONG_TERM_YEARS:
        warns.append(f"The horizon is {horizon_years} years. The planning guidelines are meant for projections of "
                     f"{LONG_TERM_YEARS} years or more, so over a shorter period the real range can be wider or "
                     "shifted.")
    team = sorted(t for t in tickers if cats[t].team_assumption)
    if team:
        warns.append(f"No official planning-guideline category for {', '.join(team)}: their expected return is "
                     "set equal to inflation; their volatility comes from their price history.")
    if renormalized:
        warns.append("The weights did not add up to 100% and were scaled to 100%.")
    if repaired:
        warns.append("How the holdings move together was adjusted slightly so the simulation stays consistent.")

    per_ticker = []
    for i, t in enumerate(tickers):
        stock = inp.universe.at[t, "asset_class"] == "equity"
        per_ticker.append({"ticker": t, "weight": round(w[t], 6), "category": cats[t].name,
                           "rate": cats[t].rate, "team_assumption": cats[t].team_assumption,
                           "vol_start": round(float(vols[0, i]), 6), "vol_end": round(float(vols[-1, i]), 6),
                           **({"note": "Individual stock: its region's equity rate (the guidelines are asset-class "
                                       "rates), its own volatility."} if stock else {})})
    return {
        "as_of": str(as_of_ts.date()), "horizon_years": horizon_years, "start_value": float(start_value),
        "monthly_contribution": float(monthly_contribution), "total_contributed": invested,
        "headline": {"p10": final["nominal"]["p10"], "p90": final["nominal"]["p90"],
                     "label": "P10–P90 range of the simulated value at the horizon (8 in 10 scenarios)"},
        "percentiles_by_year": by_year, "final": final, "drawdown": dd,
        "assumptions": {
            "per_ticker": per_ticker, "inflation": infl, "annual_fee": float(annual_fee),
            "vol_source": vol_info, "historical_vol_from": hist_src, "correlation_from": corr_src,
            "correlation_repaired": bool(repaired), "n_paths": n, "seed": int(seed), "as_of": str(as_of_ts.date()),
            "rebalancing": "monthly, to the target weights", "steps": "monthly",
            "drift": "monthly log drift ln(1 + rate − fee)/12: the median path compounds at the rate",
            "source": cfg["source"], "source_url": cfg["source_url"], "horizon_note": cfg.get("horizon_note"),
        },
        "warnings": warns,
        "disclaimer": "Simulation under official planning assumptions. Not a forecast, not advice.",
    }


__all__ = [
    "MAX_PATHS",
    "PERCENTILES",
    "MarketInputs",
    "UnmappedTickerError",
    "garch_status",
    "load_inputs",
    "nearest_psd",
    "project",
]
