"""Node ⑤ — Main orchestrator agent (Claude Sonnet via Bedrock).

Builds the Strands Agent for ONE run: prompt template (L5/L7), quant tools
(L12), evidence ledger (L15), optional AgentCore Memory (L17) via AF_MEMORY_ID.
`run_orchestrator` wraps the call and returns an OrchestratorOutput citing the
evidence produced.

Sub-agents come from the box 4 ExecutionPlan (L6): only `plan.enabled_agents` are registered as
tools, and the tool guide lists only those. A payload without a plan (local smoke runs) gets one from
the same deterministic resolver. A plan with no enabled agent (task out_of_scope) runs no LLM at all.
Planned extension: the plan's sub-agents as Strands graph nodes (GraphBuilder / Swarm in parallel).
"""
from __future__ import annotations

import time
from typing import Any

from strands import Agent

from app.agents.hooks import EvidenceHook
from app.agents.subagents import subagents_for_plan
from app.agents.subagents.base import run_context
from app.context.plan_resolver import resolve_plan
from app.context.template_loader import render_prompt
from app.context.tool_guide import tools_block
from app.core.config import Settings, get_settings
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.models import OrchestratorInput, OrchestratorOutput, Profile
from app.store.ledger import JsonlLedger
from app.tools import agent_tools


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


def compose_user_message(inp: OrchestratorInput) -> str:
    """User message: request + current portfolio + as_of reminder."""
    parts = [inp.request.user_message or inp.request.task]
    if inp.portfolio_weights:
        w = ", ".join(f"{t} {v:.0%}" for t, v in inp.portfolio_weights.items())
        parts.append(f"Current (fictional) portfolio: {w}.")
    else:
        parts.append("No portfolio provided: analyze according to the request.")
    if inp.as_of:
        parts.append(f"Reference date (as_of): {inp.as_of}.")
    return "\n\n".join(p for p in parts if p)


def plan_for(inp: OrchestratorInput) -> ExecutionPlan:
    """L6: the box 4 plan carried by the payload, or resolved on the fly (same code) when absent.
    A plan built for another request or actor is refused: it would run the wrong sub-agents."""
    plan = inp.execution_plan or resolve_plan(inp.profile, inp.request)
    if plan.request_id != inp.request.request_id or plan.actor_id != inp.request.actor_id:
        raise ValueError(f"execution_plan is for request {plan.request_id} / actor {plan.actor_id}, "
                         f"not {inp.request.request_id} / {inp.request.actor_id}")
    return plan


def build_orchestrator(inp: OrchestratorInput, ledger: JsonlLedger | None = None,
                       settings: Settings | None = None, plan: ExecutionPlan | None = None) -> Agent:
    """Strands Agent for one run (never reused across runs)."""
    s = settings or get_settings()
    plan = plan or plan_for(inp)
    system_prompt = render_prompt(
        "orchestrator",
        persona_block=persona_block(inp.profile, inp.language),
        task=inp.request.task,
        as_of=inp.as_of or "2026-11-12",
        reply_language=inp.language,                # reply language follows the investor
        tools_block=tools_block(inp.request.task, plan),
    )

    session_manager = None
    if s.memory_id:
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
        session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=AgentCoreMemoryConfig(
                memory_id=s.memory_id,
                session_id=inp.session_id or inp.run_id,   # one isolated session per analysis
                actor_id=inp.actor_id,
            ),
            region_name=s.aws_region,
        )

    led = ledger or JsonlLedger(s.abs_path(s.runs_dir))
    # Sub-agents enabled by the box 4 ExecutionPlan (task rules + profile toggles, L6).
    # Pure delegation: the orchestrator keeps ONLY the deterministic governance
    # verdict — every number comes from a sub-agent finding (no double compute).
    subagents = subagents_for_plan(plan)
    return Agent(
        system_prompt=system_prompt,
        tools=[agent_tools.governance_check, *subagents],
        hooks=[EvidenceHook(led, inp.run_id)],
        session_manager=session_manager,
    )


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
    """Runs the end-to-end analysis for one validated payload."""
    s = settings or get_settings()
    led = JsonlLedger(s.abs_path(s.runs_dir))
    agent_tools.current_profile.set(inp.profile.model_dump())
    # Sub-agents write their nested tool calls into the SAME run ledger (L15);
    # as_of feeds the deterministic brief preamble.
    run_context.set({"ledger": led, "run_id": inp.run_id,
                     "as_of": inp.as_of or "2026-11-12"})

    start = time.perf_counter()
    try:
        plan = plan_for(inp)
        if not plan.enabled_agents:  # out_of_scope: nothing to analyse, the Explainer declines
            return OrchestratorOutput(
                run_id=inp.run_id, status="DONE", language=inp.language,
                summary=f"No analysis run: the execution plan enables no sub-agent (task {plan.task}).",
                evidence_ids=[], model="none",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        agent = build_orchestrator(inp, ledger=led, settings=s, plan=plan)
        result = agent(compose_user_message(inp))
        return OrchestratorOutput(
            run_id=inp.run_id, status="DONE", language=inp.language,
            summary=_final_text(result), evidence_ids=led.evidence_ids(inp.run_id),
            model=s.model_orchestrator,
            session_id=inp.session_id or inp.run_id if s.memory_id else None,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as e:  # visible failure, never silent
        return OrchestratorOutput(
            run_id=inp.run_id, status="FAILED", language=inp.language,
            evidence_ids=led.evidence_ids(inp.run_id), model=s.model_orchestrator,
            duration_ms=int((time.perf_counter() - start) * 1000), error=f"{type(e).__name__}: {e}",
        )
