"""Les schémas Pydantic acceptent les exemples de l'équipe et personas.json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.models import AnalysisRequest, ExecutionPlan, OrchestratorInput, Profile

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "backend" / "app" / "schemas" / "examples"
PERSONAS = REPO / "backend" / "app" / "core" / "personas.json"


def test_request_example():
    req = AnalysisRequest.model_validate(json.loads((EXAMPLES / "request.example.json").read_text()))
    assert req.task == "risk_review"
    assert req.actor_id == "sub-xyz"
    assert req.toggles == {"stress_tests": True, "include_rag": False}


def test_execution_plan_example():
    plan = ExecutionPlan.model_validate(json.loads((EXAMPLES / "execution_plan.example.json").read_text()))
    assert [a.agent for a in plan.enabled_agents] == ["risk", "macro_scenarios"]
    assert plan.run_governance and plan.run_explainer


@pytest.mark.parametrize("persona_id", ["amira", "mateo", "robert"])
def test_personas_sont_des_profils_valides(persona_id):
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    p = Profile.model_validate(next(x for x in personas if x["id"] == persona_id))
    assert p.language in ("fr", "en")
    assert len(p.horizon_years) == 2


def test_payload_orchestrator_minimal():
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    inp = OrchestratorInput(
        run_id="run-1", actor_id="a1", language="fr",
        profile=Profile.model_validate(personas[2]),
        request=AnalysisRequest(request_id="r1", actor_id="a1", task="risk_review",
                                user_message="..."),
        portfolio_weights={"XIU.TO": 0.5, "NVDA": 0.5},
    )
    assert inp.profile.risk_band == "faible_a_modere"
    assert inp.portfolio_weights["NVDA"] == 0.5
