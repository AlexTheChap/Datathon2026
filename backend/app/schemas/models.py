"""Orchestrator-layer schemas (node ⑤) — the violet JSON nodes of docs/architecture.mmd.

Profile and Request are the CANONICAL schemas owned by the Intake/Plan teams
(app/schemas/profile.py and request.py); this module re-exports them and adds
what only the orchestrator needs: OrchestratorInput (AgentCore entrypoint payload, L6) and
OrchestratorOutput. ExecutionPlan and PromptRef are box 4's canonical schemas (app/schemas/execution_plan.py,
prompt_ref.py), re-exported here.
`profile_from_persona` adapts core/personas.json entries to the canonical Profile
(the Intake agent will do this conversion in production).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.profile import AgentToggles, HorizonYears, Language, Profile, RiskBand
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef
from app.schemas.request import PortfolioRef, Request

# Canonical Intake schemas (re-exported so the orchestrator code imports one module).
__all__ = [
    "Profile", "Request", "AnalysisRequest", "PortfolioRef", "RiskBand", "Language",
    "HorizonYears", "AgentToggles", "ExecutionPlan", "PromptRef",
    "OrchestratorInput", "OrchestratorOutput", "profile_from_persona",
]

# Historical alias: the orchestrator code was written against "AnalysisRequest".
AnalysisRequest = Request


def profile_from_persona(persona: dict[str, Any]) -> Profile:
    """Adapt a core/personas.json entry to the canonical Profile.

    personas.json keeps `horizon_years` as a list [min, max] and nests the
    capital as `simulated_capital.value`; the canonical Profile uses a
    HorizonYears object and a flat `capital` field. Extra descriptive fields
    (occupation, test_case…) are dropped — Profile is strict (extra=forbid).
    """
    h_min, h_max = persona.get("horizon_years", [None, None])
    cap = persona.get("simulated_capital") or {}
    return Profile(
        actor_id=f"persona-{persona['id']}",
        risk_band=persona["risk_band"],
        horizon_years=HorizonYears(min=h_min, max=h_max),
        experience=persona.get("experience", "intermediate"),
        language=persona.get("language", "en"),
        age=persona.get("age"),
        objective=persona.get("objective"),
        preferences_text=persona.get("preferences_text"),
        constraints_text=persona.get("constraints_text"),
        preferences=persona.get("preferences", []),
        constraints=persona.get("constraints", []),
        exclusions=persona.get("exclusions", []),
        loss_capacity=persona.get("loss_capacity"),
        capital=cap.get("value"),
    )


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
    # L6: box 4 plan (which sub-agents run, with their parameters). When absent (local smoke runs),
    # the orchestrator resolves it with the same deterministic code (app.context.resolve_plan).
    execution_plan: Optional[ExecutionPlan] = None
    # L7: Explainer template reference, passed through untouched for the Explainer.
    prompt_ref: Optional[PromptRef] = None


class OrchestratorOutput(BaseModel):
    """Result written by the orchestrator (full RunResult = the Explainer's job, node ⑤)."""

    run_id: str
    status: Literal["DONE", "FAILED"] = "DONE"
    language: Language = "en"
    summary: str = ""                                 # the orchestrator's working draft (the Explainer writes the answer)
    as_of: Optional[str] = None
    memory: Optional[dict[str, Any]] = None           # AgentCore Memory binding of the run (enabled, ids, error)
    evidence_ids: list[str] = []
    model: str = ""
    session_id: Optional[str] = None
    duration_ms: int | None = None
    error: str | None = None
