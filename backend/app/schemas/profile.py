"""Profile JSON: output of the Intake agent to the Plan resolver and the Prompt loader (links L3, L4, L5).
The `risk_band` values match the keys of `core/rules.yaml`. `experience`, `exclusions`
and `capital` follow `core/personas.json`. All profiles are fictional (simulation only).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

RiskBand = Literal["low", "low_moderate", "moderate", "high"]
Experience = Literal["beginner", "intermediate", "advanced"]
Language = Literal["en"]


class HorizonYears(BaseModel):
    """Investment horizon in years. `None` = unbounded (e.g. 15 years or more)."""

    model_config = ConfigDict(extra="forbid")

    min: Optional[int] = Field(default=None, ge=0)
    max: Optional[int] = Field(default=None, ge=0)


class Exclusion(BaseModel):
    """Exclusion constraint, in the format read by governance (rules.yaml)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["asset_class", "controversy", "esg_activity"]
    value: Optional[str] = Field(default=None, description="E.g.: crypto")
    min_severity: Optional[Literal["minor", "moderate", "major"]] = None


class AgentToggles(BaseModel):
    """Sub-agents the user does (or does not) want to run."""

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
    age: Optional[int] = Field(
        default=None, ge=18, le=120,
        description="Context only. Never mapped to a risk level or an allocation (the case forbids automatic profile-to-asset rules).",
    )
    objective: Optional[str] = None
    preferences_text: Optional[str] = Field(
        default=None, max_length=2000,
        description="The investor's own words for preferences and constraints, kept verbatim.",
    )
    # Derived from `preferences_text` when the profile is saved (app.api.service.put_profile).
    preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    loss_capacity: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Tolerated loss, as a fraction of capital.")
    capital: Optional[float] = Field(default=None, gt=0, description="Simulated capital, in CAD.")
    agent_toggles: AgentToggles = Field(default_factory=AgentToggles)
    wants_to_learn: bool = Field(
        default=False,
        description="The person wants explanations that teach (terms defined, reasoning shown), e.g. a student. "
        "Chosen from what they say or the profile form, never from age or other demographics.",
    )
    portfolio: Optional[dict[str, float]] = Field(
        default=None,
        description="The user's current simulated portfolio (ticker -> weight): the one AlphaForge built from this "
        "profile (web app, My portfolio). A question about \"my portfolio\" that names no asset is analysed on it.",
    )
    portfolio_run_id: Optional[str] = Field(default=None, description="The run that built `portfolio`.")
    agent_toggle_reasons: dict[str, str] = Field(
        default_factory=dict,
        description="Why a toggle was last changed from chat: agent name -> the user's exact words "
        "(e.g. news -> \"I don't care about the news\"). Lets the Explainer quote the user.",
    )

    @field_validator("portfolio")
    @classmethod
    def _weights(cls, v: Optional[dict[str, float]]) -> Optional[dict[str, float]]:
        if v is not None and any(not (0.0 <= w <= 1.0) for w in v.values()):
            raise ValueError("portfolio: every weight must be between 0 and 1")
        return v or None

    @field_validator("agent_toggle_reasons")
    @classmethod
    def _known_agents(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = set(v) - set(AgentToggles.model_fields)
        if unknown:
            raise ValueError(f"agent_toggle_reasons: unknown agents {sorted(unknown)}")
        return v
