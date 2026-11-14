"""Node ⑤ (first sub-agent) — Risk sub-agent called BY the orchestrator.

Demonstrates the AWS "agents-as-tools" supervisor pattern: the orchestrator LLM
calls `risk_subagent` like any tool; the tool runs a small specialized agent
(Claude Haiku via Bedrock) and returns its finding wrapped in the standard
evidence envelope, so it lands in the ledger like every other call (L15).

IMPORTANT — prototype scope: the sub-agent returns FICTITIOUS risk data
("fake data of subject X"). Its prompt forces simulated content, clearly
labeled, never presented as real market data. The production version will call
real quant tools instead of inventing numbers (see docs/architecture.mmd: the
full design is the Strands graph with parallel sub-agent nodes).
"""
from __future__ import annotations

import json

from strands import Agent, tool
from strands.models import BedrockModel

from app.core.config import get_settings
from app.tools.risk_tools import _eid  # same stable evidence-id scheme as quant tools

SYSTEM_PROMPT = """You are the Risk sub-agent of AlphaForge (datathon prototype).

Given a subject (a ticker, a theme, or a portfolio question), you produce a
SHORT qualitative risk briefing. Everything you write is FICTITIOUS: this
prototype has no live risk-data feed yet, so you invent plausible, clearly
simulated content — never present it as real market data.

Return ONLY a JSON object, no prose around it, with exactly these keys:
{
  "subject": "<the subject you were given>",
  "headline": "<one-sentence simulated risk headline>",
  "severity": "info" | "warn" | "block",
  "simulated_metrics": [{"name": "<metric>", "value": <number>, "unit": "<unit>"}],
  "caveats": ["<at least one caveat stating the data is simulated>"]
}
"""


def _build_agent() -> Agent:
    s = get_settings()
    return Agent(
        model=BedrockModel(model_id=s.model_subagent, region_name=s.aws_region),
        system_prompt=SYSTEM_PROMPT,
    )


def _extract_json(text: str) -> dict:
    """First {...last} JSON object in the text; tolerant of ```json fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in sub-agent output")
    return json.loads(text[start:end + 1])


def _envelope(inputs: dict, finding: dict) -> dict:
    """Standard evidence envelope so the ledger/citations treat it like any tool."""
    return {
        "evidence_id": _eid("risk_subagent", inputs),
        "tool": "risk_subagent",
        "inputs": inputs,
        "output": finding,
        "say": finding.get("headline", ""),
        "assumptions": ["Sub-agent output is SIMULATED (prototype): not real market data."],
        "data_sources": ["Risk sub-agent (LLM, fictional content)"],
    }


@tool
def risk_subagent(subject: str) -> dict:
    """Qualitative risk briefing on a subject (ticker, theme, portfolio question)
    from the Risk sub-agent. Returns an evidence record whose content is
    SIMULATED (prototype placeholder for the future risk-data feed) — say so
    whenever you use it, and never mix its numbers with the real quant tools.

    Args:
        subject: e.g. "NVDA", "concentration in tech", "retirement horizon risk".
    """
    inputs = {"subject": subject}
    try:
        result = _build_agent()(f"Subject: {subject}")
        finding = _extract_json(str(result))
        return _envelope(inputs, finding)
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"risk_subagent failed: {e}",
                "note": "Simulated prototype; retry with a simpler subject."}


SUBAGENT_TOOLS = [risk_subagent]
