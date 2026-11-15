"""Integration ③ → ④ through the real Lambda entry points (mock intake backend, local prompts):

  intake Lambda handler({text, actor_id})  →  HTTP response {statusCode, body: IntakeResult JSON}
    → glue (future box ② dispatcher): keep profile + request only when status allows it
    → context Lambda handler({profile, request})  →  {execution_plan, prompt_ref}
"""
import json

import pytest

from app.api import intake_handler
from app.context import handler as context_handler
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("AF_INTAKE_MODE", "mock")
    monkeypatch.setenv("AF_PROMPT_SOURCE", "local")
    monkeypatch.setattr(intake_handler, "_backend", None)


def call_intake(text: str, actor: str) -> dict:
    # API Gateway style event: body is a JSON string.
    resp = intake_handler.handler({"body": json.dumps({"text": text, "actor_id": actor})})
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])


def call_context(intake: dict) -> dict:
    # Over the wire: the context Lambda receives plain JSON, not Python objects.
    event = json.loads(json.dumps({"profile": intake["profile"], "request": intake["request"]}))
    out = context_handler(event)
    ExecutionPlan.model_validate(out["execution_plan"])
    PromptRef.model_validate(out["prompt_ref"])
    return out


@pytest.mark.parametrize("text, task, must_enable, level", [
    ("I'm 26, intermediate investor, high risk tolerance, horizon of 15 years or more, $50,000. "
     "I love AI and crypto. Is my portfolio of NVDA, QQQ and Bitcoin too risky?",
     "risk_review", {"risk"}, "intermediate"),
    ("Balanced profile, intermediate experience, horizon of 8 to 12 years, $150,000. I want to avoid "
     "companies linked to major controversies. Do Enbridge and Exxon pass my ESG screen?",
     "esg_screen", {"esg"}, "intermediate"),
])
def test_intake_lambda_output_feeds_context_lambda(text, task, must_enable, level):
    intake = call_intake(text, "u-test")
    assert intake["status"] == "ready"
    out = call_context(intake)
    plan = out["execution_plan"]
    assert plan["task"] == task
    assert plan["actor_id"] == intake["profile"]["actor_id"] == intake["request"]["actor_id"]
    assert plan["request_id"] == intake["request"]["request_id"]
    assert must_enable <= {a["agent"] for a in plan["enabled_agents"]}
    assert out["prompt_ref"]["experience"] == level


def test_out_of_scope_goes_through_with_no_agents():
    intake = call_intake("I'm cautious, low to moderate risk, 3 to 5 years, $400,000, advanced. "
                         "Buy 100 Tesla shares for me right now.", "u-robert")
    assert intake["status"] == "out_of_scope"
    plan = call_context(intake)["execution_plan"]
    assert plan["enabled_agents"] == [] and plan["run_governance"] is False


def test_clarification_stops_before_context():
    intake = call_intake("Is my portfolio too risky?", "u-unknown")
    assert intake["status"] == "needs_clarification"
    assert intake["clarification_question"]
    assert intake["profile"] is None or intake["request"] is None  # nothing for ④ to resolve
