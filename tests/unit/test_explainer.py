"""Explainer + claim verifier wiring: context built from the ledger, one rewrite on unknown citations, nothing
unverifiable left looking sourced. The model call is injected: no AWS, no LLM."""
from __future__ import annotations

import json

import pytest
from app.agents import explainer
from app.context.plan_resolver import resolve_plan
from app.context.prompt_loader import PromptVersionMismatch
from app.core.config import Settings
from app.schemas.models import OrchestratorInput
from app.store.ledger import InMemoryLedger

STRESS, GOV, RISK_AGENT = "ev-3f2a91c0de", "ev-5e75ec16fc", "ev-aaaa000001"
RUN = "run-x"


@pytest.fixture
def setup(make_profile, make_request):
    profile = make_profile("robert")
    request = make_request("persona-robert", "risk_review", toggles={"stress_tests": True})
    plan = resolve_plan(profile, request)
    inp = OrchestratorInput(run_id=RUN, actor_id=request.actor_id, profile=profile, request=request,
                            execution_plan=plan)
    led = InMemoryLedger()
    led.append(RUN, {"tool": "stress", "agent": "risk_subagent", "evidence_id": STRESS, "payload": {
        "evidence_id": STRESS, "tool": "stress", "say": "In 2022 this portfolio would have returned -12.9%.",
        "period": "2022-01-03/2022-12-30", "as_of": "2026-11-12", "assumptions": ["Historical, not a forecast."],
        "output": {"portfolio": {"max_drawdown": -0.2112}}}})
    led.append(RUN, {"tool": "risk_subagent", "evidence_id": RISK_AGENT, "payload": {
        "evidence_id": RISK_AGENT, "tool": "risk_subagent", "nested_evidence_ids": [STRESS], "say": "Risky",
        "output": {"headline": "NVDA breaks the loss limit", "severity": "block", "details": [f"-21.1% {STRESS}"]}}})
    led.append(RUN, {"tool": "governance_check", "evidence_id": GOV, "payload": {
        "evidence_id": GOV, "tool": "governance_check", "say": "Governance verdict BLOCK",
        "verdict": "BLOCK", "output": {"verdict": "BLOCK", "missing_info": ["loss_capacity"], "checks": [
            {"id": "SINGLE_POSITION", "status": "WARN", "value": 0.1667, "limit": 0.1, "detail": "NVDA"}]}}})
    return inp, plan, led


class ScriptedLLM:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.answers.pop(0)


def run(setup, llm):
    inp, plan, led = setup
    return explainer.explain(inp, plan, "2026-11-12", led, "notes: NVDA looks risky", settings=Settings(), llm=llm)


GOOD = (f"The worst point of the 2022 replay was -21.1% [{STRESS}]. The single-position check says 16.7% against a "
        f"limit of 10% [{GOV}]. Simulation only.")


def test_collect_splits_evidence_findings_and_governance(setup):
    _, _, led = setup
    evidence, findings, governance = explainer.collect(led, RUN)
    assert set(evidence) == {STRESS, RISK_AGENT, GOV}
    assert [f["agent"] for f in findings] == ["risk_subagent"] and findings[0]["headline"].startswith("NVDA")
    assert governance["evidence_id"] == GOV


def test_context_has_what_the_template_promises(setup):
    inp, plan, led = setup
    ctx = explainer.build_context(inp, plan, "2026-11-12", *explainer.collect(led, RUN), "notes")
    assert set(ctx) == {"as_of", "profile", "request", "portfolio_weights", "execution_plan", "governance",
                        "findings", "evidence", "orchestrator_notes"}
    assert ctx["governance"]["verdict"] == "BLOCK" and ctx["governance"]["checks"][0]["id"] == "SINGLE_POSITION"
    assert [e["evidence_id"] for e in ctx["evidence"]] == [STRESS, GOV]       # sub-agent envelopes live in findings
    assert {d["agent"] for d in ctx["execution_plan"]["disabled_agents"]} >= {"news", "esg"}


def test_a_well_sourced_answer_is_returned_as_is(setup):
    llm = ScriptedLLM(GOOD)
    res = run(setup, llm)
    assert res.text == GOOD and res.attempts == 1
    assert res.verification["ok"] and res.verification["unknown"] == [] and res.verification["unmatched_numbers"] == []
    system, user = llm.calls[0]
    assert "You are the **Explainer**" in system and "Reader level: advanced" in system   # Robert's template
    payload = json.loads(user.split("```json\n")[1].rsplit("\n```", 1)[0])
    assert payload["orchestrator_notes"] == "notes: NVDA looks risky" and payload["governance"]["verdict"] == "BLOCK"
    assert res.template["key"] == "explainer.advanced.md" and len(res.template["sha256"]) == 64


def test_unknown_citation_triggers_one_rewrite(setup):
    llm = ScriptedLLM("The loss was -21.1% [ev-ffffffffff].", GOOD)
    res = run(setup, llm)
    assert res.text == GOOD and res.attempts == 2 and res.verification["ok"]
    assert "ev-ffffffffff" in llm.calls[1][1] and "do not exist" in llm.calls[1][1]


def test_an_answer_still_unverifiable_is_marked_not_ok_and_unsourced(setup):
    llm = ScriptedLLM("Loss -21.1% [ev-ffffffffff].", "Loss -21.1% [ev-ffffffffff] again.")
    res = run(setup, llm)
    assert res.attempts == 2 and res.verification["ok"] is False and res.verification["unknown"] == ["ev-ffffffffff"]
    assert "ev-ffffffffff" not in res.text and "[unverified]" in res.text


def test_warnings_are_reported_without_failing(setup):
    res = run(setup, ScriptedLLM(f"The loss was -35.0% [{STRESS}]. NVDA could also fall 50%."))
    assert res.verification["ok"] is True
    assert res.verification["unmatched_numbers"][0]["number"] == "-35.0%"
    assert res.verification["uncited_numbers"] == ["NVDA could also fall 50%."]


def test_a_template_changed_since_box_4_is_refused(setup):
    inp, plan, led = setup
    bad = inp.model_copy(update={"prompt_ref": explainer.resolve_explainer_prompt(inp.profile)
                                 .model_copy(update={"sha256": "0" * 64})})
    with pytest.raises(PromptVersionMismatch):
        explainer.explain(bad, plan, "2026-11-12", led, "", settings=Settings(), llm=ScriptedLLM(GOOD))
