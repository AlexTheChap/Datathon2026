"""Plan resolver tests (④, links L4 → L6). Run: python -m pytest -q tests  (from the repo root)"""
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.context import load_registry, resolve_plan
from app.context.plan_resolver import REGISTRY_PATH, TRIGGERS, default_registry
from app.schemas import TASKS, AgentToggles
from app.schemas.execution_plan import AGENTS, ExecutionPlan

APP = Path(__file__).resolve().parents[2] / "backend" / "app"
EXAMPLES = APP / "schemas" / "examples"


def _by_agent(plan: ExecutionPlan) -> dict:
    return {a.agent: a for a in plan.enabled_agents}


# ---------- Registry ----------

def test_registry_covers_every_task_and_known_triggers():
    reg = default_registry()
    assert set(reg.tasks) == set(TASKS)
    assert all(r.trigger in TRIGGERS for r in reg.triggers)


def test_registry_default_scenario_matches_rules_yaml():
    rules = yaml.safe_load((APP / "core" / "rules.yaml").read_text(encoding="utf-8"))
    assert default_registry().default_scenarios == [rules["stress_window"]]


def test_agent_names_match_profile_toggles():
    assert set(AGENTS) == set(AgentToggles.model_fields)


def test_registry_rejects_unknown_trigger(tmp_path):
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["triggers"].append({"agent": "news", "trigger": "does_not_exist"})
    bad = tmp_path / "reg.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_registry(bad)


def test_registry_rejects_missing_task(tmp_path):
    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    del raw["tasks"]["esg_screen"]
    bad = tmp_path / "reg.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_registry(bad)


# ---------- Contract: JSON examples ----------

def test_examples_produce_the_documented_plan(example_profile, example_request):
    """execution_plan.example.json is the exact resolver output for the two other examples."""
    expected = json.loads((EXAMPLES / "execution_plan.example.json").read_text(encoding="utf-8"))
    assert resolve_plan(example_profile, example_request).model_dump(mode="json") == expected


def test_every_agent_listed_exactly_once(make_profile, make_request):
    for task in TASKS:
        plan = resolve_plan(make_profile("mateo"), make_request("persona-mateo", task))
        names = [a.agent for a in plan.enabled_agents] + [a.agent for a in plan.disabled_agents]
        assert sorted(names) == sorted(AGENTS), task


def test_deterministic(make_profile, make_request):
    p, r = make_profile("robert"), make_request("persona-robert", "risk_review", toggles={"stress_tests": True})
    assert resolve_plan(p, r).model_dump() == resolve_plan(p, r).model_dump()


def test_plan_keeps_diagram_order(make_profile, make_request):
    r = make_request("persona-amira", "allocation_proposal",
                     toggles={"stress_tests": True, "include_rag": True})
    plan = resolve_plan(make_profile("amira"), r)
    assert plan.enabled_names == [a for a in AGENTS if a in plan.enabled_names]


# ---------- Rules per task ----------

@pytest.mark.parametrize("task, required", [
    ("risk_review", {"risk"}),
    ("scenario_analysis", {"macro_scenarios", "risk"}),
    ("allocation_proposal", {"allocation", "risk"}),
    ("esg_screen", {"esg"}),
    ("fundamentals_review", {"fundamentals"}),
    ("news_review", {"news"}),
])
def test_required_agents_per_task(make_profile, make_request, task, required):
    plan = resolve_plan(make_profile("amira"), make_request("persona-amira", task))
    assert set(plan.enabled_names) == required
    assert plan.run_governance and plan.run_explainer


def test_out_of_scope_runs_no_agent_and_no_governance(make_profile, make_request):
    r = make_request("persona-robert", "out_of_scope", toggles={"stress_tests": True, "include_rag": True})
    plan = resolve_plan(make_profile("robert"), r)
    assert plan.enabled_agents == []
    assert plan.run_governance is False and plan.run_explainer is True
    assert all("out_of_scope" in d.reason for d in plan.disabled_agents)


# ---------- Triggers and toggles ----------

def test_stress_tests_enable_macro_with_default_scenario(make_profile, make_request):
    r = make_request("persona-robert", "risk_review", toggles={"stress_tests": True})
    agents = _by_agent(resolve_plan(make_profile("robert"), r))
    assert agents["macro_scenarios"].parameters == {"scenarios": ["2022"]}
    assert "toggles.stress_tests = true" in agents["macro_scenarios"].reason


def test_explicit_scenarios_are_passed_through(make_profile, make_request):
    r = make_request("persona-robert", "risk_review", parameters={"scenarios": ["2022_H1"], "benchmark": "XIU.TO"})
    agents = _by_agent(resolve_plan(make_profile("robert"), r))
    assert agents["macro_scenarios"].parameters == {"scenarios": ["2022_H1"]}
    assert agents["risk"].parameters == {"benchmark": "XIU.TO"}


def test_include_rag_enables_news(make_profile, make_request):
    r = make_request("persona-amira", "risk_review", toggles={"include_rag": True})
    assert "news" in resolve_plan(make_profile("amira"), r).enabled_names


def test_required_agent_forced_on_despite_toggle(make_profile, make_request):
    p = make_profile("amira", agent_toggles={"risk": False})
    plan = resolve_plan(p, make_request("persona-amira", "risk_review"))
    risk = _by_agent(plan)["risk"]
    assert risk.forced is True
    assert "agent_toggles.risk = false ignored" in risk.reason
    assert any("risk enabled although" in w for w in plan.warnings)


def test_optional_agent_toggle_off_wins_with_warning(make_profile, make_request):
    p = make_profile("robert", agent_toggles={"news": False})
    plan = resolve_plan(p, make_request("persona-robert", "risk_review", toggles={"include_rag": True}))
    assert "news" not in plan.enabled_names
    news = next(d for d in plan.disabled_agents if d.agent == "news")
    assert "agent_toggles.news = false" in news.reason and "include_rag" in news.reason
    assert any(w.startswith("news requested") for w in plan.warnings)


def test_toggle_on_but_not_needed_stays_disabled(make_profile, make_request):
    plan = resolve_plan(make_profile("amira"), make_request("persona-amira", "esg_screen"))
    assert next(d for d in plan.disabled_agents if d.agent == "fundamentals").reason == \
        "not needed for task esg_screen"


# ---------- Personas (brief) ----------

def test_amira_allocation_has_no_esg(make_profile, make_request):
    """Amira: no ESG preference or exclusion → the ESG agent is not added."""
    plan = resolve_plan(make_profile("amira"), make_request("persona-amira", "allocation_proposal"))
    assert set(plan.enabled_names) == {"risk", "allocation"}


def test_mateo_esg_forced_by_controversy_exclusion(make_profile, make_request):
    """Mateo: 'major controversy' exclusion → ESG enabled even if he switched the ESG agent off."""
    p = make_profile("mateo", agent_toggles={"esg": False})
    plan = resolve_plan(p, make_request("persona-mateo", "allocation_proposal"))
    esg = _by_agent(plan)["esg"]
    assert esg.forced is True
    assert "controversy" in esg.reason and "ESG" in esg.reason


def test_mateo_esg_added_for_any_task(make_profile, make_request):
    plan = resolve_plan(make_profile("mateo"), make_request("persona-mateo", "risk_review"))
    assert "esg" in plan.enabled_names


def test_robert_nvidia_demo_runs_risk_and_stress(make_profile, make_request):
    """Robert: '20% in Nvidia' → risk + 2022 scenario so that governance can BLOCK."""
    r = make_request("persona-robert", "risk_review", toggles={"stress_tests": True},
                     user_message="I'm thinking of putting 20% in Nvidia, everyone is talking about it.")
    plan = resolve_plan(make_profile("robert"), r)
    assert set(plan.enabled_names) == {"risk", "macro_scenarios"}
    assert plan.run_governance


# ---------- Edge cases ----------

def test_low_confidence_flags_but_still_runs(make_profile, make_request):
    plan = resolve_plan(make_profile("amira"), make_request("persona-amira", "risk_review", intake_confidence=0.4))
    assert plan.needs_clarification is True
    assert plan.enabled_names == ["risk"]
    assert any("intake_confidence" in w for w in plan.warnings)


def test_confidence_at_threshold_is_not_flagged(make_profile, make_request):
    plan = resolve_plan(make_profile("amira"), make_request("persona-amira", "risk_review", intake_confidence=0.6))
    assert plan.needs_clarification is False


def test_actor_mismatch_is_rejected(make_profile, make_request):
    with pytest.raises(ValueError, match="actor_id"):
        resolve_plan(make_profile("amira"), make_request("persona-robert", "risk_review"))


def test_missing_portfolio_and_as_of_are_warned(make_profile, make_request):
    r = make_request("persona-amira", "risk_review", portfolio_ref=None, as_of=None)
    plan = resolve_plan(make_profile("amira"), r)
    assert any("portfolio_ref" in w for w in plan.warnings)
    assert any("as_of" in w for w in plan.warnings)


def test_plan_rejects_duplicate_or_missing_agents():
    base = dict(registry_version=1, request_id="r", actor_id="a", task="risk_review",
                run_governance=True, intake_confidence=0.9)
    with pytest.raises(ValidationError):
        ExecutionPlan(**base, enabled_agents=[{"agent": "risk", "reason": "x"}], disabled_agents=[])
