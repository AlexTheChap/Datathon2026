"""Pydantic schemas — the violet JSON nodes of docs/architecture.mmd.

Fields aligned with the team's examples (app/schemas/examples/*.json) and with
core/personas.json (the actual profile shape: risk_band, horizon_years,
exclusions…). `extra="allow"`: the examples carry descriptive fields (test,
hypothese…) that we preserve without validating.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RiskBand = Literal["faible", "faible_a_modere", "modere", "eleve"]
Language = Literal["en", "fr"]


class PortfolioRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    portfolio_id: str | None = None
    upload_id: str | None = None


class Profile(BaseModel):
    """Investor profile validated by the Intake (node ③) — shape of personas.json."""

    model_config = ConfigDict(extra="allow")

    id: str
    risk_band: RiskBand
    horizon_years: list[float | None] = Field(min_length=2, max_length=2)
    experience: str | None = None
    objectif: str | None = None
    preferences: list[str] = []
    contraintes: list[str] = []
    exclusions: list[dict[str, Any]] = []
    capacite_perte: float | None = None
    capital_fictif: dict[str, Any] | None = None
    language: Language = "fr"


class AnalysisRequest(BaseModel):
    """Request validated by the Intake — shape of examples/request.example.json."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    request_id: str
    actor_id: str
    task: str
    portfolio_ref: PortfolioRef | None = None
    user_message: str = ""
    parameters: dict[str, Any] = {}
    toggles: dict[str, bool] = {}
    intake_confidence: float | None = None


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
    session_id: str | None = None
    language: Language = "fr"
    as_of: str | None = None
    profile: Profile
    request: AnalysisRequest
    portfolio_weights: dict[str, float] | None = None


class OrchestratorOutput(BaseModel):
    """Result written by the orchestrator (full RunResult = the Explainer's job, node ⑤)."""

    run_id: str
    status: Literal["DONE", "FAILED"] = "DONE"
    language: Language = "fr"
    summary: str = ""
    evidence_ids: list[str] = []
    model: str = ""
    session_id: str | None = None
    duration_ms: int | None = None
    error: str | None = None
