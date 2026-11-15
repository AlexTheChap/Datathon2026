"""Request JSON: output of the Intake agent to the Plan resolver (links L3 and L4 of architecture.mmd).
Validated by Bedrock structured outputs, then by Pydantic. The allowed values of `task`,
`scenarios` and `benchmark` are in `enums.py`.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import Benchmark, Scenario, Task


class PortfolioRef(BaseModel):
    """Reference to the portfolio. The file itself stays in S3 (never read by the Intake)."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: str
    upload_id: Optional[str] = None


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: list[Scenario] = Field(default_factory=list)
    benchmark: Optional[Benchmark] = None


class Toggles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stress_tests: bool = False
    include_rag: bool = False


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    request_id: str
    actor_id: str
    task: Task
    portfolio_ref: Optional[PortfolioRef] = None
    user_message: str
    parameters: Parameters = Field(default_factory=Parameters)
    toggles: Toggles = Field(default_factory=Toggles)
    as_of: Optional[date] = Field(default=None, description="Analysis date; no data published after it is read.")
    intake_confidence: float = Field(ge=0.0, le=1.0)
