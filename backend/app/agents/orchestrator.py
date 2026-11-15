"""Node ⑤ — Main orchestrator agent (Claude Sonnet via Bedrock).

Builds the Strands Agent for ONE run: prompt template (L5/L7), quant tools
(L12), evidence ledger (L15), optional AgentCore Memory (L17) via AF_MEMORY_ID.
`run_orchestrator` wraps the call and returns an OrchestratorOutput citing the
evidence produced.

Sub-agents come from the box 4 ExecutionPlan (L6): only `plan.enabled_agents` are registered as
tools, and the tool guide lists only those. A payload without a plan (local smoke runs) gets one from
the same deterministic resolver. A plan with no enabled agent (task out_of_scope) runs no LLM at all.
AF_ORCHESTRATION=graph (default): the plan's sub-agents run in parallel (agents/graph.py), then a synthesis
agent joins them; AF_ORCHESTRATION=tools: this orchestrator calls them as tools, one after the other.
Either way the top-level agent carries the AgentCore Memory session (L17) when AF_MEMORY_ID is set.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.agents.hooks import EvidenceHook
from app.agents.memory import MemoryBinding, build_memory, memory_block
from app.agents.subagents import REGISTRY, subagents_for_plan
from app.agents.subagents.base import run_context
from app.context.plan_resolver import resolve_plan
from app.context.template_loader import render_prompt
from app.context.tool_guide import tools_block
from app.core.config import Settings, get_settings
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.models import OrchestratorInput, OrchestratorOutput, Profile
from app.store.ledger import EvidenceLedger, make_ledger
from app.tools import agent_tools
from app.tools.context import current_as_of, current_profile, default_as_of
from strands import Agent
from strands.models import BedrockModel, CacheConfig

_CACHE = CacheConfig(strategy="auto")   # static system prompt + tools: cache the prefix (see BedrockModel)


log = logging.getLogger(__name__)


def persona_block(profile: Profile, language: str) -> str:
    """Structured profile summary injected into the template (canonical Profile schema)."""
    h = profile.horizon_years
    horizon = f"{h.min} to {h.max} years" if h.max else f"{h.min}+ years"
    lines = [
        f"- actor: {profile.actor_id} · risk {profile.risk_band} · horizon {horizon} · experience: {profile.experience}",
        f"- objective: {profile.objective or 'n/a'}",
    ]
    if profile.preferences:
        lines.append("- preferences: " + "; ".join(profile.preferences))
    if profile.constraints:
        lines.append("- constraints: " + "; ".join(profile.constraints))
    if profile.exclusions:
        lines.append("- exclusions: " + "; ".join(
            f"{e.type}={e.value or e.min_severity or ''}".rstrip("=") for e in profile.exclusions))
    if profile.loss_capacity is not None:
        lines.append(f"- stated loss capacity: {profile.loss_capacity:.0%}")
    if profile.capital is not None:
        lines.append(f"- fictional capital (assumption): {profile.capital:,.0f} $ CAD".replace(",", " "))
    return "\n".join(lines)


def resolve_as_of(inp: OrchestratorInput, plan: ExecutionPlan | None = None) -> str:
    """Reference date of the run: payload, then plan, then request, then the last date of the curated data
    (never a hard-coded date)."""
    for value in (inp.as_of, getattr(plan, "as_of", None), inp.request.as_of):
        if value:
            return str(value)
    return default_as_of()


def compose_user_message(inp: OrchestratorInput, as_of: str | None = None) -> str:
    """User message: request + current portfolio + as_of reminder."""
    parts = [inp.request.user_message or inp.request.task]
    if inp.portfolio_weights:
        w = ", ".join(f"{t} {v:.0%}" for t, v in inp.portfolio_weights.items())
        parts.append(f"Current (fictional) portfolio: {w}.")
    else:
        parts.append("No portfolio provided: analyze according to the request.")
    as_of = as_of or inp.as_of
    if as_of:
        parts.append(f"Reference date (as_of): {as_of}.")
    return "\n\n".join(p for p in parts if p)


def plan_for(inp: OrchestratorInput) -> ExecutionPlan:
    """L6: the box 4 plan carried by the payload, or resolved on the fly (same code) when absent.
    A plan built for another request or actor is refused: it would run the wrong sub-agents."""
    plan = inp.execution_plan or resolve_plan(inp.profile, inp.request)
    if plan.request_id != inp.request.request_id or plan.actor_id != inp.request.actor_id:
        raise ValueError(f"execution_plan is for request {plan.request_id} / actor {plan.actor_id}, "
                         f"not {inp.request.request_id} / {inp.request.actor_id}")
    return plan


def build_orchestrator(inp: OrchestratorInput, ledger: EvidenceLedger | None = None,
                       settings: Settings | None = None, plan: ExecutionPlan | None = None,
                       as_of: str | None = None, memory: MemoryBinding | None = None) -> Agent:
    """Strands Agent for one run (never reused across runs)."""
    s = settings or get_settings()
    plan = plan or plan_for(inp)
    # L17: AgentCore Memory (short term: the session; long term: the actor's soft preferences) on THIS agent only.
    memory = memory or build_memory(s, inp.actor_id, inp.session_id or inp.run_id)

    def system_prompt() -> str:
        return render_prompt(
            "orchestrator",
            persona_block=persona_block(inp.profile, inp.language),
            task=inp.request.task,
            as_of=as_of or resolve_as_of(inp, plan),
            reply_language=inp.language,            # reply language follows the investor
            tools_block=tools_block(inp.request.task, plan),
            memory_block=memory_block(memory),      # soft preferences (long-term memory), empty when memory is off
        )


    led = ledger or make_ledger(s)   # JSONL locally, DynamoDB when AF_STORE=dynamodb (L15)
    # Sub-agents enabled by the box 4 ExecutionPlan (task rules + profile toggles, L6).
    # Pure delegation: the orchestrator keeps ONLY the deterministic governance
    # verdict — every number comes from a sub-agent finding (no double compute).
    subagents = subagents_for_plan(plan)

    def make(session_manager) -> Agent:
        return Agent(
            model=BedrockModel(model_id=s.model_orchestrator, region_name=s.aws_region, cache_config=_CACHE),
            system_prompt=system_prompt(),
            tools=[agent_tools.governance_check, *subagents],
            hooks=[EvidenceHook(led, inp.run_id)],
            session_manager=session_manager,
        )

    return _with_memory(make, memory, inp.run_id)


def build_synthesis(inp: OrchestratorInput, ledger: EvidenceLedger, settings: Settings, as_of: str,
                    memory: MemoryBinding) -> Agent:
    """Graph mode's top-level agent (Sonnet): joins the sub-agents' findings, holds governance_check only, and
    carries the run's memory session like the tools-mode orchestrator (it runs after the graph, not inside it)."""
    s = settings

    def make(session_manager) -> Agent:
        return Agent(
            model=BedrockModel(model_id=s.model_orchestrator, region_name=s.aws_region, cache_config=_CACHE),
            system_prompt=render_prompt(
                "synthesis",
                persona_block=persona_block(inp.profile, inp.language),
                task=inp.request.task,
                as_of=as_of,
                reply_language=inp.language,
                memory_block=memory_block(memory),      # rendered after a fallback: never stale memory text
            ),
            tools=[agent_tools.governance_check],
            hooks=[EvidenceHook(ledger, inp.run_id, agent="synthesis")],
            session_manager=session_manager,
        )

    return _with_memory(make, memory, inp.run_id)


def _with_memory(make, memory: MemoryBinding, run_id: str) -> Agent:
    """make(session_manager) with the run's memory session; if restoring that session fails, memory is auxiliary:
    it is disabled (and reported) and the agent is built without it."""
    try:
        return make(memory.session_manager)
    except Exception as e:  # noqa: BLE001
        if memory.session_manager is None:
            raise
        log.warning("AgentCore Memory disabled for run %s: %s", run_id, e)
        memory.disable(f"{type(e).__name__}: {e}")
        return make(None)


def _final_text(result: Any) -> str:
    """Last assistant message of the AgentResult, robust to API variations."""
    direct = getattr(result, "result", None)
    if isinstance(direct, str) and direct.strip():
        return direct
    for msg in reversed(getattr(result, "messages", []) or []):
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role != "assistant":
            continue
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        if content is None:
            continue
        blocks = content if isinstance(content, list) else [content]
        texts = [getattr(b, "text", None) or (b.get("text") if isinstance(b, dict) else None) for b in blocks]
        texts = [t for t in texts if t]
        if texts:
            return "\n".join(texts)
    return str(result)


def run_orchestrator(inp: OrchestratorInput, settings: Settings | None = None) -> OrchestratorOutput:
    """Runs the analysis for one validated payload: plan, sub-agents, governance. `summary` is the orchestrator's
    working draft, not the answer: the Explainer (agents/explainer.py, called by the dispatcher after this, L7 L11
    L16 L18) writes the answer the user reads from the run's evidence."""
    s = settings or get_settings()
    led = make_ledger(s)   # JSONL locally, DynamoDB when AF_STORE=dynamodb (L15)
    start = time.perf_counter()
    try:
        plan = plan_for(inp)
        as_of = resolve_as_of(inp, plan)
        current_profile.set(inp.profile.model_dump())
        current_as_of.set(as_of)                   # every tool is cut at this date (point-in-time)
        # Sub-agents write their nested tool calls into the SAME run ledger (L15);
        # as_of feeds the deterministic brief preamble.
        run_context.set({"ledger": led, "run_id": inp.run_id, "as_of": as_of})

        def elapsed() -> int:
            return int((time.perf_counter() - start) * 1000)

        if not plan.enabled_agents:  # out_of_scope: nothing to analyse, the Explainer declines
            return OrchestratorOutput(
                run_id=inp.run_id, status="DONE", language=inp.language, as_of=as_of,
                summary=f"No analysis run: the execution plan enables no sub-agent (task {plan.task}).",
                evidence_ids=[], model="none", duration_ms=elapsed(),
            )
        memory = build_memory(s, inp.actor_id, inp.session_id or inp.run_id)

        if s.orchestration == "graph":
            # L8 parallel graph: the plan's sub-agents fork concurrently, one synthesis
            # node joins them (Sonnet keeps the deterministic governance_check only).
            from app.agents.graph import run as run_graph

            synthesis = build_synthesis(inp, led, s, as_of, memory)
            enabled = [(a.agent, REGISTRY[a.agent]) for a in plan.enabled_agents]
            subject = inp.request.user_message or inp.request.task
            g = run_graph(enabled, subject, inp.portfolio_weights, None,
                          synthesis, ledger=led, run_id=inp.run_id, brief=compose_user_message(inp, as_of))
            return OrchestratorOutput(
                run_id=inp.run_id, status="DONE", language=inp.language, summary=g["summary"], as_of=as_of,
                memory=memory.info(), evidence_ids=led.evidence_ids(inp.run_id),
                model=s.model_orchestrator, session_id=inp.session_id or inp.run_id if s.memory_id else None,
                duration_ms=elapsed(),
            )

        agent = build_orchestrator(inp, ledger=led, settings=s, plan=plan, as_of=as_of, memory=memory)
        summary = _final_text(agent(compose_user_message(inp, as_of)))
        return OrchestratorOutput(
            run_id=inp.run_id, status="DONE", language=inp.language, summary=summary, as_of=as_of,
            memory=memory.info(),
            evidence_ids=led.evidence_ids(inp.run_id),
            model=s.model_orchestrator, session_id=inp.session_id or inp.run_id if s.memory_id else None,
            duration_ms=elapsed(),
        )
    except Exception as e:  # visible failure, never silent
        return OrchestratorOutput(
            run_id=inp.run_id, status="FAILED", language=inp.language,
            evidence_ids=led.evidence_ids(inp.run_id), model=s.model_orchestrator,
            duration_ms=int((time.perf_counter() - start) * 1000), error=f"{type(e).__name__}: {e}",
        )
