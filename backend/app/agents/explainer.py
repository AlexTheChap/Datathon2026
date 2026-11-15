"""Explainer (node ⑤): writes the answer the user reads, from the run's evidence only, then a claim verifier checks it.

Inputs come from CODE, not from the orchestrator's prose: the execution plan, the governance verdict and checks,
the sub-agents' findings and the evidence ledger of the run. The system prompt is the versioned template of the
investor's experience level (`PromptRef` from box 4, sha256-checked by `context.prompt_loader.load_prompt`).
The Explainer has no tools and never computes: every figure it writes must carry an `[ev-…]` citation.

`verifier.verify_answer` then checks every citation against the ledger. An answer with an unknown citation is sent
back once with the list of the ids that do not exist; if it still cites an unknown id, those citations are replaced by
`unverified` and `verification.ok` is False, so nothing unverifiable looks sourced.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.subagents.base import evidence_of
from app.agents.verifier import strip_unknown, verify_answer
from app.context.prompt_loader import load_prompt, resolve_explainer_prompt
from app.core.config import Settings, get_settings
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.models import OrchestratorInput
from app.store.ledger import EvidenceLedger

MAX_CONTEXT_CHARS = 90_000
MAX_REWRITES = 1

LLM = Callable[[str, str], str]      # (system prompt, user message) -> answer text

USER_INSTRUCTIONS = """Write the final answer for the investor, following your template.
Everything you may rely on is in the JSON below. `orchestrator_notes` are working notes, NOT evidence:
use a claim from them only if a finding or an evidence record supports it, and cite that evidence.
Cite with the evidence ids exactly as they appear in `evidence` (short form `[ev-xxxx]` is accepted)."""


@dataclass
class ExplainerResult:
    text: str
    verification: dict[str, Any]
    attempts: int
    model: str
    template: dict[str, str]


def collect(ledger: EvidenceLedger, run_id: str) -> tuple[dict[str, dict], list[dict], dict | None]:
    """(evidence by id, sub-agent findings, last governance result) from the run ledger."""
    evidence: dict[str, dict] = {}
    findings: list[dict] = []
    governance: dict | None = None
    for r in ledger.records(run_id):
        ev = evidence_of(r)
        if ev is None:
            continue
        evidence[ev["evidence_id"]] = ev
        tool_name = ev.get("tool", "")
        if tool_name.endswith("_subagent"):
            findings.append({"agent": tool_name, "evidence_id": ev["evidence_id"],
                             "nested_evidence_ids": ev.get("nested_evidence_ids", []), **(ev.get("output") or {})})
        elif tool_name == "governance_check":
            governance = ev
    return evidence, findings, governance


def _compact_evidence(evidence: dict[str, dict]) -> list[dict]:
    rows = []
    for eid, ev in evidence.items():
        if str(ev.get("tool", "")).endswith("_subagent"):
            continue                                  # their content is in `findings`
        rows.append({"evidence_id": eid, "tool": ev.get("tool"), "say": ev.get("say"),
                     "period": ev.get("period"), "as_of": ev.get("as_of"),
                     "assumptions": (ev.get("assumptions") or [])[:4], "data_sources": ev.get("data_sources")})
    return rows


def _governance_block(governance: dict | None) -> dict | None:
    if governance is None:
        return None
    out = governance.get("output") or governance
    return {"evidence_id": governance.get("evidence_id"), "verdict": out.get("verdict"),
            "checks": [{k: c.get(k) for k in ("id", "status", "value", "limit", "detail", "evidence_id")}
                       for c in out.get("checks", [])],
            "missing_info": out.get("missing_info", [])}


def _plan_block(plan: ExecutionPlan) -> dict:
    return {"task": plan.task, "needs_clarification": plan.needs_clarification, "warnings": plan.warnings,
            "enabled_agents": [{"agent": a.agent, "reason": a.reason, "forced": a.forced} for a in plan.enabled_agents],
            "disabled_agents": [{"agent": a.agent, "reason": a.reason} for a in plan.disabled_agents]}


def build_context(inp: OrchestratorInput, plan: ExecutionPlan, as_of: str, evidence: dict[str, dict],
                  findings: list[dict], governance: dict | None, notes: str) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "profile": inp.profile.model_dump(mode="json"),
        "request": {"task": inp.request.task, "user_message": inp.request.user_message},
        "portfolio_weights": inp.portfolio_weights,
        "execution_plan": _plan_block(plan),
        "governance": _governance_block(governance),
        "findings": findings,
        "evidence": _compact_evidence(evidence),
        "orchestrator_notes": notes,
    }


def bedrock_llm(settings: Settings) -> LLM:
    """Default model call: a tool-less Strands agent on the orchestrator model (Sonnet)."""
    def call(system: str, user: str) -> str:
        from strands import Agent
        from strands.models import BedrockModel
        agent = Agent(model=BedrockModel(model_id=settings.model_orchestrator, region_name=settings.aws_region),
                      system_prompt=system, tools=[], callback_handler=None)
        return str(agent(user))
    return call


def explain(inp: OrchestratorInput, plan: ExecutionPlan, as_of: str, ledger: EvidenceLedger,
            orchestrator_notes: str, settings: Settings | None = None, llm: LLM | None = None) -> ExplainerResult:
    """Explainer + verifier for one run. `llm` can be injected (tests)."""
    s = settings or get_settings()
    ref = inp.prompt_ref or resolve_explainer_prompt(inp.profile)
    template = load_prompt(ref)                     # refuses a template changed since box 4 resolved it
    evidence, findings, governance = collect(ledger, inp.run_id)
    context = build_context(inp, plan, as_of, evidence, findings, governance, orchestrator_notes)
    payload = json.dumps(context, ensure_ascii=False, indent=1, default=str)
    if len(payload) > MAX_CONTEXT_CHARS:
        context["orchestrator_notes"] = ""          # the notes are the first thing to drop
        payload = json.dumps(context, ensure_ascii=False, indent=1, default=str)[:MAX_CONTEXT_CHARS]
    user = f"{USER_INSTRUCTIONS}\n\n```json\n{payload}\n```"
    call = llm or bedrock_llm(s)
    text, attempts = call(template.body, user), 1
    result = verify_answer(text, evidence)
    for _ in range(MAX_REWRITES):
        if result.ok:
            break
        text = call(template.body, f"{user}\n\nYour previous answer cited evidence ids that do not exist: "
                                   f"{result.unknown}. Rewrite the whole answer and cite ONLY ids that appear in "
                                   "`evidence` (or drop the claim).")
        attempts += 1
        result = verify_answer(text, evidence)
    if not result.ok:                               # still unverifiable: never present it as sourced
        text = strip_unknown(text, result.unknown)
    return ExplainerResult(text=text, verification=result.to_dict(), attempts=attempts, model=s.model_orchestrator,
                           template={"key": template.key, "version": template.version, "sha256": template.sha256})
