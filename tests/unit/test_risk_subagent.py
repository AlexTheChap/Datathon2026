"""Risk sub-agent unit tests — JSON extraction and evidence envelope (no AWS, no LLM)."""
from __future__ import annotations

import pytest

from app.agents.subagents.risk import SUBAGENT_TOOLS, _envelope, _extract_json


def test_tool_is_registered():
    assert len(SUBAGENT_TOOLS) == 1
    assert SUBAGENT_TOOLS[0].tool_name == "risk_subagent"


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fences_and_prose():
    text = 'Sure! Here it is:\n```json\n{"subject": "NVDA", "severity": "warn"}\n```\nDone.'
    assert _extract_json(text)["subject"] == "NVDA"


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        _extract_json("no json here at all")


def test_envelope_marks_content_as_simulated():
    finding = {"subject": "NVDA", "headline": "H", "severity": "warn"}
    ev = _envelope({"subject": "NVDA"}, finding)
    assert ev["tool"] == "risk_subagent" and ev["evidence_id"].startswith("ev-")
    assert ev["say"] == "H"
    assert any("SIMULATED" in a for a in ev["assumptions"])
    # stable id: same inputs -> same evidence id (same scheme as risk_tools)
    assert ev["evidence_id"] == _envelope({"subject": "NVDA"}, {"x": 1})["evidence_id"]
