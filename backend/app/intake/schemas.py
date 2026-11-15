"""Modèles propres à l'Intake (boîte ③ de architecture.mmd).

- `IntakeDraft` : ce que le LLM (ou le backend hors ligne) remplit. Il ne contient AUCUN champ serveur
  (`actor_id`, `request_id`, `schema_version`) : le code les ajoute après, pour que le modèle ne puisse pas
  les inventer. Tous les champs du profil sont optionnels : un texte libre ne dit pas tout.
- `IntakeResult` : ce que `run_intake` renvoie à l'API ② et au Plan resolver ④.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import Benchmark, Exclusion, Position, Profile, Request, Scenario, Task
from app.schemas.profile import Experience, Language, RiskBand


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_band: Optional[RiskBand] = None
    horizon_min_years: Optional[int] = None
    horizon_max_years: Optional[int] = None
    experience: Optional[Experience] = None
    language: Optional[Language] = None
    objective: Optional[str] = None
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    loss_capacity: Optional[float] = Field(default=None, description="Perte tolérée, fraction du capital (0,15 = 15 %).")
    capital: Optional[float] = Field(default=None, description="Capital fictif total, en CAD.")


class RequestDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    positions: list[Position] = Field(default_factory=list)
    capital: Optional[float] = None
    scenarios: list[Scenario] = Field(default_factory=list)
    benchmark: Optional[Benchmark] = None
    stress_tests: bool = False
    include_rag: bool = False
    as_of: Optional[date] = None
    intake_confidence: float = Field(description="Entre 0 et 1 : à quel point la demande est claire.")


class IntakeDraft(BaseModel):
    """Sortie structurée du modèle (Bedrock structured outputs, puis validation Pydantic)."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileDraft
    request: RequestDraft
    clarification_question: Optional[str] = Field(
        default=None, description="Une seule question, seulement si une information indispensable manque."
    )


IntakeStatus = Literal["ready", "needs_clarification", "out_of_scope"]


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IntakeStatus
    profile: Optional[Profile] = None
    request: Optional[Request] = None
    clarification_question: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    backend: str = Field(description="'bedrock' ou 'mock' : qui a produit le brouillon.")
