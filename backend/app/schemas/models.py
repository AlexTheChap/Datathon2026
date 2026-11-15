"""Orchestrator-layer schemas (node ⑤) — the violet JSON nodes of docs/architecture.mmd.

Profile and Request are the CANONICAL schemas owned by the Intake/Plan teams
(app/schemas/profile.py and request.py); this module re-exports them and adds
what only the orchestrator needs: ExecutionPlan (plan-resolver output shape),
OrchestratorInput (AgentCore entrypoint payload, L6) and OrchestratorOutput.
`profile_from_persona` adapts core/personas.json entries to the canonical Profile
(the Intake agent will do this conversion in production).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.profile import AgentToggles, HorizonYears, Language, Profile, RiskBand
from app.schemas.request import PortfolioRef, Request

# Canonical Intake schemas (re-exported so the orchestrator code imports one module).
__all__ = [
    "Profile", "Request", "AnalysisRequest", "PortfolioRef", "RiskBand", "Language",
    "HorizonYears", "AgentToggles", "ExecutionPlan", "PlanAgent",
    "OrchestratorInput", "OrchestratorOutput", "profile_from_persona",
]

# Historical alias: the orchestrator code was written against "AnalysisRequest".
AnalysisRequest = Request


def profile_from_persona(persona: dict[str, Any]) -> Profile:
    """Adapt a core/personas.json entry to the canonical Profile.

    personas.json keeps `horizon_years` as a list [min, max] and nests the
    capital as `simulated_capital.value`; the canonical Profile uses a
    HorizonYears object and a flat `capital` field. Extra descriptive fields
    (age, occupation, test_case…) are dropped — Profile is strict (extra=forbid).
    """
    h_min, h_max = persona.get("horizon_years", [None, None])
    cap = persona.get("simulated_capital") or {}
    return Profile(
        actor_id=f"persona-{persona['id']}",
        risk_band=persona["risk_band"],
        horizon_years=HorizonYears(min=h_min, max=h_max),
        experience=persona.get("experience", "intermediate"),
        language=persona.get("language", "en"),
        objective=persona.get("objective"),
        preferences=persona.get("preferences", []),
        constraints=persona.get("constraints", []),
        exclusions=persona.get("exclusions", []),
        loss_capacity=persona.get("loss_capacity"),
        capital=cap.get("value"),
    )


class PlanAgent(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent: str
    reason: str = ""
    parameters: dict[str, Any] = {}


class ExecutionPlan(BaseModel):
    """Output of the Plan resolver (node ④) — shape of examples/execution_plan.example.json.
    Current phase (orchestrator only): carried as-is in the payload, not yet resolved."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    request_id: str
    actor_id: str
    task: str
    portfolio_ref: PortfolioRef | None = None
    as_of: str | None = None
    enabled_agents: list[PlanAgent] = []
    disabled_agents: list[PlanAgent] = []
    run_governance: bool = True
    run_explainer: bool = True
    intake_confidence: float | None = None


class OrchestratorInput(BaseModel):
    """Payload received by the AgentCore entrypoint (L6): profile + request + portfolio.
    `portfolio_weights`: current weights inline (Intake/upload phase comes later)."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    actor_id: str
    session_id: Optional[str] = None
    language: Language = "en"
    as_of: Optional[str] = None
    profile: Profile
    request: Request
    portfolio_weights: dict[str, float] | None = None


class OrchestratorOutput(BaseModel):
    """Result written by the orchestrator (full RunResult = the Explainer's job, node ⑤)."""

    run_id: str
    status: Literal["DONE", "FAILED"] = "DONE"
    language: Language = "en"
    summary: str = ""
    evidence_ids: list[str] = []
    model: str = ""
    session_id: Optional[str] = None
    duration_ms: int | None = None
    error: str | None = None
