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
Output contract (validated by CODE, see `validate_finding`): the sub-agent returns a JSON object with at least
{"headline": str, "details": [str], "severity": "info"|"warn"|"block"}.
Every `ev-…` id the finding cites must be one of the evidence ids produced by the sub-agent's own tool calls,
and a sub-agent that has tools must have called at least one. A rejected answer is sent back to the model once
with the reasons; if it is still rejected, the tool returns an error (never an unverified finding).
The envelope adds the stable evidence_id and the nested evidence_ids it produced.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, Literal

from app.agents.hooks import EvidenceHook
from app.agents.verifier import verify_answer
from app.core.config import get_settings
from app.store.ledger import EvidenceLedger, InMemoryLedger
from app.tools.context import current_profile
from app.tools.risk_tools import _eid
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from strands import Agent, tool
from strands.models import BedrockModel, CacheConfig

# Set by the orchestrator for each run so sub-agents write into the same ledger.
run_context: ContextVar[dict[str, Any] | None] = ContextVar("af_subagent_ctx", default=None)

EVIDENCE_ID = re.compile(r"\bev-[0-9a-f]{10}\b")
MAX_REPAIRS = 1          # one second chance for a rejected answer


class FindingError(ValueError):
    """The sub-agent's answer does not respect the output contract."""


class Finding(BaseModel):
    """Minimum contract of every sub-agent finding (extra keys are kept: each domain adds its own)."""
    model_config = ConfigDict(extra="allow")
    headline: str = Field(min_length=1)
    details: list[str] = Field(default_factory=list)
    severity: Literal["info", "warn", "block"]


def extract_json(text: str) -> dict:
    """First {...last} JSON object in the text; tolerant of ```json fences and prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in sub-agent output")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in sub-agent output: {e}") from e


def cited_ids(finding: dict) -> list[str]:
    """Every evidence id mentioned anywhere in the finding, in order of appearance."""
    return list(dict.fromkeys(EVIDENCE_ID.findall(json.dumps(finding, ensure_ascii=False))))


def check_figures(details: list[str], evidence: dict[str, dict]) -> None:
    """Every percentage or dollar amount in `details` must carry an evidence id and appear in that evidence:
    a sub-agent never computes a figure of its own (same check as the claim verifier, line by line)."""
    for line in details:
        text = EVIDENCE_ID.sub(lambda m: f"[{m.group(0)}]", re.sub(rf"\[({EVIDENCE_ID.pattern})\]", r"\1", line))
        res = verify_answer(text, evidence, split_sentences=False)
        if res.uncited_numbers:
            raise FindingError(f"this detail states a figure without an evidence_id: {line[:160]!r}")
        if res.unmatched_numbers:
            bad = res.unmatched_numbers[0]
            raise FindingError(f"the figure {bad['number']} does not appear in the evidence cited by: "
                               f"{line[:160]!r}. Use only numbers returned by your tools")


def validate_finding(raw: dict, nested_ids: list[str], requires_evidence: bool = True,
                     evidence: dict[str, dict] | None = None) -> dict:
    """Contract check: schema, no unknown citation, evidence behind the finding, figures that come from the
    cited evidence (when `evidence`, the sub-agent's own tool results, is given). Returns the finding."""
    try:
        finding = Finding.model_validate(raw)
    except ValidationError as e:
        fields = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors(include_url=False))
        raise FindingError(f"the JSON does not match the contract ({fields})") from e
    if requires_evidence and not nested_ids:
        raise FindingError("you answered without calling any tool: call the tools, then cite their evidence_id")
    unknown = [i for i in cited_ids(raw) if i not in nested_ids]
    if unknown:
        raise FindingError(f"you cited evidence ids that none of your tool calls produced: {unknown}; "
                           "cite only the evidence_id values returned by your tools")
    if evidence:
        check_figures(finding.details, evidence)
    return {**raw, "severity": finding.severity}


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


def _horizon_text(h: Any) -> str:
    """"3–5y", "15y+" (no upper bound), "up to 5y" (no lower bound)."""
    if not isinstance(h, dict):
        return str(h)
    lo, hi = h.get("min"), h.get("max")
    if lo is not None and hi is not None:
        return f"{lo}–{hi}y"
    if lo is not None:
        return f"{lo}y+"
    if hi is not None:
        return f"up to {hi}y"
    return "unspecified"


def render_brief(subject: str, weights: dict[str, float] | None = None,
                 tickers: list[str] | None = None) -> str:
    """Deterministic brief: investor context from the run's ContextVars + the question.
    Identical requests produce identical briefs — variance is limited to `subject`."""
    parts: list[str] = []
    profile = current_profile.get()
    if profile:
        cap = profile.get("capital")
        parts.append(f"Investor: risk={profile.get('risk_band')}, horizon={_horizon_text(profile.get('horizon_years'))}"
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


def evidence_of(record: dict) -> dict | None:
    """Evidence envelope stored in a ledger record (payload is a dict or a list of content blocks)."""
    payload = record.get("payload")
    if isinstance(payload, dict) and "evidence_id" in payload:
        return payload
    if isinstance(payload, list):
        return next((b for b in payload if isinstance(b, dict) and "evidence_id" in b), None)
    return None


def own_records(ledger: EvidenceLedger, run_id: str, agent: str, before: int) -> list[dict]:
    """Ledger records written by THIS sub-agent after position `before` (other sub-agents may run in parallel)."""
    return [r for r in ledger.records(run_id)[before:] if r.get("agent") == agent]


def _resolve(value: Any) -> Any:
    """A setting given as a zero-argument callable is evaluated at call time (see make_subagent_tool)."""
    return value() if callable(value) and not isinstance(value, (list, tuple)) else value


def make_subagent_tool(tool_name: str, system_prompt: Any, tools: Any,
                       simulated: Any = False, description: str | None = None,
                       extra_validator: Callable[[dict, list[dict]], None] | None = None):
    """Create the Strands @tool that runs one sub-agent.
    Args:
        tool_name: e.g. "esg_subagent" (the tool the orchestrator calls).
        system_prompt: the sub-agent's role prompt (forces the JSON contract).
        tools: the domain toolset the sub-agent may use (may be empty). May also contain a Strands ToolProvider
            such as an MCPClient (AgentCore Gateway, L12).
        simulated: True when the domain has no real data (content is fictional).
        description: docstring for the orchestrator-facing tool.
        extra_validator: optional domain check `(finding, own ledger records)`; raises FindingError.

    system_prompt / tools / simulated may each be a zero-argument callable, resolved at every call: the choice
    between AWS (Gateway, Knowledge Base) and the local fallback then follows the current settings, and importing
    this module never opens a connection.
    """
    def run(subject: str, weights: dict[str, float] | None = None,
            tickers: list[str] | None = None) -> dict:
        inputs = {"subject": subject, "weights": weights, "tickers": tickers}
        ctx = run_context.get()
        ledger: EvidenceLedger = ctx["ledger"] if ctx else InMemoryLedger()
        run_id: str = ctx["run_id"] if ctx else "local"
        try:
            s = get_settings()
            is_simulated = bool(_resolve(simulated))
            toolset = _resolve(tools)
            agent = Agent(
                # cache_config: the system prompt and tool specs are static per sub-agent — Bedrock
                # caches that prefix instead of re-reading it on every fork (latency and cost).
                model=BedrockModel(model_id=s.model_subagent, region_name=s.aws_region,
                                   cache_config=CacheConfig(strategy="auto")),
                system_prompt=_resolve(system_prompt),
                tools=toolset,
                hooks=[EvidenceHook(ledger, run_id, agent=tool_name)],
                callback_handler=None,     # sub-agents run in parallel: their streams would interleave in the logs
            )
            before = len(ledger.records(run_id))
            message, last_error = render_brief(subject, weights, tickers), ""
            for _ in range(MAX_REPAIRS + 1):
                result = agent(message)
                own = own_records(ledger, run_id, tool_name, before)
                nested = list(dict.fromkeys(r["evidence_id"] for r in own if r.get("evidence_id")))
                try:
                    evidence = {ev["evidence_id"]: ev for r in own if (ev := evidence_of(r))}
                    finding = validate_finding(extract_json(str(result)), nested, requires_evidence=bool(toolset),
                                               evidence=evidence)
                    if extra_validator:
                        extra_validator(finding, own)
                except (FindingError, ValueError) as e:      # FindingError is a ValueError
                    last_error = str(e)
                    message = (f"Your answer was rejected: {last_error}. Fix it and return ONLY the corrected "
                               "JSON object (call tools again if you need to).")
                    continue
                return build_envelope(tool_name, inputs, finding, nested, simulated=is_simulated)
            return {"error": f"{tool_name} returned no valid finding: {last_error}",
                    "note": "The finding was rejected by the output contract; do not use it."}
        except Exception as e:  # noqa: BLE001  Bedrock throttling/access errors, tool failures…: visible, never silent
            return {"error": f"{tool_name} failed: {type(e).__name__}: {e}",
                    "note": "Prototype sub-agent; retry with a simpler brief."}

    run.__name__ = tool_name
    run.__doc__ = description or f"Domain analysis by {tool_name}."
    # Wrap with the Strands decorator last so the spec matches name/docstring.
    return tool(run)


EVENT_ID = re.compile(r"\bN-[0-9a-f]{10}\b")


def event_validator(finding: dict, own: list[dict]) -> None:
    """News-domain check: every news event id (N-…) in the finding was returned by a `news_search` call
    of this sub-agent, so no headline can be invented."""
    returned: set[str] = set()
    for r in own:
        ev = evidence_of(r)
        if ev and ev.get("tool") == "news_search":
            returned |= {e["event_id"] for e in (ev.get("output") or {}).get("events", [])}
    unknown = [i for i in dict.fromkeys(EVENT_ID.findall(json.dumps(finding, ensure_ascii=False))) if i not in returned]
    if unknown:
        raise FindingError(f"you cited news event ids that news_search did not return: {unknown}")
