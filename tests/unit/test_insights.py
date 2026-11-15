"""Dashboard insights (score, Venn sets, explanation, parameters) and the crisis replay route."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frontend"))

import insights  # noqa: E402
import views  # noqa: E402

from app.api.replay import portfolios_from_evidence, replay  # noqa: E402
from app.tools.risk_tools import RiskTools  # noqa: E402

pytestmark = pytest.mark.skip(reason="stress-window feature removed: insights/replay to be reworked for the dashboard")

CURATED = Path(__file__).resolve().parents[2] / "data" / "curated"
IDEA = {"XIU.TO": 0.24, "XBB.TO": 0.24, "ZST.TO": 0.08, "RY.TO": 0.08, "FTS.TO": 0.08, "JNJ": 0.08, "NVDA": 0.2}
PROPOSAL = {"XBB.TO": 0.4, "XIU.TO": 0.2, "ZST.TO": 0.15, "JNJ": 0.1, "RY.TO": 0.07, "NVDA": 0.05, "FTS.TO": 0.03}


@pytest.fixture(scope="module")
def rt():
    return RiskTools(CURATED)


@pytest.fixture(scope="module")
def records(rt):
    stress_idea = rt.stress(IDEA, "2022", 400000)
    stress_small = rt.stress({**IDEA, "NVDA": 0.05}, "2022")
    pos = rt.max_position({k: v for k, v in IDEA.items() if k != "NVDA"}, "NVDA", 0.15)
    return [
        {"seq": "E-001", "tool": "scenarios___stress", "evidence_id": stress_small["evidence_id"], "payload": [stress_small]},
        {"seq": "E-002", "tool": "scenarios___stress", "evidence_id": stress_idea["evidence_id"], "payload": [stress_idea]},
        {"seq": "E-003", "tool": "optimizer___max_position", "evidence_id": pos["evidence_id"], "payload": [pos]},
        {"seq": "E-004", "tool": "governance_check", "payload": [{"verdict": "BLOCK", "checks": [
            {"id": "MAX_STRESS_LOSS", "status": "BLOCK", "value": -0.21, "limit": -0.15, "evidence_id": "ev-x"},
            {"id": "SINGLE_POSITION", "status": "WARN", "value": 0.2, "limit": 0.1}]}]},
        {"seq": "E-005", "tool": "allocation_subagent", "evidence_id": "ev-alloc",
         "payload": [{"output": {"headline": "5% NVDA", "weights": PROPOSAL}}]},
        {"seq": "E-006", "tool": "governance_check", "payload": [{"verdict": "PASS", "checks": [
            {"id": "MAX_STRESS_LOSS", "status": "PASS", "value": -0.119, "limit": -0.15}]}]},
    ]


def test_replay_reads_the_runs_portfolios_and_matches_the_stress_tool(rt, records):
    found = portfolios_from_evidence(records)
    assert found["idea"]["NVDA"] == 0.2           # the tested asset at its highest weight, not the 5% variant
    assert found["proposal"] == PROPOSAL
    r = replay(records, 400000, rt)
    idea = r["series"]["idea"]
    assert idea["value"][0] == 400000 and idea["dates"][0] == "2021-12-31"
    assert idea["max_drawdown"] == pytest.approx(rt.stress(IDEA, "2022")["output"]["portfolio"]["max_drawdown"], abs=1e-6)
    assert r["series"]["proposal"]["max_drawdown"] > idea["max_drawdown"]   # the proposal falls less
    assert "not a forecast" in r["note"]


def test_fit_score_explains_every_point(records):
    sc = insights.scores(views.RunEvidence.from_records(records))
    idea, prop = sc["idea"], sc["proposal"]
    assert idea["score"] == 4.0 and idea["band"] == "Risky fit"   # 10 - (3 + 2 over the limit) - 1 warning
    assert sum(w["points"] for w in idea["why"]) == pytest.approx(idea["score"] - 10, abs=0.5)
    assert prop["score"] == 10 and prop["band"] == "Good fit"


def test_fit_score_bounds():
    assert insights.fit_score([])["score"] == 10
    many = [{"id": f"R{i}", "status": "BLOCK"} for i in range(9)]
    assert insights.fit_score(many)["score"] == 0


def test_venn_idea_vs_allowed(rt, records):
    ev = views.RunEvidence.from_records(records)
    rp = replay(records, 400000, rt)
    curve = views.position_curve(ev, -0.15)
    v = insights.venn_idea_vs_allowed({"exclusions": [{"type": "asset_class", "value": "crypto"}]}, curve, rp)
    assert v["left"][0] == "NVDA 20%" and "no crypto" in v["right"] and v["both"][0] == "NVDA 5%"


def test_venn_holdings_kept_sold_added():
    v = insights.venn_holdings({"A": 0.5, "B": 0.5}, {"B": 0.6, "C": 0.4})
    assert v["left"] == ["A 50%"] and v["right"] == ["C 40%"] and v["both"] == ["B 50%→60%"]
    assert insights.venn_holdings(None, {"B": 1.0}) is None


def test_venn_risk_drivers_puts_nvda_in_the_centre():
    risk = {"weights": {"NVDA": 0.2, "XBB.TO": 0.24, "ZST.TO": 0.08}, "risk": {"NVDA": 0.73, "XBB.TO": 0.03, "ZST.TO": 0.0}}
    v = insights.venn_risk_drivers(risk, {"NVDA": -0.47, "XBB.TO": -0.12, "ZST.TO": 0.01})
    assert v["regions"]["ABC"] == ["NVDA"] and v["regions"]["AC"] == ["XBB.TO"] and v["outside"] == ["ZST.TO"]


def test_analysis_and_parameters_are_built_from_evidence(rt, records):
    ev = views.RunEvidence.from_records(records)
    rp = replay(records, 400000, rt)
    text = insights.analysis_paragraph(ev, {"risk_band": "low_moderate"}, -0.15, rp)
    assert "maximum drawdown" in text and "low-moderate" in text and "PASS" in text
    names = [p["parameter"] for p in insights.key_parameters(ev, rp)]
    assert "Daily VaR 95%" in names and "Max drawdown (2022 replay)" in names and "Scenario" in names


def test_replay_route_owner_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.api.dispatch import InlineDispatcher
    from app.api.service import ApiService
    from app.intake.backends import MockBackend
    from app.schemas.models import OrchestratorOutput
    from app.store.ledger import JsonlLedger

    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    ledger = JsonlLedger(tmp_path)
    rt = RiskTools(CURATED)
    s = rt.stress(IDEA, "2022")

    def fake(inp):
        ledger.append(inp.run_id, {"tool": "scenarios___stress", "evidence_id": s["evidence_id"], "payload": [s]})
        return OrchestratorOutput(run_id=inp.run_id, summary="x")

    svc = ApiService(intake_backend=MockBackend(), ledger=ledger)
    svc.dispatcher = InlineDispatcher(svc.runs, run_fn=fake)
    c = TestClient(create_app(svc))
    r = c.post("/v1/runs", headers={"X-Actor-Id": "rob"}, json={
        "text": "Is my portfolio too risky? NVDA 20%, XBB.TO 80%.",
        "profile_form": {"risk_band": "low_moderate", "horizon_years": {"min": 3, "max": 5},
                         "experience": "beginner", "capital": 400000}})
    run_id = r.json()["run_id"]
    rp = c.get(f"/v1/runs/{run_id}/replay", headers={"X-Actor-Id": "rob"}).json()
    assert rp["capital"] == 400000 and rp["series"]["idea"]["value"][0] == 400000
    assert c.get(f"/v1/runs/{run_id}/replay", headers={"X-Actor-Id": "eve"}).status_code == 404
