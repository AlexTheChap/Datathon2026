"""Improve the fit (governance/improve.py): the closest mix that fits the profile better, every candidate scored with
the real fit score, on the real curated data (no AWS, no network)."""
from pathlib import Path

import pytest

from app.agents.governance.fit_score import fit_score
from app.agents.governance.improve import improve
from app.tools.risk_tools import RiskTools

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "backend/app/core/rules.yaml"


@pytest.fixture(scope="module")
def rt():
    return RiskTools(ROOT / "data/curated").at("2026-11-12")


def profile(band, h_min, h_max=None, loss=None):
    return {"actor_id": "t", "risk_band": band, "horizon_years": {"min": h_min, "max": h_max},
            "loss_capacity": loss, "exclusions": [], "preferences": []}


def run(rt, weights, prof):
    return improve(weights, prof, RULES, rt, ROOT / "data")


def test_a_bold_mix_for_a_cautious_investor_gets_a_much_better_one_that_really_scores_so(rt):
    prof = profile("low", 3, 5, loss=0.15)
    r = run(rt, {"QQQ": 1.0}, prof)
    assert r["found"] and r["best"]["score"] >= r["base"]["score"] + 40
    assert abs(sum(r["weights"].values()) - 1) < 0.001 and "QQQ" in r["weights"]          # their pick is kept
    assert fit_score(r["weights"], prof, RULES, rt, ROOT / "data")["score"] == r["best"]["score"]  # checked, not claimed
    assert any("share in stocks" in s for s in r["steps"]) and any("XBB.TO" in s for s in r["steps"])


def test_what_breaks_a_rule_is_removed_and_said(rt):
    r = run(rt, {"SPY": 0.9, "BTC-USD": 0.1}, profile("low", 10, 15))
    assert "BTC-USD" not in r["weights"]
    assert any(s.startswith("Removed BTC-USD: crypto isn't allowed") for s in r["steps"])
    assert {"ticker": "BTC-USD", "from": 0.1, "to": 0} in r["changes"]


def test_single_stocks_and_crypto_are_capped_under_their_limits_not_dropped(rt):
    r = run(rt, {"NVDA": 0.5, "BTC-USD": 0.5}, profile("high", 15))
    assert r["weights"]["NVDA"] <= 0.15 * 0.9 + 1e-4 and r["weights"]["BTC-USD"] <= 0.10 * 0.9 + 1e-4
    assert r["best"]["score"] >= 85 and r["best"]["verdict"] != "BLOCK"
    assert sum(1 for s in r["steps"] if s.startswith("Capped")) == 2


def test_a_mix_that_already_fits_is_left_alone(rt):
    r = run(rt, {"SPY": 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, "QQQ": 0.0956}, profile("moderate", 8, 12))
    assert r["found"] is False and r["weights"] is None and "better" in r["message"]
