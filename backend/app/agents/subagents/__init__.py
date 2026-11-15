"""Sub-agent registry (node ⑤): the domain analysts the orchestrator can dispatch.

Keys match `schemas.profile.AgentToggles` fields, so the orchestrator can honor
the investor's toggles (plan-resolver behavior) by filtering this registry.
"""
from __future__ import annotations

from typing import Any

from app.agents.subagents.allocation import allocation_subagent
from app.agents.subagents.esg import esg_subagent
from app.agents.subagents.fundamentals import fundamentals_subagent
from app.agents.subagents.macro import macro_subagent
from app.agents.subagents.news import news_subagent
from app.agents.subagents.risk import risk_subagent

# AgentToggles field name -> orchestrator-facing tool
REGISTRY: dict[str, Any] = {
    "risk": risk_subagent,
    "macro_scenarios": macro_subagent,
    "esg": esg_subagent,
    "fundamentals": fundamentals_subagent,
    "allocation": allocation_subagent,
    "news": news_subagent,
}

ALL_SUBAGENTS = list(REGISTRY.values())


def subagents_for_toggles(toggles: Any) -> list:
    """Sub-agents enabled by the profile's agent_toggles (all by default/None)."""
    if toggles is None:
        return ALL_SUBAGENTS
    return [tool for key, tool in REGISTRY.items() if getattr(toggles, key, True)]
