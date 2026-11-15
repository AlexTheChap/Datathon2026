"""POST /v1/projection and GET /v1/vol-forecast: auth, isolation, profile defaults, n_paths cap, small responses.
Plus the projection / vol_forecast tools' evidence envelopes and the "projection" intake route."""
import pytest
from app.api.app import create_app
from app.api.service import ApiService
from app.intake import MockBackend
from app.quant.projection import MAX_PATHS
from app.tools.projection_tool import horizon_from_profile, run_projection
from app.tools.vol_forecast_tool import holding_risk_input, trend_of, vol_forecast
from fastapi.testclient import TestClient

ROBERT = {"XIU.TO": 0.3, "XBB.TO": 0.3, "ZST.TO": 0.1, "RY.TO": 0.1, "FTS.TO": 0.1, "JNJ": 0.1}
FORM = {"risk_band": "low_moderate", "experience": "advanced", "language": "en", "capital": 400000,
        "horizon_years": {"min": 3, "max": 5}, "portfolio": ROBERT}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    return TestClient(create_app(ApiService(intake_backend=MockBackend())))


def h(actor):
    return {"X-Actor-Id": actor}


def longest_list(value) -> int:
    if isinstance(value, dict):
        return max([longest_list(v) for v in value.values()] or [0])
    if isinstance(value, list):
        return max([len(value), *[longest_list(v) for v in value]])
    return 0


def keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_requires_sign_in(client):
    assert client.post("/v1/projection", json={"weights": {"SPY": 1}}).status_code == 401
    assert client.get("/v1/vol-forecast?tickers=SPY").status_code == 401


def test_profile_defaults_saved_portfolio_capital_and_horizon_upper_bound(client):
    assert client.put("/v1/profile", json=FORM, headers=h("robert")).status_code == 200
    r = client.post("/v1/projection", json={"n_paths": 2000}, headers=h("robert"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["inputs"]["weights"] == ROBERT and b["start_value"] == 400000 and b["horizon_years"] == 5
    assert "upper bound (5)" in b["horizon_note"]
    assert any("10 years or more" in w for w in b["warnings"])
    assert b["headline"]["p10"] < b["final"]["nominal"]["p50"] < b["headline"]["p90"]
    assert b["disclaimer"].startswith("Simulation under official planning assumptions")
    assert b["evidence_id"].startswith("ev-")


def test_another_user_does_not_get_my_saved_portfolio(client):
    client.put("/v1/profile", json=FORM, headers=h("robert"))
    r = client.post("/v1/projection", json={}, headers=h("mallory"))
    assert r.status_code == 422 and "weights" in r.json()["detail"]


def test_ad_hoc_weights_and_n_paths_cap(client):
    body = {"weights": {"SPY": 0.6, "XBB.TO": 0.4}, "start_value": 10000, "horizon_years": 12,
            "monthly_contribution": 100, "n_paths": 50000}
    b = client.post("/v1/projection", json=body, headers=h("u1")).json()
    assert b["inputs"]["n_paths"] == MAX_PATHS == 20000
    assert b["total_contributed"] == 10000 + 100 * 144 and "prob_below_contributions" in b["final"]
    assert not any("10 years or more" in w for w in b["warnings"])


def test_response_is_small_and_has_no_raw_paths(client):
    body = {"weights": {"SPY": 1}, "start_value": 1000, "horizon_years": 30, "n_paths": 20000}
    r = client.post("/v1/projection", json=body, headers=h("u1"))
    assert len(r.content) < 30_000
    assert longest_list(r.json()) <= 31             # 0..30 yearly percentiles; never one entry per path
    assert not [k for k in keys(r.json()) if "path" in k and k != "n_paths"]


@pytest.mark.parametrize("body", [{"weights": {"NOPE": 1}, "start_value": 1, "horizon_years": 5},
                                  {"weights": {"SPY": 1}, "start_value": 1, "horizon_years": 0},
                                  {"weights": {"SPY": 1}, "start_value": 1, "horizon_years": 5, "extra": 1},
                                  {"weights": {"SPY": 1}, "horizon_years": 5}])
def test_bad_bodies_are_422(client, body):
    assert client.post("/v1/projection", json=body, headers=h("u1")).status_code == 422


def test_vol_forecast_route(client):
    r = client.get("/v1/vol-forecast?tickers=NVDA,SPY", headers=h("u1"))
    assert r.status_code == 200
    b = r.json()
    assert set(b["tickers"]) == {"NVDA", "SPY"} and b["source"] == "garch"
    assert b["tickers"]["SPY"]["trend"] in ("rising", "stable", "easing")
    assert client.get("/v1/vol-forecast?tickers=NOPE", headers=h("u1")).status_code == 422


# ---------- tools ----------

def test_projection_evidence_traces_the_garch_job():
    ev = run_projection(ROBERT, 400000, 5, "2026-11-12", n_paths=1000)
    again = run_projection(ROBERT, 400000, 5, "2026-11-12", n_paths=1000)
    assert ev["evidence_id"] == again["evidence_id"] and ev["output"] == again["output"]
    assert ev["garch_job"]["as_of"] == "2026-11-12" and ev["garch_job"]["job_name"]
    assert "8 of 10 scenarios" in ev["say"] and "not a forecast" in ev["say"]
    assert ev["assumptions"][0] == ev["output"]["warnings"][0]          # warnings first (Explainer sees 4)
    assert any("team assumption" in a or "fixed_income" in a for a in ev["assumptions"])


def test_projection_before_the_forecast_date_uses_historical_volatility():
    ev = run_projection(ROBERT, 400000, 5, "2025-06-30", n_paths=500)
    vs = ev["output"]["assumptions"]["vol_source"]
    assert vs["used"] == "historical" and "after the requested as_of" in vs["reason"]
    assert "garch_job" not in ev


@pytest.mark.parametrize("horizon,years", [({"min": 3, "max": 5}, 5), ([15, None], 15), ({"min": 8, "max": 12}, 12),
                                           (None, None)])
def test_horizon_from_profile(horizon, years):
    assert horizon_from_profile(horizon)[0] == years


def test_trend_thresholds():
    cfg = {"risk_trend": {"rising_ratio": 1.15, "easing_ratio": 0.85}}
    assert trend_of(0.30, 0.20, cfg) == "rising"
    assert trend_of(0.16, 0.20, cfg) == "easing"
    assert trend_of(0.21, 0.20, cfg) == "stable"
    assert trend_of(None, 0.2, cfg) is None


def test_vol_forecast_evidence_and_holding_integration_point():
    ev = vol_forecast(["SPY", "XBB.TO"], "2026-11-12")
    assert ev["tool"] == "vol_forecast" and ev["evidence_id"].startswith("ev-")
    assert "not a return" in ev["say"] and ev["garch_job"]["job_name"]
    old = vol_forecast(["SPY"], "2024-01-31")                      # forecast file is dated after: not used
    assert old["output"]["source"] == "historical" and old["output"]["tickers"]["SPY"]["trend"] is None
    assert holding_risk_input("SPY", "2026-11-12")["risk_source"] == "garch_forecast"
    assert holding_risk_input("SPY", "2024-01-31")["risk_source"] == "historical"
