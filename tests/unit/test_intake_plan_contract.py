"""Contract between the Intake (box 3) and Context resolution (box 4), links L3 -> L4 of architecture.mmd.

The JSON the Intake really returns goes, unchanged, through the box 4 Lambda entry point
(`app.context.handler`, input {"profile": ..., "request": ...}). For each persona scenario we check that the
resolver accepts it and produces the expected ExecutionPlan, plus a prompt reference for the Explainer.
"""
import json
from pathlib import Path

import pytest
from app.context import handler as context_handler
from app.intake import MockBackend, run_intake
from app.schemas import Profile, Request
from app.schemas.execution_plan import ExecutionPlan
from app.store.profiles import InMemoryProfileStore

EXAMPLES = Path(__file__).resolve().parents[2] / "backend" / "app" / "schemas" / "examples"

ROBERT_PROFILE = ("I'm cautious: I want to preserve my capital of $400,000 over 3 to 5 years. "
                  "I actively follow the markets, advanced experience. No crypto for me.")

# (name, actor, text, expected task, expected enabled agents in diagram order)
SCENARIOS = [
    ("amira", "amira", "I'm 26, intermediate investor, high risk tolerance, horizon of 15 years or more, $50,000. "
     "I love AI and crypto but want some diversification. Is my portfolio of NVDA, QQQ and Bitcoin too risky?",
     "risk_review", ["risk"]),
    ("mateo", "mateo", "Balanced profile, intermediate experience, horizon of 8 to 12 years, $150,000. I want to "
     "avoid companies linked to major controversies. Do Enbridge and Exxon pass my ESG screen?",
     "esg_screen", ["esg"]),
    ("robert_nvda", "robert", "I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
     "allocation_proposal", ["risk", "news", "allocation"]),
    ("robert_crash", "robert", "Is my portfolio too risky compared to SPY? Also show me what would happen in a crash.",
     "risk_review", ["risk"]),
    ("robert_trade", "robert", "Buy 100 Tesla shares for me right now.", "out_of_scope", []),
    ("robert_projection", "robert", "How much could my portfolio of XIU.TO and XBB.TO be worth in 5 years?",
     "projection", ["risk"]),
]


@pytest.fixture(scope="module")
def runs():
    """Intake output (as JSON) -> box 4 handler, for every scenario. Robert's profile is stored first."""
    store, backend = InMemoryProfileStore(), MockBackend()
    run_intake(ROBERT_PROFILE, "robert", backend=backend, store=store)
    out = {}
    for name, actor, text, _, _ in SCENARIOS:
        res = run_intake(text, actor, backend=backend, store=store)
        event = {"profile": json.loads(res.profile.model_dump_json()),
                 "request": json.loads(res.request.model_dump_json())}
        out[name] = (res, event, context_handler(event))
    return out


@pytest.mark.parametrize("name,actor,text,task,agents", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_resolver_accepts_intake_json_and_plans_the_right_agents(runs, name, actor, text, task, agents):
    res, event, ctx = runs[name]
    plan = ExecutionPlan.model_validate(ctx["execution_plan"])
    assert plan.task == task
    assert plan.enabled_names == agents
    assert plan.actor_id == event["profile"]["actor_id"] == event["request"]["actor_id"]
    assert plan.request_id == event["request"]["request_id"]
    assert plan.as_of is not None  # the Intake always sets as_of, so no "as_of not set" warning
    assert not plan.needs_clarification
    assert plan.run_governance == bool(agents)
    assert ctx["prompt_ref"]  # the Explainer template was resolved from the profile


def test_crash_request_matches_the_documented_plan(runs):
    """Same situation as execution_plan.example.json: risk (benchmark SPY) + macro_scenarios, no news."""
    plan = ExecutionPlan.model_validate(runs["robert_crash"][2]["execution_plan"])
    params = {a.agent: a.parameters for a in plan.enabled_agents}
    assert params == {"risk": {"benchmark": "SPY"}}
    disabled = {a.agent: a.reason for a in plan.disabled_agents}
    assert "include_rag" in disabled["news"] or "not needed" in disabled["news"]


def test_example_files_match_what_the_intake_emits(runs):
    profile_example = json.loads((EXAMPLES / "profile.example.json").read_text(encoding="utf-8"))
    request_example = json.loads((EXAMPLES / "request.example.json").read_text(encoding="utf-8"))
    Profile.model_validate(profile_example)
    Request.model_validate(request_example)
    _, event, _ = runs["amira"]
    assert set(request_example) == set(event["request"]), "request.example.json is out of sync with the Intake"
    assert set(profile_example) <= set(event["profile"])
