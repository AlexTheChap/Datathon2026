"""Nœud ⑤ — Agent orchestrateur principal (Claude Sonnet via Bedrock).

Construit l'agent Strands pour UNE exécution : gabarit de prompt (L5/L7),
outils quantitatifs (L12), registre de preuves (L15), mémoire AgentCore (L17)
optionnelle (AF_MEMORY_ID). `run_orchestrator` encapsule l'appel et renvoie un
OrchestratorOutput citant les preuves produites.

Extension prévue (voir docs/architecture.mmd) : les sous-agents du plan seront
des nœuds du graphe Strands (strands.multiagent.GraphBuilder, Swarm en parallèle)
ou des outils — l'orchestrateur devient alors le nœud de synthèse du graphe.
"""
from __future__ import annotations

import time
from typing import Any

from strands import Agent

from app.agents.hooks import EvidenceHook
from app.context.prompt_loader import load_prompt
from app.core.config import Settings, get_settings
from app.schemas.models import OrchestratorInput, OrchestratorOutput, Profile
from app.store.ledger import JsonlLedger
from app.tools import agent_tools

_RISK_BAND_FR = {"faible": "faible", "faible_a_modere": "faible à modéré",
                 "modere": "modéré", "eleve": "élevé"}
_RISK_BAND_EN = {"faible": "low", "faible_a_modere": "low to moderate",
                 "modere": "moderate", "eleve": "high"}


def persona_block(profile: Profile, language: str) -> str:
    """Résumé structuré du profil injecté dans le gabarit (source : personas.json)."""
    h = profile.horizon_years
    horizon = f"{h[0]} à {h[1]} ans" if h[1] else f"{h[0]} ans ou plus"
    band = (_RISK_BAND_FR if language == "fr" else _RISK_BAND_EN).get(profile.risk_band, profile.risk_band)
    lines = [
        f"- id: {profile.id} · risque {band} · horizon {horizon} · expérience : {profile.experience or 'n/d'}",
        f"- objectif : {profile.objectif or 'n/d'}",
    ]
    if profile.preferences:
        lines.append("- préférences : " + " ; ".join(profile.preferences))
    if profile.contraintes:
        lines.append("- contraintes : " + " ; ".join(profile.contraintes))
    if profile.exclusions:
        lines.append("- exclusions : " + " ; ".join(
            f"{e.get('type')}={e.get('value', e.get('min_severity', ''))}".rstrip("=") for e in profile.exclusions))
    if profile.capacite_perte is not None:
        lines.append(f"- capacité de perte déclarée : {profile.capacite_perte:.0%}")
    elif profile.capital_fictif and "valeur" in (profile.capital_fictif or {}):
        lines.append(f"- capital fictif (hypothèse) : {profile.capital_fictif['valeur']:,.0f} $ CAD".replace(",", " "))
    return "\n".join(lines)


def compose_user_message(inp: OrchestratorInput) -> str:
    """Message utilisateur : demande + portefeuille actuel + rappel as_of."""
    fr = inp.language == "fr"
    parts = [inp.request.user_message or (inp.request.task if fr else inp.request.task)]
    if inp.portfolio_weights:
        w = ", ".join(f"{t} {v:.0%}" for t, v in inp.portfolio_weights.items())
        parts.append((f"Portefeuille actuel (fictif) : {w}." if fr else f"Current (fictional) portfolio: {w}."))
    elif fr:
        parts.append("Aucun portefeuille fourni : propose une analyse en fonction de la demande.")
    else:
        parts.append("No portfolio provided: analyze according to the request.")
    if inp.as_of:
        parts.append(f"as_of = {inp.as_of}" if not fr else f"Date de référence : {inp.as_of}.")
    return "\n\n".join(p for p in parts if p)


def build_orchestrator(inp: OrchestratorInput, ledger: JsonlLedger | None = None,
                       settings: Settings | None = None) -> Agent:
    """Agent Strands pour une exécution (jamais réutilisé entre exécutions)."""
    s = settings or get_settings()
    system_prompt = load_prompt(
        "orchestrator", inp.language,
        persona_block=persona_block(inp.profile, inp.language),
        task=inp.request.task,
        as_of=inp.as_of or "2026-11-12",
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
                session_id=inp.session_id or inp.run_id,   # une session isolée par analyse
                actor_id=inp.actor_id,
            ),
            region_name=s.aws_region,
        )

    led = ledger or JsonlLedger(s.abs_path(s.runs_dir))
    return Agent(
        system_prompt=system_prompt,
        tools=agent_tools.ALL_TOOLS,
        hooks=[EvidenceHook(led, inp.run_id)],
        session_manager=session_manager,
    )


def _final_text(result: Any) -> str:
    """Dernier message assistant de l'AgentResult, robuste aux variantes de l'API."""
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
    """Exécute l'analyse de bout en bout pour un payload validé."""
    s = settings or get_settings()
    led = JsonlLedger(s.abs_path(s.runs_dir))
    agent_tools.current_profile.set(inp.profile.model_dump())

    start = time.perf_counter()
    try:
        agent = build_orchestrator(inp, ledger=led, settings=s)
        result = agent(compose_user_message(inp))
        return OrchestratorOutput(
            run_id=inp.run_id, status="DONE", language=inp.language,
            summary=_final_text(result), evidence_ids=led.evidence_ids(inp.run_id),
            model=s.model_orchestrator,
            session_id=inp.session_id or inp.run_id if s.memory_id else None,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    except Exception as e:  # défaillance visible, jamais silencieuse
        return OrchestratorOutput(
            run_id=inp.run_id, status="FAILED", language=inp.language,
            evidence_ids=led.evidence_ids(inp.run_id), model=s.model_orchestrator,
            duration_ms=int((time.perf_counter() - start) * 1000), error=f"{type(e).__name__}: {e}",
        )
