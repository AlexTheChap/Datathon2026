"""Macro & scenarios sub-agent — rates, inflation, macro context (architecture node ⑤)."""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools import agent_tools
from app.tools.analysis_tools import macro_snapshot, rate_sensitivity

SYSTEM_PROMPT = """You are the Macro & scenarios sub-agent of AlphaForge.

Using ONLY the tools available to you (macro snapshot, rate sensitivity, crisis
stress replay), describe the macro environment relevant to the brief and, when
asked, what a scenario (e.g. the 2022 rate shock) would do to a portfolio.
Every number must come from a tool result.

Return ONLY a JSON object:
{
  "headline": "<one-sentence macro takeaway>",
  "details": ["<fact with its evidence_id>"],
  "severity": "info" | "warn" | "block",
  "scenarios": [{"name": "<e.g. 2022 replay>", "outcome": "<with evidence_id>"}]
}
No forecasts: historical scenarios only. No investment advice.
"""

macro_subagent = make_subagent_tool(
    "macro_subagent",
    SYSTEM_PROMPT,
    tools=[macro_snapshot, rate_sensitivity, agent_tools.correlation],
    description="""Macro context and scenario analysis (policy rates, yields, inflation,
    rate sensitivity, 2022-style replays) from the Macro sub-agent, grounded in
    Bank of Canada / FRED curated data. Cite its nested evidence_ids.

    Args:
        subject: e.g. "current rate environment and a 2022-style shock for this portfolio".
        weights: optional portfolio weights to stress (investor context attached automatically).
        tickers: optional assets of particular interest, e.g. ["XBB.TO", "TLT"].
    """,
)
