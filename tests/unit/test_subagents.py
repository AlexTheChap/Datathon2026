"""Sub-agent factory tests — no AWS, no LLM."""
from __future__ import annotations

import pytest

from app.agents.subagents import ALL_SUBAGENTS, REGISTRY, subagents_for_toggles
from app.agents.subagents.base import build_envelope, extract_json
from app.schemas.profile import AgentToggles


def test_registry_matches_agent_toggles_fields():
    assert set(REGISTRY) == {"risk", "macro_scenarios", "esg", "fundamentals", "allocation", "news"}
    assert len(ALL_SUBAGENTS) == 6
    for tool in ALL_SUBAGENTS:
        assert tool.tool_name.endswith("_subagent")


def test_toggles_filter_subagents():
    assert len(subagents_for_toggles(None)) == 6
    assert len(subagents_for_toggles(AgentToggles())) == 6
    only_risk = subagents_for_toggles(
        AgentToggles(risk=True, news=False, esg=False, fundamentals=False,
                     macro_scenarios=False, allocation=False))
    assert [t.tool_name for t in only_risk] == ["risk_subagent"]
    no_news = subagents_for_toggles(AgentToggles(news=False))
    assert "news_subagent" not in [t.tool_name for t in no_news]
    assert len(no_news) == 5


def test_extract_json_plain_and_fenced():
    assert extract_json('{"a": 1}') == {"a": 1}
    text = 'Sure! Here it is:\n```json\n{"subject": "NVDA", "severity": "warn"}\n```\nDone.'
    assert extract_json(text)["subject"] == "NVDA"
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_envelope_simulated_flag_and_stable_id():
    finding = {"headline": "H", "details": [], "severity": "warn"}
    ev = build_envelope("risk_subagent", {"brief": "x"}, finding, ["ev-1", "ev-2"])
    assert ev["evidence_id"].startswith("ev-")
    assert ev["say"] == "H"
    assert ev["nested_evidence_ids"] == ["ev-1", "ev-2"]
    assert all("SIMULATED" not in a for a in ev["assumptions"])
    sim = build_envelope("news_subagent", {"brief": "x"}, finding, [], simulated=True)
    assert any("SIMULATED" in a for a in sim["assumptions"])
    assert ev["evidence_id"] == build_envelope("risk_subagent", {"brief": "x"}, {"other": 1}, [])["evidence_id"]
