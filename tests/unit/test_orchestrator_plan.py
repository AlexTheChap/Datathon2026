"""L6 wiring: the box 4 ExecutionPlan drives the orchestrator's sub-agents — no AWS, no LLM call."""
from __future__ import annotations

import pytest

from app.agents import orchestrator
from app.agents.subagents import REGISTRY, subagents_for_plan, subagents_for_toggles
from app.context import resolve_plan
from app.context.tool_guide import tools_block
from app.core.config import Settings
from app.schemas.models import OrchestratorInput


def _input(profile, request, **extra) -> OrchestratorInput:
    return OrchestratorInput(run_id="run-test", actor_id=profile.actor_id, profile=profile, request=request, **extra)


# ---------- Sub-agent selection ----------

def test_subagents_follow_the_plan_in_plan_order(make_profile, make_request):
    r = make_request("persona-amira", "allocation_proposal", toggles={"stress_tests": True, "include_rag": True})
    plan = resolve_plan(make_profile("amira"), r)
    assert [t.tool_name for t in subagents_for_plan(plan)] == [REGISTRY[a].tool_name for a in plan.enabled_names]


def test_mateo_esg_forced_by_plan_where_toggles_alone_would_drop_it(make_profile, make_request):
    """The bug the wiring fixes: Mateo switched ESG off, but his controversy exclusion must be screened."""
    p = make_profile("mateo", agent_toggles={"esg": False})
    plan = resolve_plan(p, make_request("persona-mateo", "allocation_proposal"))
    assert "esg_subagent" in [t.tool_name for t in subagents_for_plan(plan)]
    assert "esg_subagent" not in [t.tool_name for t in subagents_for_toggles(p.agent_toggles)]


def test_task_rules_limit_subagents(make_profile, make_request):
    """Toggles alone would register all 6 sub-agents; the plan keeps only what the task needs."""
    plan = resolve_plan(make_profile("robert"), make_request("persona-robert", "risk_review"))
    assert [t.tool_name for t in subagents_for_plan(plan)] == ["risk_subagent"]


# ---------- plan_for ----------

def test_payload_plan_is_used_as_is(make_profile, make_request):
    p, r = make_profile("robert"), make_request("persona-robert", "risk_review", toggles={"stress_tests": True})
    plan = resolve_plan(p, r)
    assert orchestrator.plan_for(_input(p, r, execution_plan=plan)) is plan


def test_missing_plan_is_resolved_with_the_same_code(make_profile, make_request):
    p, r = make_profile("mateo"), make_request("persona-mateo", "allocation_proposal")
    assert orchestrator.plan_for(_input(p, r)) == resolve_plan(p, r)


def test_plan_for_another_request_is_refused(make_profile, make_request):
    p = make_profile("robert")
    other = resolve_plan(p, make_request("persona-robert", "risk_review", request_id="req-OTHER"))
    with pytest.raises(ValueError, match="req-OTHER"):
        orchestrator.plan_for(_input(p, make_request("persona-robert", "risk_review"), execution_plan=other))


def test_payload_from_box4_json_validates(make_profile, make_request):
    """The dispatcher forwards box 4's JSON: OrchestratorInput accepts execution_plan + prompt_ref as dicts."""
    from app.context import handler
    p, r = make_profile("robert"), make_request("persona-robert", "risk_review")
    ctx = handler({"profile": p.model_dump(mode="json"), "request": r.model_dump(mode="json")})
    inp = OrchestratorInput.model_validate({
        "run_id": "run-1", "actor_id": p.actor_id, "profile": p.model_dump(mode="json"),
        "request": r.model_dump(mode="json"), "execution_plan": ctx["execution_plan"], "prompt_ref": ctx["prompt_ref"]})
    assert inp.execution_plan.enabled_names == ["risk"]
    assert inp.prompt_ref.key == "explainer.advanced.md"


# ---------- Tool guide ----------

def test_tools_block_lists_only_enabled_subagents_with_parameters(make_profile, make_request):
    r = make_request("persona-robert", "risk_review", toggles={"stress_tests": True}, parameters={"benchmark": "SPY"})
    g = tools_block("risk_review", resolve_plan(make_profile("robert"), r))
    assert "**`risk_subagent`**" in g
    for absent in ("esg_subagent", "news_subagent", "allocation_subagent", "fundamentals_subagent",
                   "macro_subagent"):
        assert f"**`{absent}`**" not in g
    assert '{"benchmark": "SPY"}' in g
    assert "Not available in this run" in g


def test_tools_block_without_plan_is_unchanged(make_profile, make_request):
    g = tools_block("risk_review")
    assert g.startswith("### Strategy") and "Execution plan" not in g
    assert all(f"**`{t.tool_name}`**" in g for t in REGISTRY.values())


# ---------- Orchestrator runs ----------

def test_out_of_scope_runs_no_llm(make_profile, make_request, monkeypatch, tmp_path):
    def must_not_build(*a, **k):
        raise AssertionError("the LLM agent must not be built for an out_of_scope plan")

    monkeypatch.setattr(orchestrator, "build_orchestrator", must_not_build)
    p, r = make_profile("robert"), make_request("persona-robert", "out_of_scope")
    out = orchestrator.run_orchestrator(_input(p, r), settings=Settings(runs_dir=tmp_path))
    assert out.status == "DONE" and out.evidence_ids == [] and "no sub-agent" in out.summary


def test_mismatched_plan_fails_visibly(make_profile, make_request, tmp_path):
    p = make_profile("robert")
    other = resolve_plan(p, make_request("persona-robert", "risk_review", request_id="req-OTHER"))
    out = orchestrator.run_orchestrator(_input(p, make_request("persona-robert", "risk_review"), execution_plan=other),
                                        settings=Settings(runs_dir=tmp_path))
    assert out.status == "FAILED" and "req-OTHER" in out.error


def test_built_agent_registers_exactly_the_plan_subagents(make_profile, make_request, tmp_path):
    """Builds the real Strands Agent (no model call) and checks its registered tools."""
    from app.tools.agent_tools import ALL_TOOLS

    p = make_profile("mateo", agent_toggles={"esg": False})
    r = make_request("persona-mateo", "allocation_proposal")
    agent = orchestrator.build_orchestrator(_input(p, r), settings=Settings(runs_dir=tmp_path))
    sub = {n for n in agent.tool_names if n.endswith("_subagent")}
    assert sub == {"risk_subagent", "esg_subagent", "allocation_subagent"}
    # Pure delegation: the orchestrator registers ONLY governance_check + the plan's
    # sub-agents — the quant tools live inside the sub-agents (no double compute).
    assert "governance_check" in agent.tool_names
    quant = {t.tool_spec["name"] for t in ALL_TOOLS} - {"governance_check"}
    assert not (quant & set(agent.tool_names))
    assert "Execution plan for this run" in agent.system_prompt
    assert "{{" not in agent.system_prompt
