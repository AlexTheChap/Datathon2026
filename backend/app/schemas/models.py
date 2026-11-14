"""Schémas Pydantic — les nœuds violets du schéma docs/architecture.mmd.

Champs alignés sur les exemples de l'équipe (app/schemas/examples/*.json) et sur
core/personas.json (le profil réel : risk_band, horizon_years, exclusions…).
`extra="allow"` : les exemples contiennent des champs descriptifs (test, hypothese…)
que l'on conserve sans les valider.
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
    """Profil investisseur validé par l'Intake (nœud ③) — forme de personas.json."""

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
    """Requête validée par l'Intake — forme de examples/request.example.json."""

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
    """Sortie du Plan resolver (nœud ④) — forme de examples/execution_plan.example.json.
    Phase actuelle (orchestrateur seul) : porté tel quel dans le payload, pas encore résolu."""

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
    """Payload reçu par l'entrypoint AgentCore (L6) : profil + requête + portefeuille.
    `portfolio_weights` : pondérations actuelles en ligne (phase Intake/upload plus tard)."""

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
    """Résultat écrit par l'orchestrateur (RunResult complet = travail de l'Explainer, nœud ⑤)."""

    run_id: str
    status: Literal["DONE", "FAILED"] = "DONE"
    language: Language = "fr"
    summary: str = ""
    evidence_ids: list[str] = []
    model: str = ""
    session_id: str | None = None
    duration_ms: int | None = None
    error: str | None = None
