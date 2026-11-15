"""app.quant.garch: parameter recovery, point-in-time, fallbacks, batch robustness, backtest shape."""
import math

import numpy as np
import pandas as pd
import pytest
from app.quant import garch

pytest.importorskip("arch")


def simulate_garch(n=3000, omega=0.05, alpha=0.08, beta=0.90, nu=8.0, seed=7) -> pd.Series:
    """Daily returns (decimal) from a GARCH(1,1)-t with parameters in percent² units."""
    rng = np.random.default_rng(seed)
    z = rng.standard_t(nu, n) * math.sqrt((nu - 2) / nu)        # unit variance
    var, r = omega / (1 - alpha - beta), np.empty(n)
    for t in range(n):
        r[t] = math.sqrt(var) * z[t]
        var = omega + alpha * r[t] ** 2 + beta * var
    idx = pd.bdate_range("2010-01-01", periods=n)
    return pd.Series(r / 100.0, index=idx, name="SIM")


def test_recovers_known_parameters():
    s = simulate_garch()
    out = garch.fit_forecast(s, str(s.index[-1].date()), ewma_lambda=0.94)
    assert out["model"] == "garch", out["fallback_reason"]
    assert out["alpha"] == pytest.approx(0.08, abs=0.04)
    assert out["beta"] == pytest.approx(0.90, abs=0.05)
    assert out["persistence"] == pytest.approx(0.98, abs=0.03)
    lr_true = math.sqrt(0.05 / (1 - 0.98) / 1e4 * 252)
    assert out["long_run_vol_ann"] == pytest.approx(lr_true, rel=0.35)
    assert out["forecast_vol"] == out["vol_h21"] and out["n_obs"] == 3000
    for h in (1, 21, 63, 252):
        assert out[f"vol_h{h}"] > 0


def test_no_look_ahead():
    s = simulate_garch()
    as_of = str(s.index[2000].date())
    base = garch.fit_forecast(s, as_of, ewma_lambda=0.94)
    changed = s.copy()
    changed.iloc[2001:] = changed.iloc[2001:] * 5 + 0.03       # wild data after as_of
    again = garch.fit_forecast(changed, as_of, ewma_lambda=0.94)
    for k in ("forecast_vol", "alpha", "beta", "omega", "hist_vol_63d_ann", "n_obs"):
        assert again[k] == pytest.approx(base[k], rel=1e-12), k


def test_short_series_falls_back_to_ewma():
    s = simulate_garch(n=300)
    out = garch.fit_forecast(s, str(s.index[-1].date()), ewma_lambda=0.94)
    assert out["model"] == "ewma" and "300 observations" in out["fallback_reason"]
    assert out["vol_h1"] == out["vol_h21"] == out["vol_h252"] == out["forecast_vol"] > 0   # flat
    assert out["alpha"] is None and out["long_run_vol_ann"] is None


def test_constant_series_falls_back():
    s = pd.Series(0.001, index=pd.bdate_range("2015-01-01", periods=800), name="FLAT")
    out = garch.fit_forecast(s, "2030-01-01", ewma_lambda=0.94)
    assert out["model"] == "ewma" and out["fallback_reason"] == "constant series"


def test_ewma_lambda_comes_from_config():
    from app.quant.assumptions import load_assumptions

    s = simulate_garch(n=300)
    a = garch.fit_forecast(s, str(s.index[-1].date()))
    b = garch.fit_forecast(s, str(s.index[-1].date()), ewma_lambda=load_assumptions()["ewma_lambda"])
    assert a["forecast_vol"] == b["forecast_vol"]


def test_fit_all_survives_a_broken_ticker():
    s = simulate_garch(n=700)
    df = pd.DataFrame({"GOOD": s, "BAD": ["x"] * len(s), "EMPTY": np.nan}, index=s.index)
    out = garch.fit_all(df, str(s.index[-1].date()), ewma_lambda=0.94)
    assert list(out["ticker"]) == ["GOOD", "BAD", "EMPTY"]
    assert out.set_index("ticker").at["GOOD", "model"] == "garch"
    assert set(out.set_index("ticker").loc[["BAD", "EMPTY"], "model"]) <= {"ewma", "error"}
    assert list(out.columns[:len(garch.COLUMNS)]) == garch.COLUMNS


def test_backtest_compares_against_realized_after_as_of():
    s = simulate_garch(n=1200)
    df = pd.DataFrame({"SIM": s})
    dates = garch.monthly_as_of_grid(df.index, start=str(s.index[600].date()))
    assert dates and all((df.index > d).sum() >= 21 for d in dates)
    bt = garch.backtest(df, dates[:6], horizon=21, ewma_lambda=0.94)
    row = bt.iloc[0]
    assert row["n_dates"] == 6 and row["n_fallback"] == 0
    for k in ("rmse_garch", "rmse_hist", "qlike_garch", "qlike_hist"):
        assert np.isfinite(row[k])
    assert isinstance(bool(row["garch_better"]), bool)
    summary = garch.backtest_summary(bt)
    assert summary["tickers"] == 1 and summary["horizon"] == 21
