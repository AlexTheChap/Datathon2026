"""GARCH(1,1) volatility forecasts per ticker. Forecasts RISK (volatility), never returns.

Model (per ticker, daily simple returns in CAD):
    arch_model(r * 100, mean="Constant", vol="GARCH", p=1, q=1, dist="t").fit(disp="off")
Returns are scaled to percent for the optimiser and the results scaled back (variances / 1e4).

Point-in-time: only observations dated <= as_of are used. Nothing after as_of reaches a fit; the realized
volatility used by `backtest` is computed in a separate, clearly marked evaluation step.

Horizons: for h trading days, `vol_h{h}` = sqrt(mean of the h forecast daily variances × 252), the annualized
average volatility over the next h days. `forecast_vol` = vol_h21 (the column the holding risk level reads).
`long_run_vol_ann` = sqrt(omega / (1 − alpha − beta) × 252) when alpha + beta < 1.

252 trading days a year for every asset, crypto included: the curated returns sit on one common trading
calendar (data/curated/calendar.csv), so a crypto "day" here is a calendar trading day, not a 24/7 day.

Fallback (model = "ewma", fallback_reason set): fewer than MIN_OBS observations, a constant series, the fit
raises or does not converge, persistence alpha + beta >= MAX_PERSISTENCE, or a NaN parameter. EWMA uses the
lambda of projection_assumptions.yaml (ewma_lambda) and gives a flat forecast across horizons.
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MIN_OBS = 500
MAX_PERSISTENCE = 0.999
HORIZONS: tuple[int, ...] = (1, 21, 63, 252)
FORECAST_HORIZON = 21          # forecast_vol = vol_h21
HIST_WINDOW = 63

COLUMNS = ["ticker", "as_of", "model", "forecast_vol", "vol_h1", "vol_h21", "vol_h63", "vol_h252",
           "long_run_vol_ann", "hist_vol_63d_ann", "omega", "alpha", "beta", "nu", "persistence", "n_obs",
           "fallback_reason"]


def _lambda(ewma_lambda: float | None) -> float:
    if ewma_lambda is not None:
        return float(ewma_lambda)
    from app.quant.assumptions import load_assumptions

    return float(load_assumptions()["ewma_lambda"])


def point_in_time(returns: pd.Series, as_of: str | pd.Timestamp) -> pd.Series:
    """Observations dated <= as_of, NaN dropped, float."""
    r = returns.dropna().astype(float)
    return r[r.index <= pd.Timestamp(as_of)]


def hist_vol(r: pd.Series, window: int = HIST_WINDOW) -> float:
    """Std of the last `window` returns × sqrt(252) (NaN if fewer than 2)."""
    tail = r.tail(window)
    return float(tail.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(tail) >= 2 else float("nan")


def ewma_vol(r: pd.Series, lam: float) -> float:
    """RiskMetrics EWMA of squared (zero-mean) returns, seeded with the sample variance; annualized."""
    x = r.to_numpy(dtype=float)
    if len(x) < 2:
        return float("nan")
    var = float(np.var(x))
    for v in x:
        var = lam * var + (1.0 - lam) * v * v
    return math.sqrt(var * TRADING_DAYS)


def _result(ticker, as_of, n_obs, hist, horizons) -> dict:
    out = {c: None for c in COLUMNS}
    out.update(ticker=ticker, as_of=str(pd.Timestamp(as_of).date()), n_obs=int(n_obs), hist_vol_63d_ann=hist)
    for h in horizons:
        out[f"vol_h{h}"] = None
    return out


def _ewma(out: dict, r: pd.Series, horizons, lam: float, reason: str) -> dict:
    v = ewma_vol(r, lam)
    out.update(model="ewma", fallback_reason=reason, omega=None, alpha=None, beta=None, nu=None,
               persistence=None, long_run_vol_ann=None)
    for h in horizons:
        out[f"vol_h{h}"] = v
    out["forecast_vol"] = v
    return out


def fit_forecast(returns: pd.Series, as_of: str, horizons: Sequence[int] = HORIZONS,
                 ewma_lambda: float | None = None) -> dict:
    """GARCH(1,1)-t forecast for one ticker at as_of (see the module docstring). Never raises on bad data."""
    horizons = tuple(sorted({int(h) for h in horizons} | {FORECAST_HORIZON}))
    lam = _lambda(ewma_lambda)
    r = point_in_time(returns, as_of)
    out = _result(returns.name, as_of, len(r), hist_vol(r), horizons)
    if len(r) < MIN_OBS:
        return _ewma(out, r, horizons, lam, f"only {len(r)} observations up to as_of (need {MIN_OBS})")
    if float(r.max() - r.min()) < 1e-12:
        return _ewma(out, r, horizons, lam, "constant series")
    try:
        from arch import arch_model

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = arch_model(r * 100.0, mean="Constant", vol="GARCH", p=1, q=1, dist="t").fit(
                disp="off", show_warning=False)
        if res.convergence_flag != 0:
            return _ewma(out, r, horizons, lam, f"fit did not converge (flag {res.convergence_flag})")
        p = res.params
        omega, alpha, beta, nu = (float(p["omega"]) / 1e4, float(p["alpha[1]"]), float(p["beta[1]"]),
                                  float(p["nu"]))
        persistence = alpha + beta
        if any(math.isnan(x) for x in (omega, alpha, beta, nu)):
            return _ewma(out, r, horizons, lam, "a fitted parameter is NaN")
        if persistence >= MAX_PERSISTENCE:
            return _ewma(out, r, horizons, lam, f"persistence {persistence:.4f} >= {MAX_PERSISTENCE}")
        daily_var = np.asarray(res.forecast(horizon=max(horizons), reindex=False).variance.iloc[-1]) / 1e4
        if not np.all(np.isfinite(daily_var)):
            return _ewma(out, r, horizons, lam, "forecast variance is not finite")
    except Exception as e:  # noqa: BLE001 - one bad ticker must never stop the batch
        return _ewma(out, r, horizons, lam, f"fit failed: {type(e).__name__}: {e}"[:200])
    for h in horizons:
        out[f"vol_h{h}"] = math.sqrt(float(daily_var[:h].mean()) * TRADING_DAYS)
    out.update(model="garch", fallback_reason=None, omega=omega, alpha=alpha, beta=beta, nu=nu,
               persistence=persistence, forecast_vol=out[f"vol_h{FORECAST_HORIZON}"],
               long_run_vol_ann=math.sqrt(omega / (1.0 - persistence) * TRADING_DAYS))
    return out


def fit_all(returns_df: pd.DataFrame, as_of: str, horizons: Sequence[int] = HORIZONS,
            ewma_lambda: float | None = None, tickers: Iterable[str] | None = None) -> pd.DataFrame:
    """One row per ticker (COLUMNS order). A ticker that breaks anything gets an EWMA row (or an error row)."""
    lam = _lambda(ewma_lambda)
    rows = []
    for t in tickers or returns_df.columns:
        try:
            rows.append(fit_forecast(returns_df[t].rename(t), as_of, horizons, lam))
        except Exception as e:  # noqa: BLE001 - e.g. a non-numeric column
            row = {c: None for c in COLUMNS}
            row.update(ticker=t, as_of=str(pd.Timestamp(as_of).date()), model="error", n_obs=0,
                       fallback_reason=f"{type(e).__name__}: {e}"[:200])
            rows.append(row)
    cols = COLUMNS + [f"vol_h{h}" for h in sorted(set(horizons)) if f"vol_h{h}" not in COLUMNS]
    return pd.DataFrame(rows).reindex(columns=cols)


# ---------- evaluation only (uses data AFTER each as_of, never feeds a forecast) ----------

def monthly_as_of_grid(index: pd.DatetimeIndex, start: str = "2023-01", horizon: int = FORECAST_HORIZON
                       ) -> list[pd.Timestamp]:
    """Last trading day of each month from `start`, keeping only dates with >= horizon observations after."""
    idx = pd.DatetimeIndex(index).sort_values()
    last_ok = idx[-horizon - 1] if len(idx) > horizon else idx[0] - pd.Timedelta(days=1)
    s = pd.Series(idx, index=idx)
    month_ends = s.groupby(idx.to_period("M")).max()
    return [d for d in month_ends if d >= pd.Timestamp(start) and d <= last_ok]


def realized_vol(r: pd.Series, as_of: pd.Timestamp, horizon: int) -> float:
    """EVALUATION ONLY: annualized root mean square of the `horizon` returns strictly after as_of."""
    after = r.dropna()
    after = after[after.index > as_of].head(horizon)
    if len(after) < horizon:
        return float("nan")
    return math.sqrt(float((after.to_numpy() ** 2).mean()) * TRADING_DAYS)


def _qlike(realized_var: np.ndarray, forecast_var: np.ndarray) -> float:
    """Patton (2011) QLIKE: mean(RV/F − ln(RV/F) − 1); 0 = perfect, robust to noise in the realized proxy."""
    ratio = realized_var / forecast_var
    return float(np.mean(ratio - np.log(ratio) - 1.0))


def backtest(returns_df: pd.DataFrame, as_of_dates: Sequence | None = None, horizon: int = FORECAST_HORIZON,
             ewma_lambda: float | None = None) -> pd.DataFrame:
    """Per ticker: the GARCH h-day forecast (EWMA when the fit is rejected, counted in n_fallback) and the
    trailing 63-day historical volatility, each compared with the realized volatility of the next h days.
    RMSE on annualized volatility; QLIKE on variance. garch_better = lower QLIKE (ties: lower RMSE)."""
    lam = _lambda(ewma_lambda)
    dates = list(as_of_dates) if as_of_dates is not None else monthly_as_of_grid(returns_df.index, horizon=horizon)
    rows = []
    for t in returns_df.columns:
        s = returns_df[t].rename(t)
        f, hst, real, n_fb = [], [], [], 0
        for d in dates:
            d = pd.Timestamp(d)
            rv = realized_vol(s, d, horizon)
            if not np.isfinite(rv) or rv <= 0:
                continue
            fc = fit_forecast(s, str(d.date()), (horizon,), lam)
            g, hv = fc[f"vol_h{horizon}"], fc["hist_vol_63d_ann"]
            if g is None or hv is None or not (np.isfinite(g) and np.isfinite(hv)) or g <= 0 or hv <= 0:
                continue
            n_fb += fc["model"] != "garch"
            f.append(g)
            hst.append(hv)
            real.append(rv)
        if not real:
            rows.append({"ticker": t, "n_dates": 0})
            continue
        f, hst, real = map(np.asarray, (f, hst, real))
        rmse_g = float(np.sqrt(np.mean((f - real) ** 2)))
        rmse_h = float(np.sqrt(np.mean((hst - real) ** 2)))
        q_g, q_h = _qlike(real ** 2, f ** 2), _qlike(real ** 2, hst ** 2)
        rows.append({"ticker": t, "horizon": horizon, "n_dates": len(real), "n_fallback": int(n_fb),
                     "first_as_of": str(pd.Timestamp(dates[0]).date()), "last_as_of": str(pd.Timestamp(dates[-1]).date()),
                     "rmse_garch": rmse_g, "rmse_hist": rmse_h, "qlike_garch": q_g, "qlike_hist": q_h,
                     "garch_better": bool(q_g < q_h or (q_g == q_h and rmse_g < rmse_h))})
    return pd.DataFrame(rows)


def backtest_summary(bt: pd.DataFrame) -> dict:
    ok = bt[bt.get("n_dates", 0) > 0] if len(bt) else bt
    return {"tickers": len(ok), "garch_better": int(ok["garch_better"].sum()) if len(ok) else 0,
            "garch_better_rmse": int((ok["rmse_garch"] < ok["rmse_hist"]).sum()) if len(ok) else 0,
            "horizon": int(ok["horizon"].iloc[0]) if len(ok) else FORECAST_HORIZON,
            "criterion": "QLIKE on the next-21-day realized variance (lower is better)"}
