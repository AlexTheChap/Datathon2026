"""Profile JSON : sortie de l'Intake agent vers le Plan resolver et le Prompt loader (liens L3, L4, L5).

Les valeurs de `risk_band` correspondent aux clés de `core/rules.yaml`. `experience`, `exclusions`
et `capital` suivent `core/personas.json`. Tous les profils sont fictifs (simulation uniquement).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RiskBand = Literal["faible", "faible_a_modere", "modere", "eleve"]
Experience = Literal["debutante", "intermediaire", "avancee"]
Language = Literal["fr", "en"]


class HorizonYears(BaseModel):
    """Horizon de placement en années. `None` = non borné (ex. 15 ans et plus)."""

    model_config = ConfigDict(extra="forbid")

    min: Optional[int] = Field(default=None, ge=0)
    max: Optional[int] = Field(default=None, ge=0)


class Exclusion(BaseModel):
    """Contrainte d'exclusion, au format lu par la gouvernance (rules.yaml)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["asset_class", "controversy", "esg_activity"]
    value: Optional[str] = Field(default=None, description="Ex. : crypto")
    min_severity: Optional[Literal["mineure", "moderee", "majeure"]] = None


class AgentToggles(BaseModel):
    """Sous-agents que l'utilisateur veut (ou non) voir exécutés."""

    model_config = ConfigDict(extra="forbid")

    risk: bool = True
    news: bool = True
    esg: bool = True
    fundamentals: bool = True
    macro_scenarios: bool = True
    allocation: bool = True


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    actor_id: str
    risk_band: RiskBand
    horizon_years: HorizonYears = Field(default_factory=HorizonYears)
    experience: Experience
    language: Language
    objective: Optional[str] = None
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    loss_capacity: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Perte tolérée, en fraction du capital.")
    capital: Optional[float] = Field(default=None, gt=0, description="Capital fictif, en CAD.")
    agent_toggles: AgentToggles = Field(default_factory=AgentToggles)
