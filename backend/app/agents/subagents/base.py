"""Sub-agent factory (node ⑤): one small LLM per domain, called BY the orchestrator.

Every sub-agent follows the AWS agents-as-tools pattern: it is exposed to the
orchestrator as a single tool, runs its own Claude Haiku agent with a domain
toolset, and returns ONE structured finding wrapped in the standard evidence
envelope. Its internal tool calls are recorded in the same run ledger (tagged
with the sub-agent name), so a trace shows: orchestrator -> sub-agent -> tools.

Deterministic context: the orchestrator-facing tool takes a STRUCTURED payload
(subject + optional weights/tickers). The investor profile, portfolio and as_of
are rendered into the brief by CODE (from the run's ContextVars), so briefs are
identical across runs for the same request — the LLM only authors the question.

Output contract: the sub-agent returns a JSON object with at least
{"headline": str, "details": [str], "severity": "info"|"warn"|"block"}.
The envelope adds the stable evidence_id and the nested evidence_ids it produced.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from strands import Agent, tool
from strands.models import BedrockModel

from app.agents.hooks import EvidenceHook
from app.core.config import get_settings
from app.store.ledger import EvidenceLedger
from app.tools import agent_tools
from app.tools.risk_tools import _eid

# Set by the orchestrator for each run so sub-agents write into the same ledger.
run_context: ContextVar[dict[str, Any] | None] = ContextVar("af_subagent_ctx", default=None)


def extract_json(text: str) -> dict:
    """First {...last} JSON object in the text; tolerant of ```json fences and prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in sub-agent output")
    return json.loads(text[start:end + 1])


def build_envelope(tool_name: str, inputs: dict, finding: dict,
                   nested_evidence_ids: list[str], simulated: bool = False) -> dict:
    """Standard evidence envelope so ledger/citations treat the finding like any tool."""
    assumptions = ["Finding synthesized by a domain sub-agent (LLM) from its cited tool evidence."]
    if simulated:
        assumptions.append("Content is SIMULATED (prototype placeholder: no real feed for this domain).")
    return {
        "evidence_id": _eid(tool_name, inputs),
        "tool": tool_name,
        "inputs": inputs,
        "output": finding,
        "nested_evidence_ids": nested_evidence_ids,
        "say": finding.get("headline", ""),
        "assumptions": assumptions,
        "data_sources": ["sub-agent analysis over data/curated tools"],
    }


def render_brief(subject: str, weights: dict[str, float] | None = None,
                 tickers: list[str] | None = None) -> str:
    """Deterministic brief: investor context from the run's ContextVars + the question.
    Identical requests produce identical briefs — variance is limited to `subject`."""
    parts: list[str] = []
    profile = agent_tools.current_profile.get()
    if profile:
        h = profile.get("horizon_years") or {}
        if isinstance(h, dict):
            horizon = f"{h.get('min')}–{h.get('max')}y"
        else:
            horizon = str(h)
        cap = profile.get("capital")
        parts.append(f"Investor: risk={profile.get('risk_band')}, horizon={horizon}"
                     + (f", capital={cap:,.0f} CAD (fictional)" if cap else " (fictional)"))
    if weights:
        parts.append("Portfolio: " + ", ".join(f"{t} {v:.0%}" for t, v in weights.items()))
    if tickers:
        parts.append("Tickers of interest: " + ", ".join(tickers))
    as_of = (run_context.get() or {}).get("as_of")
    if as_of:
        parts.append(f"as_of={as_of} (no data after this date)")
    parts.append(f"Question: {subject}")
    return "\n".join(parts)


def make_subagent_tool(tool_name: str, system_prompt: str, tools: list,
                       simulated: bool = False, description: str | None = None):
    """Create the Strands @tool that runs one sub-agent.

    Args:
        tool_name: e.g. "esg_subagent" (the tool the orchestrator calls).
        system_prompt: the sub-agent's role prompt (forces the JSON contract).
        tools: the domain toolset the sub-agent may use (may be empty).
        simulated: True when the domain has no real data (content is fictional).
        description: docstring for the orchestrator-facing tool.
    """

    def _nested_ids(ledger: EvidenceLedger, run_id: str, before: int) -> list[str]:
        seen: list[str] = []
        for r in ledger.records(run_id)[before:]:
            eid = r.get("evidence_id")
            if eid and eid not in seen:
                seen.append(eid)
        return seen

    def run(subject: str, weights: dict[str, float] | None = None,
            tickers: list[str] | None = None) -> dict:
        inputs = {"subject": subject, "weights": weights, "tickers": tickers}
        brief = render_brief(subject, weights, tickers)
        ctx = run_context.get()
        try:
            s = get_settings()
            agent = Agent(
                model=BedrockModel(model_id=s.model_subagent, region_name=s.aws_region),
                system_prompt=system_prompt,
                tools=tools,
                hooks=[EvidenceHook(ctx["ledger"], ctx["run_id"], agent=tool_name)] if ctx else [],
            )
            before = len(ctx["ledger"].records(ctx["run_id"])) if ctx else 0
            result = agent(brief)
            finding = extract_json(str(result))
            nested = _nested_ids(ctx["ledger"], ctx["run_id"], before) if ctx else []
            return build_envelope(tool_name, inputs, finding, nested, simulated=simulated)
        except (ValueError, json.JSONDecodeError) as e:
            return {"error": f"{tool_name} failed: {e}",
                    "note": "Prototype sub-agent; retry with a simpler brief."}

    run.__name__ = tool_name
    run.__doc__ = description or f"Domain analysis by {tool_name}."
    # Wrap with the Strands decorator last so the spec matches name/docstring.
    return tool(run)
