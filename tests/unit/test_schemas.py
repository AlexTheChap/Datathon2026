"""Schemas: the team's examples validate against the canonical models."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.models import (AnalysisRequest, ExecutionPlan, OrchestratorInput, Profile,
                                profile_from_persona)
from app.schemas.request import Request

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "backend" / "app" / "schemas" / "examples"
PERSONAS = REPO / "backend" / "app" / "core" / "personas.json"


def test_request_example():
    req = Request.model_validate(json.loads((EXAMPLES / "request.example.json").read_text()))
    assert req.task == "risk_review"
    assert req.actor_id == "sub-xyz"
    assert req.toggles.stress_tests is True and req.toggles.include_rag is False


def test_profile_example():
    prof = Profile.model_validate(json.loads((EXAMPLES / "profile.example.json").read_text()))
    assert prof.risk_band == "low_moderate"
    assert (prof.horizon_years.min, prof.horizon_years.max) == (3, 5)
    assert prof.exclusions[0].value == "crypto"


def test_execution_plan_example():
    plan = ExecutionPlan.model_validate(json.loads((EXAMPLES / "execution_plan.example.json").read_text()))
    assert [a.agent for a in plan.enabled_agents] == ["risk"]
    assert plan.run_governance and plan.run_explainer


@pytest.mark.parametrize("persona_id", ["amira", "mateo", "robert"])
def test_personas_convert_to_valid_profiles(persona_id):
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    p = profile_from_persona(next(x for x in personas if x["id"] == persona_id))
    assert p.language == "en"
    assert p.horizon_years.min is not None
    assert p.capital is not None


def test_robert_conversion_carries_exclusion_and_capital():
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    p = profile_from_persona(next(x for x in personas if x["id"] == "robert"))
    assert p.risk_band == "low_moderate"
    assert p.exclusions[0].type == "asset_class" and p.exclusions[0].value == "crypto"
    assert p.capital == 400000


def test_minimal_orchestrator_payload():
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    profile = profile_from_persona(next(x for x in personas if x["id"] == "robert"))
    inp = OrchestratorInput(
        run_id="run-1", actor_id=profile.actor_id, language="en",
        profile=profile,
        request=AnalysisRequest(request_id="r1", actor_id=profile.actor_id,
                                task="risk_review", user_message="...", intake_confidence=0.9),
        portfolio_weights={"XIU.TO": 0.5, "NVDA": 0.5},
    )
    assert inp.profile.risk_band == "low_moderate"
    assert inp.portfolio_weights["NVDA"] == 0.5
