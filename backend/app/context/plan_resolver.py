"""Plan resolver (④, links L4 → L6 of docs/architecture.mmd): task × profile × toggles → ExecutionPlan.

Deterministic code, no LLM and no network call. The rules live in core/plan_registry.yaml; this
module only applies them, in this order, to each sub-agent:

1. task `out_of_scope` → no sub-agent, no governance, the Explainer declines;
2. agent REQUIRED by the task → enabled, even if profile.agent_toggles disables it (forced);
3. forced trigger (e.g. controversy exclusion in the profile → ESG) → enabled (forced);
4. optional trigger (e.g. toggles.stress_tests → macro_scenarios) → enabled, unless the profile
   toggle is false (the profile wins, with a warning);
5. otherwise → disabled, "not needed for task".

    from app.context import resolve_plan
    plan = resolve_plan(profile, request)
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas import TASKS, Profile, Request, Scenario, Task
from ..schemas.execution_plan import AGENTS, AgentName, DisabledAgent, EnabledAgent, ExecutionPlan

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "core" / "plan_registry.yaml"

_ESG_WORD = re.compile(r"\besg\b", re.IGNORECASE)
_ESG_EXCLUSIONS = ("controversy", "esg_activity")

# Named predicates, referenced by `triggers[].trigger` in the registry.
# Each one returns the readable reason (str) when it fires, otherwise None.
Trigger = Callable[[Profile, Request], Optional[str]]

TRIGGERS: dict[str, Trigger] = {
    "stress_tests_requested": lambda p, r: "toggles.stress_tests = true" if r.toggles.stress_tests else None,
    "scenarios_requested": lambda p, r: (
        f"parameters.scenarios = {json.dumps(list(r.parameters.scenarios))}" if r.parameters.scenarios else None
    ),
    "rag_requested": lambda p, r: "toggles.include_rag = true" if r.toggles.include_rag else None,
    "esg_preference": lambda p, r: (
        "profile preference mentions ESG" if any(_ESG_WORD.search(x) for x in p.preferences) else None
    ),
    "esg_exclusion": lambda p, r: (
        "profile exclusion ({}) must be screened".format(
            ", ".join(sorted({e.type for e in p.exclusions if e.type in _ESG_EXCLUSIONS}))
        )
        if any(e.type in _ESG_EXCLUSIONS for e in p.exclusions)
        else None
    ),
}


class _TaskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[AgentName] = Field(default_factory=list)


class _TriggerRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentName
    trigger: str
    forced: bool = False

    @field_validator("trigger")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in TRIGGERS:
            raise ValueError(f"Unknown trigger: {v}. Known: {sorted(TRIGGERS)}")
        return v


class PlanRegistry(BaseModel):
    """Validated content of core/plan_registry.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: int
    updated: Any = None
    min_intake_confidence: float = Field(ge=0.0, le=1.0)
    default_scenarios: list[Scenario]
    tasks: dict[Task, _TaskRule]
    triggers: list[_TriggerRule] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def _all_tasks(cls, v: dict) -> dict:
        missing = set(TASKS) - set(v)
        if missing:
            raise ValueError(f"Tasks missing from the registry: {sorted(missing)}")
        return v


def load_registry(path: Path | str = REGISTRY_PATH) -> PlanRegistry:
    return PlanRegistry.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default_registry() -> PlanRegistry:
    return load_registry(REGISTRY_PATH)


def _parameters(agent: str, request: Request, registry: PlanRegistry) -> dict[str, Any]:
    """Parameters taken from the Request only (the profile travels separately to the Orchestrator)."""
    if agent == "risk" and request.parameters.benchmark:
        return {"benchmark": request.parameters.benchmark}
    if agent == "macro_scenarios":
        return {"scenarios": list(request.parameters.scenarios or registry.default_scenarios)}
    return {}


def resolve_plan(profile: Profile, request: Request, registry: Optional[PlanRegistry] = None) -> ExecutionPlan:
    """Apply the registry to (Profile, Request). Raises ValueError if the actors differ."""
    if profile.actor_id != request.actor_id:
        raise ValueError(
            f"actor_id differs between Profile ({profile.actor_id}) and Request ({request.actor_id})"
        )
    reg = registry or default_registry()
    task = request.task
    toggles = profile.agent_toggles.model_dump()
    warnings: list[str] = []
    enabled: list[EnabledAgent] = []
    disabled: list[DisabledAgent] = []

    needs_clarification = request.intake_confidence < reg.min_intake_confidence
    if needs_clarification:
        warnings.append(
            f"intake_confidence {request.intake_confidence:.2f} < {reg.min_intake_confidence:.2f}: "
            "the explainer must state its assumptions and ask one clarifying question"
        )

    if task == "out_of_scope":
        return ExecutionPlan(
            registry_version=reg.version,
            request_id=request.request_id,
            actor_id=request.actor_id,
            task=task,
            portfolio_ref=request.portfolio_ref,
            as_of=request.as_of,
            enabled_agents=[],
            disabled_agents=[
                DisabledAgent(agent=a, reason="task out_of_scope: no analysis is run") for a in AGENTS
            ],
            run_governance=False,
            run_explainer=True,
            intake_confidence=request.intake_confidence,
            needs_clarification=needs_clarification,
            warnings=warnings,
        )

    required = set(reg.tasks[task].required)
    fired: dict[str, list[tuple[str, bool]]] = {a: [] for a in AGENTS}
    for rule in reg.triggers:
        why = TRIGGERS[rule.trigger](profile, request)
        if why and why not in [w for w, _ in fired[rule.agent]]:
            fired[rule.agent].append((why, rule.forced))

    for agent in AGENTS:  # diagram order → stable plan
        reasons = ([f"required by task {task}"] if agent in required else []) + [w for w, _ in fired[agent]]
        must = agent in required or any(f for _, f in fired[agent])
        wanted = bool(reasons)
        toggle_on = toggles[agent]

        if not wanted:
            disabled.append(DisabledAgent(agent=agent, reason=f"not needed for task {task}"))
        elif toggle_on or must:
            reason = "; ".join(reasons)
            if not toggle_on:
                reason += f" (profile.agent_toggles.{agent} = false ignored)"
                warnings.append(f"{agent} enabled although profile.agent_toggles.{agent} = false: {reasons[0]}")
            enabled.append(
                EnabledAgent(
                    agent=agent,
                    reason=reason,
                    parameters=_parameters(agent, request, reg),
                    forced=not toggle_on,
                )
            )
        else:
            disabled.append(
                DisabledAgent(agent=agent, reason=f"profile.agent_toggles.{agent} = false (overrides {'; '.join(reasons)})")
            )
            warnings.append(f"{agent} requested ({'; '.join(reasons)}) but disabled by profile.agent_toggles.{agent}")

    if request.portfolio_ref is None and task != "news_review":
        warnings.append(f"no portfolio_ref: {task} will run without a portfolio")
    if request.as_of is None:
        warnings.append("as_of not set: tools will use the latest available data")

    return ExecutionPlan(
        registry_version=reg.version,
        request_id=request.request_id,
        actor_id=request.actor_id,
        task=task,
        portfolio_ref=request.portfolio_ref,
        as_of=request.as_of,
        enabled_agents=enabled,
        disabled_agents=disabled,
        run_governance=bool(enabled),
        run_explainer=True,
        intake_confidence=request.intake_confidence,
        needs_clarification=needs_clarification,
        warnings=warnings,
    )
