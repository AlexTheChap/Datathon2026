"""app.quant.projection: reproducibility, deterministic limit, PAG median, contributions, PSD repair,
mapping errors, warnings, GARCH vol source selection."""
import math

import numpy as np
import pandas as pd
import pytest
from app.quant import projection as P
from app.quant.assumptions import UnmappedTickerError, load_assumptions

CFG = load_assumptions()


def inputs(vols=None, corr=0.3, forecast=None, tickers=("XIU.TO", "SPY")) -> P.MarketInputs:
    uni = pd.DataFrame({"ticker": ["XIU.TO", "SPY", "XBB.TO", "GLD", "ODD"],
                        "asset_class": ["etf_equity", "etf_equity", "etf_bond", "etf_commodity", "etf_commodity"],
                        "market": ["TSX", "NYSE Arca", "TSX", "NYSE Arca", "LSE"], "name": list("abcde")}
                       ).set_index("ticker")
    idx = pd.bdate_range("2021-11-15", periods=1281)
    rng = np.random.default_rng(0)
    rets = pd.DataFrame(rng.normal(0, 0.01, (len(idx), len(uni))), index=idx, columns=uni.index)
    vols = vols or {t: 0.15 for t in uni.index}
    m = np.full((len(uni), len(uni)), corr)
    np.fill_diagonal(m, 1.0)
    c = pd.DataFrame(m, index=uni.index, columns=uni.index)
    return P.MarketInputs(uni, rets, pd.Series(vols, dtype=float), idx[-1], c, forecast, {})


AS_OF = "2026-11-12"


def deterministic(weights, start, years, fee=0.0, contribution=0.0):
    rates = {"XIU.TO": CFG["rates"]["canadian_equities"], "SPY": CFG["rates"]["us_equities"],
             "XBB.TO": CFG["rates"]["fixed_income"]}
    gross = sum(w * (1 + rates[t] - fee) ** (1 / 12) for t, w in weights.items())
    v = start
    for _ in range(years * 12):
        v = v * gross + contribution
    return v


def test_same_seed_same_output():
    w = {"XIU.TO": 0.6, "SPY": 0.4}
    a = P.project(w, 100_000, 10, AS_OF, inputs=inputs(), n_paths=2000, seed=1)
    b = P.project(w, 100_000, 10, AS_OF, inputs=inputs(), n_paths=2000, seed=1)
    c = P.project(w, 100_000, 10, AS_OF, inputs=inputs(), n_paths=2000, seed=2)
    assert a == b and a["final"] != c["final"]


def test_zero_volatility_is_deterministic_pag_compounding():
    w = {"XIU.TO": 0.5, "SPY": 0.3, "XBB.TO": 0.2}
    r = P.project(w, 100_000, 12, AS_OF, inputs=inputs(vols={t: 0.0 for t in ("XIU.TO", "SPY", "XBB.TO", "GLD", "ODD")}),
                  n_paths=500, annual_fee=0.01)
    expected = deterministic(w, 100_000, 12, fee=0.01)
    pcts = r["final"]["nominal"]
    assert pcts["p50"] == pytest.approx(expected, abs=0.02)
    assert len(set(pcts.values())) == 1
    assert r["drawdown"]["p50"] == 0.0


def test_single_asset_median_compounds_at_pag_rate():
    r = P.project({"SPY": 1.0}, 100_000, 10, AS_OF, inputs=inputs(vols={"SPY": 0.18}), n_paths=20_000, seed=3)
    target = 100_000 * (1 + CFG["rates"]["us_equities"]) ** 10
    assert r["final"]["nominal"]["p50"] == pytest.approx(target, rel=0.02)
    real_target = target / (1 + CFG["inflation"]) ** 10
    assert r["final"]["real"]["p50"] == pytest.approx(real_target, rel=0.02)


def test_contributions_raise_every_percentile():
    w = {"XIU.TO": 0.6, "SPY": 0.4}
    a = P.project(w, 50_000, 10, AS_OF, inputs=inputs(), n_paths=3000)
    b = P.project(w, 50_000, 10, AS_OF, inputs=inputs(), n_paths=3000, monthly_contribution=500)
    assert all(b["final"]["nominal"][k] > a["final"]["nominal"][k] for k in a["final"]["nominal"])
    assert "prob_below_contributions" in b["final"] and "prob_below_contributions" not in a["final"]
    assert b["total_contributed"] == 50_000 + 500 * 120


def test_psd_repair_on_a_non_psd_matrix():
    bad = np.array([[1.0, 0.9, -0.9], [0.9, 1.0, 0.9], [-0.9, 0.9, 1.0]])
    assert np.linalg.eigvalsh(bad).min() < 0
    fixed, repaired = P.nearest_psd(bad)
    assert repaired and np.linalg.eigvalsh(fixed).min() >= -1e-10
    assert np.allclose(np.diag(fixed), 1.0) and np.allclose(fixed, fixed.T)
    ok, again = P.nearest_psd(np.eye(3))
    assert not again and np.allclose(ok, np.eye(3))


def test_non_psd_correlation_is_flagged_in_output():
    inp = inputs(tickers=("XIU.TO", "SPY", "XBB.TO"))
    inp.corr.loc["XIU.TO", "SPY"] = inp.corr.loc["SPY", "XIU.TO"] = 0.9
    inp.corr.loc["SPY", "XBB.TO"] = inp.corr.loc["XBB.TO", "SPY"] = 0.9
    inp.corr.loc["XIU.TO", "XBB.TO"] = inp.corr.loc["XBB.TO", "XIU.TO"] = -0.9
    r = P.project({"XIU.TO": 0.4, "SPY": 0.3, "XBB.TO": 0.3}, 1000, 10, AS_OF, inputs=inp, n_paths=200)
    assert r["assumptions"]["correlation_repaired"] is True


def test_unmapped_ticker_raises():
    with pytest.raises(UnmappedTickerError):
        P.project({"ODD": 1.0}, 1000, 10, AS_OF, inputs=inputs(), n_paths=100)
    with pytest.raises(KeyError):
        P.project({"NOPE": 1.0}, 1000, 10, AS_OF, inputs=inputs(), n_paths=100)


def test_short_horizon_and_team_assumption_warnings():
    r = P.project({"SPY": 0.9, "GLD": 0.1}, 1000, 5, AS_OF, inputs=inputs(), n_paths=200)
    text = " ".join(r["warnings"])
    assert "10 years or more" in text and "GLD" in text and "team assumption" in text
    gld = next(x for x in r["assumptions"]["per_ticker"] if x["ticker"] == "GLD")
    assert gld["team_assumption"] is True and gld["rate"] == CFG["team_assumptions"]["gold"]
    long = P.project({"SPY": 1.0}, 1000, 10, AS_OF, inputs=inputs(), n_paths=200)
    assert not any("10 years or more" in x for x in long["warnings"])


def test_output_shape_and_no_raw_paths():
    r = P.project({"SPY": 1.0}, 1000, 3, AS_OF, inputs=inputs(), n_paths=300)
    assert [y["year"] for y in r["percentiles_by_year"]] == [0, 1, 2, 3]
    assert set(r["percentiles_by_year"][1]["nominal"]) == {f"p{p}" for p in P.PERCENTILES}
    assert r["headline"]["p10"] == r["final"]["nominal"]["p10"]
    assert "expected" not in str(r["headline"]).lower()
    assert r["assumptions"]["source_url"].startswith("https://")
    assert "prob_drawdown_worse_than" not in r["drawdown"]       # rules.yaml defines no drawdown threshold
    assert len(str(r)) < 20_000


def _forecast(as_of, vol=0.30, long_run=0.10):
    return pd.DataFrame({"ticker": ["SPY"], "as_of": [as_of], "model": ["garch"], "forecast_vol": [vol],
                         "long_run_vol_ann": [long_run], "job_name": ["garch-test-1"]})


def test_auto_uses_garch_when_fresh_and_moves_to_long_run():
    r = P.project({"SPY": 1.0}, 1000, 5, AS_OF, inputs=inputs(forecast=_forecast("2026-11-10")), n_paths=200)
    vs = r["assumptions"]["vol_source"]
    assert vs["used"] == "garch" and vs["job_name"] == "garch-test-1" and vs["forecast_as_of"] == "2026-11-10"
    spy = r["assumptions"]["per_ticker"][0]
    assert spy["vol_start"] == pytest.approx(0.30) and spy["vol_end"] == pytest.approx(0.10)


@pytest.mark.parametrize("f_as_of,why", [("2026-10-01", "days old"), ("2026-12-01", "after the requested")])
def test_auto_falls_back_to_historical_when_stale_or_from_the_future(f_as_of, why):
    r = P.project({"SPY": 1.0}, 1000, 5, AS_OF, inputs=inputs(forecast=_forecast(f_as_of)), n_paths=200)
    assert r["assumptions"]["vol_source"]["used"] == "historical"
    assert why in r["assumptions"]["vol_source"]["reason"]
    with pytest.raises(ValueError):
        P.project({"SPY": 1.0}, 1000, 5, AS_OF, inputs=inputs(forecast=_forecast(f_as_of)), n_paths=200,
                  vol_source="garch")


def test_garch_vol_path_is_linear_in_year_two():
    vols, _, _ = P._vol_paths(inputs(forecast=_forecast("2026-11-12")), ["SPY"], pd.Timestamp(AS_OF),
                                 np.array([0.2]), 36, 7, "garch")
    assert vols[:12, 0] == pytest.approx([0.30] * 12)
    assert vols[17, 0] == pytest.approx(0.30 + (6 / 12) * (0.10 - 0.30))
    assert vols[23:, 0] == pytest.approx([0.10] * 13)


def test_point_in_time_vols_before_the_metrics_window_end():
    inp = inputs()
    r = P.project({"SPY": 1.0}, 1000, 5, "2023-06-30", inputs=inp, n_paths=100, vol_source="historical")
    assert "up to as_of" in r["assumptions"]["historical_vol_from"]
    expected = inp.returns.loc[:"2023-06-30", "SPY"].std() * math.sqrt(252)
    assert r["assumptions"]["per_ticker"][0]["vol_start"] == pytest.approx(expected, abs=1e-6)


def test_bad_inputs_rejected():
    for kw in ({"start_value": 0}, {"horizon_years": 0}, {"n_paths": P.MAX_PATHS + 1}, {"monthly_contribution": -1}):
        args = {"weights": {"SPY": 1.0}, "start_value": 1000, "horizon_years": 5, "as_of": AS_OF,
                "inputs": inputs(), "n_paths": 100, **kw}
        with pytest.raises(ValueError):
            P.project(**args)


def test_speed_20_assets_60_months_10k_paths():
    import time

    tick = [f"T{i}" for i in range(20)]
    uni = pd.DataFrame({"asset_class": "etf_equity", "market": "TSX", "name": tick}, index=pd.Index(tick, name="ticker"))
    idx = pd.bdate_range("2021-11-15", periods=1281)
    rets = pd.DataFrame(np.random.default_rng(0).normal(0, 0.01, (len(idx), 20)), index=idx, columns=tick)
    inp = P.MarketInputs(uni, rets, pd.Series(0.2, index=tick), idx[-1], rets.corr(), None, {})
    t0 = time.perf_counter()
    P.project({t: 0.05 for t in tick}, 1e5, 5, AS_OF, inputs=inp, n_paths=10_000)
    assert time.perf_counter() - t0 < 2.0
