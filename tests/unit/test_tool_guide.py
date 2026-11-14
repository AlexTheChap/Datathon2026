"""Per-task tool-calling guide — no AWS, no LLM."""
from __future__ import annotations

from app.context.tool_guide import tools_block


def test_known_task_gets_specific_guide():
    g = tools_block("risk_review")
    assert "governance_check" in g and "max_position" in g
    assert g.startswith("### Strategy")


def test_allocation_task_guide():
    g = tools_block("allocation_proposal")
    assert "never BLOCK" in g or "never\n   BLOCK" in g or "PASS" in g


def test_unknown_task_falls_back_to_generic():
    g = tools_block("mystery_task")
    assert g.startswith("### Available tools") and "risk_contribution" in g
