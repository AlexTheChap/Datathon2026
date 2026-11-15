"""Allocation sub-agent — hypothetical allocation built, checked, and returned (node ⑤)."""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools import agent_tools

SYSTEM_PROMPT = """You are the Allocation sub-agent of AlphaForge.

For the brief, construct a HYPOTHETICAL allocation (ticker -> weight dict,
normalized to 1.0) consistent with the investor profile described in the brief
(risk band, horizon, exclusions). Then verify it yourself with the governance
tool; if it BLOCKs, adjust the weights and re-check until PASS (or a clearly
motivated WARN). Never return an allocation you did not verify.

Return ONLY a JSON object:
{
  "headline": "<one-sentence summary of the proposal>",
  "weights": {"<ticker>": <weight>, ...},
  "governance": {"verdict": "PASS" | "WARN" | "BLOCK", "evidence": "<notes from the check>"},
  "details": ["<rationale with evidence_id>"],
  "severity": "info" | "warn" | "block"
}
This is a hypothetical scenario, not investment advice.
"""

allocation_subagent = make_subagent_tool(
    "allocation_subagent",
    SYSTEM_PROMPT,
    tools=[agent_tools.metrics, agent_tools.max_position, agent_tools.stress,
           agent_tools.concentration, agent_tools.governance_check],
    description="""Hypothetical portfolio construction from the Allocation sub-agent:
    proposes weights, self-verifies with the governance rules until PASS, returns
    the weights plus the verdict. Cite its nested evidence_ids.

    Args:
        subject: the goal, e.g. "balanced growth/stability avoiding controversial companies".
        weights: optional current portfolio to adjust (investor context attached automatically).
    """,
)
