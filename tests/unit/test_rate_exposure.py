"""rate_exposure tool and POST /v1/rate-exposure: bond-fund sensitivity next to the current rates. No AWS."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import ApiService, NoDispatcher
from app.intake import MockBackend
from app.schemas import Profile
from app.tools.rate_exposure_tool import run_rate_exposure

CURATED = Path(__file__).resolve().parents[2] / "data" / "curated"


def test_bond_funds_are_measured_stocks_are_not():
    ev = run_rate_exposure({"XBB.TO": .4, "TLT": .1, "SPY": .5}, "2026-11-12", CURATED)
    by = {h["ticker"]: h for h in ev["output"]["holdings"]}
    assert by["XBB.TO"]["measured"] and by["XBB.TO"]["duration"] > 5
    assert by["SPY"]["measured"] is False and by["SPY"]["impact_pct_per_point"] is None   # not measured, never 0
    p = ev["output"]["portfolio"]
    assert p["measured_share"] == pytest.approx(.5)
    assert p["impact_pct_per_point"] == pytest.approx(-(.4 * by["XBB.TO"]["duration"] + .1 * by["TLT"]["duration"]), abs=0.01)
    assert ev["output"]["rates"]["BoC policy rate (%)"] == 2.5
    assert ev["evidence_id"].startswith("ev-")


def test_weights_are_normalised_and_an_empty_portfolio_is_refused():
    ev = run_rate_exposure({"XBB.TO": 2, "SPY": 2}, "2026-11-12", CURATED)
    assert ev["inputs"]["weights"] == {"XBB.TO": .5, "SPY": .5}
    with pytest.raises(ValueError):
        run_rate_exposure({}, "2026-11-12", CURATED)


def test_no_bond_fund_means_nothing_measured():
    ev = run_rate_exposure({"SPY": 1}, "2026-11-12", CURATED)
    assert ev["output"]["portfolio"]["measured_share"] == 0
    assert "None of the holdings" in ev["say"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    svc = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
    svc.profiles.put(Profile(actor_id="u1", risk_band="moderate", experience="beginner", language="en",
                             portfolio={"XBB.TO": .6, "SPY": .4}))
    return TestClient(create_app(svc))


def test_endpoint_uses_the_saved_portfolio_by_default(client):
    r = client.post("/v1/rate-exposure", json={}, headers={"X-Actor-Id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert [h["ticker"] for h in body["holdings"]] == ["XBB.TO", "SPY"]
    assert body["portfolio"]["measured_share"] == pytest.approx(.6)


def test_endpoint_without_any_portfolio_is_a_422_and_unknown_ticker_too(client):
    assert client.post("/v1/rate-exposure", json={}, headers={"X-Actor-Id": "nobody"}).status_code == 422
    r = client.post("/v1/rate-exposure", json={"weights": {"NOPE": 1}}, headers={"X-Actor-Id": "u1"})
    assert r.status_code in (422,)
