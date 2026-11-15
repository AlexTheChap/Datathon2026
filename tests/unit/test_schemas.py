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
    assert req.toggles.include_rag is False


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
# ---------- Profile: age and preferences_text (contract change) ----------
from pydantic import ValidationError  # noqa: E402

_BASE = {"actor_id": "u1", "risk_band": "moderate", "experience": "beginner", "language": "en"}


@pytest.mark.parametrize("age", [18, 59, 120])
def test_profile_age_accepts_adult_ages(age):
    assert Profile(**_BASE, age=age).age == age


@pytest.mark.parametrize("age", [17, 0, 121, -3])
def test_profile_age_outside_18_to_120_is_rejected(age):
    with pytest.raises(ValidationError):
        Profile(**_BASE, age=age)


def test_profile_preferences_text_is_limited_to_2000_characters_and_kept_verbatim():
    text = "  Some diversification,\nnot only tech.  "
    assert Profile(**_BASE, preferences_text=text).preferences_text == text
    assert Profile(**_BASE, preferences_text="x" * 2000)
    with pytest.raises(ValidationError):
        Profile(**_BASE, preferences_text="x" * 2001)


def test_profile_saved_before_age_and_preferences_text_still_loads():
    old = {**_BASE, "preferences": ["ETFs"], "constraints": ["no crypto"]}   # what older stores contain
    p = Profile.model_validate_json(json.dumps(old))
    assert p.age is None and p.preferences_text is None and p.preferences == ["ETFs"]


def test_profile_example_has_age_and_preferences_text():
    prof = Profile.model_validate(json.loads((EXAMPLES / "profile.example.json").read_text()))
    assert prof.age == 59 and "publicized company" in prof.preferences_text


@pytest.mark.parametrize("persona_id, age", [("amira", 26), ("mateo", 41), ("robert", 59)])
def test_personas_carry_age_and_preferences_text(persona_id, age):
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    p = profile_from_persona(next(x for x in personas if x["id"] == persona_id))
    assert p.age == age and p.preferences_text


def test_robert_can_be_tempted_by_a_publicized_company():
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    p = profile_from_persona(next(x for x in personas if x["id"] == "robert"))
    assert "heavily publicized company" in p.preferences_text
    assert any("publicized company" in x for x in p.preferences + p.constraints)
