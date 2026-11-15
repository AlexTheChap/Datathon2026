"""Dashboard (frontend/views.py) and the API routes it reads: GET /v1/runs, GET /v1/runs/{id}/evidence."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frontend"))

import views  # noqa: E402

from app.api.app import create_app  # noqa: E402
from app.api.dispatch import InlineDispatcher  # noqa: E402
from app.api.service import ApiService  # noqa: E402
from app.intake.backends import MockBackend  # noqa: E402
from app.schemas.models import OrchestratorOutput  # noqa: E402
from app.store.ledger import JsonlLedger  # noqa: E402

CURVE = [[0.0, -0.10], [0.05, -0.126], [0.09, -0.1494], [0.095, -0.1522]]
RECORDS = [
    {"seq": "E-001", "tool": "scenarios___stress", "agent": "risk_subagent", "evidence_id": "ev-87d4f9d88b",
     "payload": [{"evidence_id": "ev-87d4f9d88b", "say": "In 2022 …", "inputs": {"weights": {"NVDA": 0.2, "XBB.TO": 0.8}},
                  "output": {"window": "2022", "portfolio": {"max_drawdown": -0.2106, "total_return": -0.129},
                             "per_asset_total_return": {"NVDA": -0.47, "XBB.TO": -0.12}, "capital": 400000,
                             "loss_at_trough": -84231.6}}]},
    {"seq": "E-002", "tool": "optimizer___max_position", "agent": "risk_subagent", "evidence_id": "ev-829a97a221",
     "payload": [{"output": {"asset": "NVDA", "max_weight": 0.09, "limit": -0.15, "window": "2022", "curve": CURVE}}]},
    {"seq": "E-003", "tool": "risk___risk_contribution", "agent": "risk_subagent", "evidence_id": "ev-763c8280fc",
     "payload": [{"output": {"weights": {"NVDA": 0.2, "XBB.TO": 0.8}, "risk_contribution": {"NVDA": 0.73, "XBB.TO": 0.27},
                             "portfolio_vol": 0.126, "diversification_ratio": 1.48}}]},
    {"seq": "E-004", "tool": "search_news", "agent": "news_subagent", "evidence_id": "ev-a178841179",
     "payload": [{"output": {"hits": [{"title": "Old", "published_date": "2026-02-25", "doc_type": "filing"},
                                      {"title": "New", "published_date": "2026-11-12", "doc_type": "news"}]}}]},
    {"seq": "E-005", "tool": "risk_subagent", "error": "ValueError: bad weights", "payload": "Error: …"},
    {"seq": "E-006", "tool": "governance_check", "payload": [{"verdict": "BLOCK", "checks": [
        {"id": "MAX_STRESS_LOSS", "status": "BLOCK", "value": -0.21, "limit": -0.15, "evidence_id": "ev-9de6c73e44"}]}]},
    {"seq": "E-007", "tool": "allocation_subagent", "evidence_id": "ev-375124e379",
     "payload": [{"output": {"headline": "5% NVDA passes", "weights": {"NVDA": 0.05, "XBB.TO": 0.95}}}]},
]
ROBERT = {"risk_band": "low_moderate", "horizon_years": {"min": 3, "max": 5}, "preferences": [], "exclusions": []}
AMIRA = {"risk_band": "high", "horizon_years": {"min": 15, "max": None}, "preferences": ["crypto"], "exclusions": []}
MATEO = {"risk_band": "moderate", "horizon_years": {"min": 8, "max": 12}, "preferences": ["important ESG factors"],
         "exclusions": [{"type": "controversy", "min_severity": "major"}]}


@pytest.fixture
def ev():
    return views.RunEvidence.from_records(RECORDS)


def test_each_persona_gets_its_own_card_order():
    assert views.focus_for(ROBERT)[:4] == ["verdict", "loss_limit", "position_slider", "allocation"]
    assert views.focus_for(AMIRA)[:4] == ["verdict", "risk_contribution", "correlation", "concentration"]
    m = views.focus_for(MATEO)
    assert m.index("esg") < m.index("loss_limit") and m.index("news") < m.index("loss_limit")
    for order in (views.focus_for(ROBERT), views.focus_for(AMIRA), views.focus_for(MATEO)):
        assert len(order) == len(set(order))  # each card once


def test_gateway_prefixes_and_errors(ev):
    assert views.tool_name(RECORDS[0]) == "stress"
    assert "risk_subagent" not in ev.by_tool  # the failed call is not charted


def test_robert_crypto_exclusion_is_not_esg():
    robert = {**ROBERT, "exclusions": [{"type": "asset_class", "value": "crypto"}]}
    assert not views.wants_esg(robert) and "ESG" not in views.focus_label(robert)
    assert views.wants_esg(MATEO) and "ESG" in views.focus_label(MATEO)


def test_verdict_shows_idea_and_final_proposal():
    recs = RECORDS + [{"seq": "E-008", "tool": "governance_check",
                       "payload": [{"verdict": "PASS", "checks": [{"id": "MAX_STRESS_LOSS", "status": "PASS"}]}]}]
    v = views.verdict(views.RunEvidence.from_records(recs))
    assert v["verdict"] == "PASS" and v["initial"]["verdict"] == "BLOCK"


def test_cards_read_the_evidence(ev):
    assert views.verdict(ev)["verdict"] == "BLOCK" and views.verdict(ev)["initial"] is None
    loss = views.loss_limit(ev, -0.15)
    assert loss["scenarios"][0]["max_drawdown"] == -0.2106 and loss["limit"] == -0.15
    curve = views.position_curve(ev)
    assert curve["max_weight"] == 0.09 and curve["tested"] == {
        "weight": 0.2, "max_drawdown": -0.2106, "evidence_id": "ev-87d4f9d88b"}
    a = views.allocation(ev, {"XBB.TO": 1.0})
    assert a["proposed"]["NVDA"] == 0.05 and a["current"] == {"XBB.TO": 1.0}
    assert views.risk_contribution(ev)["risk"]["NVDA"] == 0.73
    assert [h["title"] for h in views.news(ev)["hits"]] == ["New", "Old"]  # newest first


def test_slider_uses_the_profile_limit_and_flags_a_different_tested_limit(ev):
    same = views.position_curve(ev, -0.15)
    assert same["limit"] == -0.15 and not same["limit_differs"]
    amira = views.position_curve(ev, -0.40)  # high risk band: -40%, the agent tested -15%
    assert amira["limit"] == -0.40 and amira["tested_limit"] == -0.15 and amira["limit_differs"]


def test_interpolate_slider():
    assert views.interpolate(CURVE, 0.0) == -0.10
    assert views.interpolate(CURVE, 0.07) == pytest.approx(-0.126 + (-0.1494 + 0.126) * 0.5)
    assert views.interpolate(CURVE, 0.2) == -0.1522


def test_claim_check_and_links():
    answer = "Loss -21% [ev-87d4f9d88b], limit [ev-9de6c73e44], invented ev-0000000000."
    res = {c["evidence_id"]: c["status"] for c in views.claim_check(answer, RECORDS)}
    assert res == {"ev-87d4f9d88b": "verified", "ev-9de6c73e44": "nested", "ev-0000000000": "not_found"}
    assert "[ev-87d4f9d88b](#ev-87d4f9d88b)" in views.link_evidence(answer)


def test_profile_loss_limit_from_rules():
    rules = {"risk_bands": {"low_moderate": {"max_drawdown_stress": 0.15}}}
    assert views.profile_loss_limit(ROBERT, rules) == -0.15
    assert views.profile_loss_limit(AMIRA, rules) is None


# ---------- API routes used by the dashboard ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    ledger = JsonlLedger(tmp_path)

    def fake_orchestrator(inp):
        ledger.append(inp.run_id, {"tool": "scenarios___stress", "evidence_id": "ev-1", "agent": "risk_subagent"})
        return OrchestratorOutput(run_id=inp.run_id, summary="see ev-1", evidence_ids=["ev-1"])

    svc = ApiService(intake_backend=MockBackend(), ledger=ledger)
    svc.dispatcher = InlineDispatcher(svc.runs, run_fn=fake_orchestrator)
    return TestClient(create_app(svc))


def _robert_run(c):
    h = {"X-Actor-Id": "robert"}
    r = c.post("/v1/runs", headers=h, json={"text": "Is my portfolio too risky? NVDA 20%, XBB.TO 80%.",
                                              "profile_form": {"risk_band": "low_moderate", "horizon_years": {"min": 3, "max": 5},
                                                               "experience": "advanced", "capital": 400000}})
    assert r.status_code == 202, r.json()
    return r.json()["run_id"]


def test_list_runs_and_evidence(client):
    run_id = _robert_run(client)
    runs = client.get("/v1/runs", headers={"X-Actor-Id": "robert"}).json()
    assert [r["run_id"] for r in runs] == [run_id] and runs[0]["status"] == "DONE" and runs[0]["n_evidence"] == 1
    ev = client.get(f"/v1/runs/{run_id}/evidence", headers={"X-Actor-Id": "robert"}).json()
    assert [(e["seq"], e["tool"]) for e in ev] == [("E-001", "scenarios___stress")]


def test_other_users_cannot_list_or_read_my_evidence(client):
    run_id = _robert_run(client)
    assert client.get("/v1/runs", headers={"X-Actor-Id": "mallory"}).json() == []
    assert client.get(f"/v1/runs/{run_id}/evidence", headers={"X-Actor-Id": "mallory"}).status_code == 404


# ---------- depth by financial knowledge ----------

def test_level_follows_experience_and_show_all_always_wins():
    assert views.level_for({"experience": "beginner"}) == "simple"
    assert views.level_for({"experience": "intermediate"}) == "standard"
    assert views.level_for({"experience": "advanced"}) == "expert"
    assert views.level_for({"experience": "advanced"}, override="simple") == "simple"
    assert views.level_for({"experience": "beginner"}, override="simple", show_all=True) == "expert"


def test_beginner_sees_few_cards_expert_sees_all():
    simple, standard, expert = (views.cards_for(ROBERT, lvl) for lvl in ("simple", "standard", "expert"))
    assert simple == ["loss_limit", "position_slider", "news"]
    assert set(simple) < set(standard) < set(expert)
    assert "correlation" not in standard and "metrics" not in standard and "correlation" in expert


def test_plain_summary_is_money_and_plain_words(ev):
    recs = RECORDS + [{"seq": "E-008", "tool": "governance_check",
                       "payload": [{"verdict": "PASS", "checks": []}]}]
    text = " ".join(views.plain_summary(views.RunEvidence.from_records(recs), ROBERT, -0.15))
    assert "$84,232" in text and "21%" in text and "15%" in text and "9%" in text
    assert "breaks your own safety rules" in text
    for jargon in ("drawdown", "VaR", "volatility", "HHI", "correlation"):
        assert jargon.lower() not in text.lower()


def test_the_explainer_answer_becomes_markdown_with_citations():
    answer = {"short_answer": "Blocked.", "short_answer_evidence": ["ev-1"],
              "checks": [{"allocation": "NVDA 20%", "verdict": "BLOCK", "text": "Too risky.", "evidence": ["ev-2"]}],
              "key_points": [{"text": "Volatile.", "evidence": []}], "glossary": [{"term": "Drawdown", "meaning": "A fall."}],
              "limitations": [], "not_analysed": ["News: off."], "disclaimer": "Simulation only."}
    md = views.answer_markdown(answer)
    assert md.startswith("Blocked. [ev-1]") and "- **BLOCK** · NVDA 20%: Too risky. [ev-2]" in md
    assert "## Words used in this answer" in md and "## Limits" not in md and md.endswith("_Simulation only._")
    assert views.short_answer(md) == "Blocked. [ev-1]" and views.answer_markdown(None) == ""


def test_short_answer_stops_at_first_section():
    assert views.short_answer("Intro line.\n\n## Details\nmore") == "Intro line."
    assert views.short_answer("Only text") == "Only text"
