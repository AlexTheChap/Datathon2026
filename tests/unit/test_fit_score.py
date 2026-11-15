"""Fit score (governance/fit_score.py): out of 100, measured from the profile and the exact weights, on the real
curated data (no AWS, no network). A portfolio that suits the profile scores high; one that doesn't, scores low."""
from pathlib import Path

import pytest
import yaml

from app.agents.governance.fit_score import band_of, fit_score
from app.tools.risk_tools import RiskTools

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "backend/app/core/rules.yaml"
MATEO = {"SPY": 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, "QQQ": 0.0956}


@pytest.fixture(scope="module")
def rt():
    return RiskTools(ROOT / "data/curated").at("2026-11-12")


def profile(band, h_min, h_max=None, loss=None, exclusions=()):
    return {"actor_id": "t", "risk_band": band, "horizon_years": {"min": h_min, "max": h_max},
            "loss_capacity": loss, "exclusions": list(exclusions), "preferences": []}


def score(rt, weights, prof):
    return fit_score(weights, prof, RULES, rt, ROOT / "data")


def part(result, key):
    return next(p for p in result["parts"] if p["key"] == key)


def test_points_add_up_to_100_and_every_lost_point_has_a_reason(rt):
    cfg = yaml.safe_load(RULES.read_text(encoding="utf-8"))["fit_score"]
    assert sum(cfg["points"].values()) == 100
    r = score(rt, {"QQQ": 1.0}, profile("low", 3, 5, loss=0.15))
    assert r["out_of"] == 100 and 0 <= r["score"] <= 100
    assert abs(sum(p["points"] for p in r["parts"]) - r["score"]) < 1      # no cap here: the parts are the score
    for p in r["parts"]:
        for i in p["items"]:
            assert i["why"] and (i["lost"] == 0 or i["lost"] <= i["max"])


def test_a_portfolio_that_suits_the_profile_scores_high(rt):
    r = score(rt, MATEO, profile("moderate", 8, 12, exclusions=[{"type": "controversy", "min_severity": "major"}]))
    assert r["score"] >= 90 and r["band"] == "Good fit"


def test_too_bold_for_a_cautious_investor_scores_poor(rt):
    r = score(rt, {"QQQ": 1.0}, profile("low", 3, 5, loss=0.15))
    assert r["score"] < 50 and r["band"] == "Poor fit"
    assert part(r, "risk_level")["points"] == 0 and "too bold" in part(r, "risk_level")["items"][0]["why"]
    assert part(r, "loss")["points"] == 0 and "more than the loss you accept (15%)" in part(r, "loss")["items"][0]["why"]


def test_too_cautious_for_a_moderate_long_horizon_investor_is_not_a_good_fit(rt):
    r = score(rt, {"XBB.TO": 1.0}, profile("moderate", 10, 10))
    assert r["score"] < 70
    assert part(r, "asset_mix")["points"] == 0 and "too little" in part(r, "asset_mix")["items"][0]["why"]
    assert "too cautious" in part(r, "risk_level")["items"][0]["why"]


def test_the_same_portfolio_scores_differently_for_different_profiles(rt):
    fits = score(rt, MATEO, profile("moderate", 8, 12))["score"]
    cautious = score(rt, MATEO, profile("low_moderate", 3, 5, loss=0.10))["score"]
    assert fits - cautious >= 30


def test_a_broken_rule_caps_the_score(rt):
    r = score(rt, {"SPY": 0.9, "BTC-USD": 0.1}, profile("low", 10, 15))       # crypto is never allowed at low risk
    assert r["verdict"] == "BLOCK" and r["score"] <= 49
    crypto = next(i for i in part(r, "rules")["items"] if i["id"] == "CRYPTO")
    assert crypto["status"] == "BLOCK" and crypto["lost"] == crypto["max"]


def test_a_worst_drop_twice_the_stated_loss_caps_the_score_but_an_assumed_one_does_not(rt):
    stated = score(rt, {"SPY": 0.7, "QQQ": 0.3}, profile("high", 15, loss=0.10))
    assert stated["score"] <= 49 and any("twice the loss" in c["why"] for c in stated["caps"])
    assumed = score(rt, {"SPY": 0.7, "QQQ": 0.3}, profile("high", 15))
    assert assumed["score"] >= 90 and part(assumed, "loss")["items"][0]["assumed_limit"] is True


def test_a_concentrated_stock_part_loses_the_diversification_points(rt):
    r = score(rt, {"NVDA": 0.5, "XBB.TO": 0.5}, profile("moderate", 10))
    d = part(r, "diversification")["items"][0]
    assert d["measured"] == 1.0 and part(r, "diversification")["points"] == 0


def test_bands():
    assert [band_of(x) for x in (95, 85, 84, 70, 69, 50, 49)] == [
        "Good fit", "Good fit", "Acceptable fit", "Acceptable fit", "Weak fit", "Weak fit", "Poor fit"]
