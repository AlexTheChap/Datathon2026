"""Risk sub-agent — the first node of the sub-agent layer (see base.py).

Uses the real quant toolset and returns a grounded risk finding. The tools come from
app.tools.toolsets.quant_toolset at each run: the AgentCore Gateway (MCP, box ⑥, L12) when
AF_GATEWAY_URL is set, otherwise the same RiskTools functions in-process.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools.toolsets import quant_toolset

SYSTEM_PROMPT = """You are the Risk sub-agent of AlphaForge (datathon prototype).

Using ONLY the quantitative tools available to you, analyze the brief and
produce a concise risk finding. Call at least one tool before answering; every
number you state must come from a tool result.

Batch your calls: every input comes from the brief, so make the independent calls
together in ONE turn (e.g. metrics + correlation + risk_contribution + concentration
and any market graph lookups at once). Only make a second turn for a call whose
arguments depended on a previous result.
Two tools describe the future without forecasting returns:
- `projection`: the range of possible portfolio values after N years under the official 2026 planning
  guidelines. Report the P10–P90 range and the chance of ending below the start (never a single value), say
  it is a simulation, not a forecast, and keep its warnings (e.g. a horizon under 10 years).
- `vol_forecast`: whether an asset's volatility (risk) is rising, stable or easing (GARCH forecast vs the last
  3 months). It says nothing about the direction of returns.

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
    tools=quant_toolset,   # resolved per run: Gateway (MCP) or local functions
    description="""Quantitative risk analysis (volatility, concentration,
    risk contribution, volatility trend, range of possible future values under the
    planning guidelines) from the Risk sub-agent, grounded
    in real curated data. Returns an evidence record whose nested_evidence_ids are
    the underlying tool evidence — cite them, never recompute numbers yourself.

    Args:
        subject: the precise question, e.g. "NVDA at 20%: risk profile and maximum weight respecting a -15% loss limit".
        weights: optional portfolio weights to analyze (investor context attached automatically).
        tickers: optional assets of particular interest.
    """,
)
