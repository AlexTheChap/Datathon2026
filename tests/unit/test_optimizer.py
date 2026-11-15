"""Tests of the constrained portfolio optimizer (no AWS, no network):
python -m pytest tests/unit/test_optimizer.py  (from the repository root)"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/app/tools"))
sys.path.insert(0, str(ROOT / "backend/app/agents/governance"))
from governance_check import check
from optimizer import optimize
from risk_tools import RiskTools

RULES = ROOT / "backend/app/core/rules.yaml"
PERSONAS = {p["id"]: p for p in json.loads(
    (ROOT / "backend/app/core/personas.json").read_text(encoding="utf-8"))["personas"]}
ROBERT = PERSONAS["robert"]

# Robert's simulated portfolio plus the hyped stock he mentions (20% in Nvidia).
ROBERT_NVDA = {**ROBERT["demo_request"]["simulated_current_portfolio"], "NVDA": 0.20}
# A portfolio that already passes every rule for Robert (checked in test_compliant_portfolio...).
COMPLIANT = {"XIU.TO": .20, "XBB.TO": .35, "ZST.TO": .20, "JNJ": .08, "FTS.TO": .07, "RY.TO": .05, "AGG": .05}


@pytest.fixture(scope="module")
def rt():
    return RiskTools(ROOT / "data/curated")


def run(rt, w0, profile=ROBERT, **kw):
    return optimize(rt, w0, profile, RULES, data_dir=ROOT / "data", **kw)


def test_weights_sum_to_one_nonnegative_and_exclusions_are_zero(rt):
    # Crypto is excluded for Robert: even when requested and offered as candidates it must end at 0.
    w0 = {"XIU.TO": .3, "XBB.TO": .3, "BTC-USD": .1, "JNJ": .1, "ZST.TO": .1, "FTS.TO": .1}
    ev = run(rt, w0, candidates=["ETH-USD", "AGG"])
    out = ev["output"]
    assert out["status"] == "ok"
    w = out["weights"]
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v >= 0 for v in w.values())
    assert w.get("BTC-USD", 0) == 0 and w.get("ETH-USD", 0) == 0
    assert all(round(v, 4) == v and v >= 1e-4 for v in w.values())     # rounded, tiny weights dropped


def test_robert_nvidia_is_capped_and_not_blocked(rt):
    ev = run(rt, ROBERT_NVDA)
    out = ev["output"]
    assert out["status"] == "ok"
    assert out["weights"]["NVDA"] <= 0.05
    assert out["changes"]["NVDA"] < 0
    ver = out["verification"]
    assert ver["governance_verdict"] != "BLOCK"
    assert ver["attempts"] == 1
    assert check(out["weights"], ROBERT, RULES, rt, ROOT / "data")["verdict"] != "BLOCK"
    assert "NVDA" in ev["say"] and "should buy" not in ev["say"].lower()


def test_compliant_portfolio_comes_back_almost_unchanged(rt):
    assert check(COMPLIANT, ROBERT, RULES, rt, ROOT / "data")["verdict"] == "PASS"
    out = run(rt, COMPLIANT)["output"]
    assert out["status"] == "ok"
    assert max(abs(d) for d in out["changes"].values()) <= 0.005
    for t, w in COMPLIANT.items():
        assert out["weights"][t] == pytest.approx(w, abs=0.005)


def test_impossible_case_is_infeasible_and_returns_no_weights(rt):
    ev = run(rt, {"BTC-USD": .5, "ETH-USD": .5})                       # crypto-only for a profile with max_crypto = 0
    out = ev["output"]
    assert out["status"] == "infeasible"
    assert out["weights"] == {} and out["changes"] == {}
    assert "crypto" in out["explanation"].lower()
    assert any("crypto" in c for c in out["conflicting_constraints"])
    assert ev["evidence_id"].startswith("ev-")


def test_deterministic_evidence_id_and_weights(rt):
    a, b = run(rt, ROBERT_NVDA), run(rt, ROBERT_NVDA)
    assert a["evidence_id"] == b["evidence_id"]
    assert a["output"]["weights"] == b["output"]["weights"]
    assert run(rt, ROBERT_NVDA, risk_aversion=0.4)["evidence_id"] != a["evidence_id"]   # inputs change the id


def test_evidence_format(rt):
    ev = run(rt, ROBERT_NVDA)
    for k in ["evidence_id", "tool", "inputs", "output", "say", "currency", "as_of", "period", "method",
              "assumptions", "data_sources"]:
        assert k in ev
    assert ev["tool"] == "optimize" and ev["currency"] == "CAD"
    out = ev["output"]
    for k in ["status", "weights", "changes", "active_constraints", "verification"]:
        assert k in out
    assert set(out["verification"]) == {"governance_verdict", "attempts"}
    assert "SHORT_HORIZON_VOLATILITY" in out["active_constraints"]
    text = " ".join(ev["assumptions"]).lower()
    assert "no return forecast" in text
    assert "moved together in the past" in text and "past performance does not predict" in text


def test_as_of_cuts_future_data(rt):
    as_of = "2022-06-30"
    clean = run(rt, ROBERT_NVDA, as_of=as_of)
    assert clean["as_of"] == as_of and clean["period"].endswith(as_of)
    # Poison every return after as_of: a point-in-time optimizer must give the same answer.
    poisoned = copy.copy(rt)
    poisoned.rets = rt.rets.copy()
    poisoned.rets.loc[as_of:, :] = 0.5
    poisoned.rets.loc[as_of, :] = rt.rets.loc[as_of, :]
    again = run(poisoned, ROBERT_NVDA, as_of=as_of)
    assert again["output"]["weights"] == clean["output"]["weights"]
    assert again["output"]["verification"] == clean["output"]["verification"]
    # And the result without as_of really depends on the later data (the test would be vacuous otherwise).
    assert run(poisoned, ROBERT_NVDA)["output"]["weights"] != clean["output"]["weights"]


def test_tighter_band_still_converges(rt):
    tight = copy.deepcopy(ROBERT)
    tight["risk_band"] = "low"                                         # the strictest band
    out = run(rt, ROBERT_NVDA, profile=tight)["output"]
    assert out["status"] == "ok"
    assert out["weights"]["NVDA"] <= out["weights"].get("XIU.TO", 1)   # nothing dominates the bonds


# ---------- controversy exclusion: a flagged company is dropped, not a reason to fail ----------
def test_company_with_a_verified_major_controversy_gets_weight_zero(rt):
    mateo = PERSONAS["mateo"]                       # exclusion: controversy, min severity major
    w0 = {"CNQ.TO": .10, "SPY": .45, "XBB.TO": .35, "AAPL": .10}      # CNQ.TO: verified NBIM exclusion (major)
    ev = run(rt, w0, profile=mateo)
    out = ev["output"]
    assert out["status"] == "ok", out.get("explanation")
    assert "CNQ.TO" not in out["weights"]
    assert out["changes"]["CNQ.TO"] < 0


def test_controversy_exclusion_does_not_touch_a_profile_without_it(rt):
    w0 = {"CNQ.TO": .05, "SPY": .5, "XBB.TO": .45}
    ev = run(rt, w0, profile=ROBERT)                 # Robert excludes crypto only
    assert ev["output"]["status"] == "ok" and "CNQ.TO" in ev["output"]["weights"]
