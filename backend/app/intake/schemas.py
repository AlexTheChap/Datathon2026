"""Intake-specific models (box 3 of architecture.mmd).

- `IntakeDraft`: what the LLM (or the offline backend) fills in. It holds NO server-side field
  (`actor_id`, `request_id`, `schema_version`): the code adds them afterwards, so the model cannot make
  them up. Every profile field is optional: free text never says everything.
- `IntakeResult`: what `run_intake` returns to the API (box 2) and the Plan resolver (box 4).
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import Benchmark, Exclusion, Position, Profile, Request, Task
from app.schemas.execution_plan import AgentName
from app.schemas.profile import Experience, Language, RiskBand


class AgentToggleChange(BaseModel):
    """The user explicitly switched a kind of analysis on or off ("I don't care about the news").

    Saved in the profile (it lasts until the user changes it again). `quote` must be the user's exact
    words: the code drops any change whose quote is not found in the message (no invented toggles).
    """

    model_config = ConfigDict(extra="forbid")

    agent: AgentName
    enabled: bool
    quote: str = Field(description="The user's exact words that express this choice, copied verbatim.")


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
    loss_capacity: Optional[float] = Field(default=None, description="Tolerated loss, as a fraction (0.15 = 15%).")
    capital: Optional[float] = Field(default=None, description="Total simulated capital, in CAD.")
    agent_toggle_changes: list[AgentToggleChange] = Field(
        default_factory=list, description="Only analyses the user explicitly turns on or off in this message.")


class RequestDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    positions: list[Position] = Field(default_factory=list)
    capital: Optional[float] = None
    benchmark: Optional[Benchmark] = None
    stress_tests: bool = False
    include_rag: bool = False
    as_of: Optional[date] = None
    intake_confidence: float = Field(description="Between 0 and 1: how clear the request is.")


class IntakeDraft(BaseModel):
    """Structured model output (Bedrock structured outputs, then Pydantic validation)."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileDraft
    request: RequestDraft
    clarification_question: Optional[str] = Field(
        default=None, description="A single question, only if essential information is missing."
    )


IntakeStatus = Literal["ready", "needs_clarification", "out_of_scope"]


class IntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IntakeStatus
    profile: Optional[Profile] = None
    request: Optional[Request] = None
    clarification_question: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    toggle_changes: list[AgentToggleChange] = Field(
        default_factory=list, description="Toggle changes applied (and saved) from this message, for the UI.")
    warnings: list[str] = Field(default_factory=list)
    backend: str = Field(description="'bedrock' or 'mock': which backend produced the draft.")
