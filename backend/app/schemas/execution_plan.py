"""ExecutionPlan: output of the Plan resolver to the Orchestrator (link L6 of architecture.mmd).

Produced by `context/plan_resolver.py` (deterministic code, no LLM) from the Profile and the
Request (links L3, L4). Each sub-agent appears exactly once, either in `enabled_agents` or in
`disabled_agents`, with a readable reason: the Explainer uses it to say what was NOT analysed.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import Task
from .request import PortfolioRef

# Sub-agents of the SUB subgraph in architecture.mmd (same names as Profile.agent_toggles).
# Order follows the diagram; the Plan resolver keeps it so that plans are stable.
AgentName = Literal["risk", "news", "esg", "fundamentals", "macro_scenarios", "allocation"]
AGENTS = get_args(AgentName)


class EnabledAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentName
    reason: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    forced: bool = Field(
        default=False,
        description="True when profile.agent_toggles disabled the agent but it is required "
        "(by the task or by a profile exclusion): the toggle is ignored.",
    )


class DisabledAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentName
    reason: str


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    registry_version: int = Field(description="Version of core/plan_registry.yaml that was applied (traceability).")
    request_id: str
    actor_id: str
    task: Task
    portfolio_ref: Optional[PortfolioRef] = None
    as_of: Optional[date] = None
    enabled_agents: list[EnabledAgent] = Field(default_factory=list)
    disabled_agents: list[DisabledAgent] = Field(default_factory=list)
    run_governance: bool
    run_explainer: bool = True
    intake_confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = Field(
        default=False,
        description="intake_confidence below the registry threshold: the run still happens, "
        "but the Explainer states its assumptions and asks one clarifying question.",
    )
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _each_agent_exactly_once(self) -> "ExecutionPlan":
        names = [a.agent for a in self.enabled_agents] + [a.agent for a in self.disabled_agents]
        if sorted(names) != sorted(AGENTS):
            raise ValueError(f"Each sub-agent must appear exactly once; got {names}")
        return self

    @property
    def enabled_names(self) -> list[str]:
        return [a.agent for a in self.enabled_agents]
