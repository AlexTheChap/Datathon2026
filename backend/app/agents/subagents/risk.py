"""Risk sub-agent — the first node of the sub-agent layer (see base.py).

Uses the real quant toolset (RiskTools via agent_tools) and returns a grounded
risk finding. Replaces the earlier SIMULATED placeholder now that tools are wired.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools import agent_tools

SYSTEM_PROMPT = """You are the Risk sub-agent of AlphaForge (datathon prototype).

Using ONLY the quantitative tools available to you, analyze the brief and
produce a concise risk finding. Call at least one tool before answering; every
number you state must come from a tool result.

Return ONLY a JSON object, no prose around it, with exactly these keys:
{
  "headline": "<one-sentence risk takeaway>",
  "details": ["<fact with its evidence_id>", "<fact with its evidence_id>"],
  "severity": "info" | "warn" | "block",
  "key_numbers": [{"metric": "<name>", "value": <number>, "evidence_id": "ev-..."}]
}
Keep it under 8 lines. No investment advice.
"""

risk_subagent = make_subagent_tool(
    "risk_subagent",
    SYSTEM_PROMPT,
    tools=[agent_tools.metrics, agent_tools.stress, agent_tools.concentration,
           agent_tools.correlation, agent_tools.risk_contribution, agent_tools.max_position],
    description="""Quantitative risk analysis (volatility, crisis stress, concentration,
    risk contribution, calibrated position limits) from the Risk sub-agent, grounded
    in real curated data. Returns an evidence record whose nested_evidence_ids are
    the underlying tool evidence — cite them, never recompute numbers yourself.

    Args:
        brief: e.g. "NVDA at 20% in Robert's portfolio", "concentration check on tech- heavy allocation".
    """,
)
