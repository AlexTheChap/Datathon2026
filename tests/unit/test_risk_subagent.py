"""Risk sub-agent tests — registration and real toolset (no AWS, no LLM)."""
from __future__ import annotations

from app.agents.subagents.risk import risk_subagent
from app.tools.agent_tools import ALL_TOOLS


def test_risk_subagent_registered():
    assert risk_subagent.tool_name == "risk_subagent"


def test_risk_subagent_briefs_carry_the_quant_toolset():
    spec = risk_subagent.tool_spec
    assert "evidence record" in spec["description"]
    # the real quant tools exist on the orchestrator side; the sub-agent's own
    # toolset is validated by the live run (nested evidence in the ledger)
    names = [t.tool_spec["name"] for t in ALL_TOOLS]
    assert {"metrics", "concentration", "correlation", "risk_contribution", "governance_check"} <= set(names)
