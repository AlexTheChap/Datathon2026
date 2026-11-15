"""Contract between the Intake (box 3) and the Plan resolver (box 4), links L3 -> L4 of architecture.mmd.

The Plan resolver does not exist yet. Its input is the canonical Profile + Request (app/schemas) and its output
is the shape of examples/execution_plan.example.json (also modelled as `ExecutionPlan` on the orchestrator
branch). These tests check that the JSON the Intake really returns carries everything the resolver needs,
so it can be wired without changing the Intake:

- the Profile / Request JSON round-trip through the canonical models (what the resolver will parse);
- every header field of the ExecutionPlan can be copied from the Request with the same JSON type;
- the profile's `agent_toggles` names exactly the sub-agents the plan enables / disables;
- the request fields the plan's reasons and parameters rely on (toggles, scenarios, benchmark) are present;
- the example files stay in sync with what the Intake emits.
"""
import json
from pathlib import Path

import pytest

from app.intake import MockBackend, run_intake
from app.schemas import TASKS, AgentToggles, Profile, Request
from app.store.profiles import InMemoryProfileStore

EXAMPLES = Path(__file__).resolve().parents[2] / "backend" / "app" / "schemas" / "examples"
PLAN = json.loads((EXAMPLES / "execution_plan.example.json").read_text(encoding="utf-8"))
PLAN_AGENT_KEYS = {"enabled_agents", "disabled_agents", "run_governance", "run_explainer"}
PLAN_HEADER = {k: v for k, v in PLAN.items() if k not in PLAN_AGENT_KEYS}  # fields copied from the Request

SCENARIOS = [  # (actor, text): the three personas + the demo request, as a conversation with a shared store
    ("amira", "I'm 26, intermediate investor, high risk tolerance, horizon of 15 years or more, $50,000. "
              "I love AI and crypto but want some diversification. Is my portfolio of NVDA, QQQ and Bitcoin too risky?"),
    ("mateo", "Balanced profile, intermediate experience, horizon of 8 to 12 years, $150,000. I want to avoid "
              "companies linked to major controversies. Do Enbridge and Exxon pass my ESG screen?"),
    ("robert", "I'm cautious: I want to preserve my capital of $400,000 over 3 to 5 years. "
               "I actively follow the markets, advanced experience. No crypto for me."),
    ("robert", "I'm thinking of putting 20% in Nvidia, everyone is talking about it."),
    ("robert", "Is my portfolio too risky compared to SPY? Also show me what would happen in a crash."),
]


def _json_type(value):
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "number", float: "number",
            type(None): "null"}[type(value)]


@pytest.fixture(scope="module")
def outputs():
    """Intake results as the resolver will receive them: JSON strings, not Python objects."""
    store, backend, out = InMemoryProfileStore(), MockBackend(), []
    for actor, text in SCENARIOS:
        res = run_intake(text, actor, backend=backend, store=store)
        if res.status == "ready":
            out.append((json.loads(res.profile.model_dump_json()), json.loads(res.request.model_dump_json())))
    assert len(out) == 4  # Robert's profile-only message asks a question instead (nothing to plan)
    return out


def test_json_round_trips_through_canonical_models(outputs):
    for profile, request in outputs:
        assert Profile.model_validate_json(json.dumps(profile)).model_dump(mode="json") == profile
        assert Request.model_validate_json(json.dumps(request)).model_dump(mode="json") == request
        assert profile["actor_id"] == request["actor_id"]


def test_plan_header_can_be_copied_from_request(outputs):
    for _, request in outputs:
        for key, example_value in PLAN_HEADER.items():
            assert key in request, f"ExecutionPlan needs '{key}' from the Request"
            if request[key] is not None and example_value is not None:
                assert _json_type(request[key]) == _json_type(example_value), key


def test_agent_toggles_match_plan_agents():
    plan_agents = {a["agent"] for a in PLAN["enabled_agents"] + PLAN["disabled_agents"]}
    assert set(AgentToggles.model_fields) == plan_agents


def test_request_has_what_plan_reasons_and_parameters_use(outputs):
    for profile, request in outputs:
        assert request["task"] in TASKS
        assert set(request["toggles"]) >= {"stress_tests", "include_rag"}  # "toggles.stress_tests = true"...
        assert set(request["parameters"]) >= {"scenarios", "benchmark"}  # copied into agent parameters
        assert set(profile["agent_toggles"]) == set(AgentToggles.model_fields)
        if request["toggles"]["stress_tests"]:
            assert request["parameters"]["scenarios"], "stress tests need a scenario for macro_scenarios"


def test_demo_request_matches_the_plan_example(outputs):
    """Last scenario = the plan example's situation: risk_review, stress test 2022, benchmark SPY, no RAG."""
    _, request = outputs[-1]
    assert request["task"] == PLAN["task"] == "risk_review"
    assert request["toggles"] == {"stress_tests": True, "include_rag": False}
    assert request["parameters"] == {"scenarios": ["2022"], "benchmark": "SPY"}
    assert request["as_of"] == PLAN["as_of"]


def test_example_files_match_what_the_intake_emits(outputs):
    profile_example = json.loads((EXAMPLES / "profile.example.json").read_text(encoding="utf-8"))
    request_example = json.loads((EXAMPLES / "request.example.json").read_text(encoding="utf-8"))
    Profile.model_validate(profile_example)
    Request.model_validate(request_example)
    profile, request = outputs[0]
    assert set(request_example) == set(request), "request.example.json is out of sync with the Intake output"
    assert set(profile_example) <= set(profile)
