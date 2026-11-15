"""Explainer (box ⑤ EXP, links L7, L11, L16, L18): the final answer, in the reader's style, every claim checked.

    answer = explain(run, orchestrator_output, ledger_records)

1. Out of scope (the plan enables no analysis) → a fixed, polite decline; no model call.
2. No usable evidence (orchestrator failed) → an honest fallback built from the evidence sentences only.
3. Otherwise: load the Explainer template named by the run's PromptRef (part ④; refused if its version or
   sha256 changed), ask Claude Sonnet on Bedrock for an ExplainerDraft (structured output) from the question,
   the reader's profile, the plan, the governance verdicts and the evidence catalogue; run the claim check;
   retry once with the problems listed; remove what still fails; record it all in `verification`.
A refusal or an API error falls back to (2) instead of failing the run.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from app.agents.claim_check import apply, check, context_numbers, count_claims
from app.agents.evidence import EvidenceItem, build_catalog, governance_items
from app.context.prompt_loader import PromptError, load_prompt
from app.core.config import Settings, get_settings
from app.schemas.run_result import ExplainerDraft, RemovedClaim, RunAnswer, Verification

AGENT_NAMES = {
    "risk": "Portfolio risk", "macro_scenarios": "Macro and rates", "esg": "ESG and controversies",
    "fundamentals": "Company fundamentals", "news": "News", "allocation": "Allocation proposal",
}
_INLINE_ID = re.compile(r"\s*[\[(]\s*(?:ev-[0-9a-f]{6,}|E-\d{3})(?:\s*[,;]\s*(?:ev-[0-9a-f]{6,}|E-\d{3}))*\s*[\])]")


def _clean(text: str) -> str:
    """Citations live in the evidence lists (the UI shows them as chips), not inside the sentences."""
    return re.sub(r"\s+([.,;:])", r"\1", _INLINE_ID.sub("", text)).strip()


def _verdict_checks(catalog: dict[str, EvidenceItem]) -> list[dict]:
    """The orchestrator's governance verdicts, straight from the evidence (used when no written check survived)."""
    seen, out = set(), []
    for g in governance_items(catalog):
        if g.agent != "orchestrator" or g.id in seen:
            continue
        seen.add(g.id)
        mix = ", ".join(f"{t} {w:.0%}" for t, w in sorted((g.weights or {}).items(), key=lambda kv: -kv[1]))
        blocked = [c.get("id") for c in (g.facts.get("checks") or []) if isinstance(c, dict) and c.get("status") == "BLOCK"] \
            if isinstance(g.facts, dict) else []
        text = g.say or (f"Blocked by: {', '.join(x for x in blocked if x)}." if blocked else f"Verdict: {g.verdict}.")
        out.append({"allocation": mix or "The portfolio checked", "verdict": g.verdict, "text": text, "evidence": [g.id]})
    return out


SHORT_ANSWER_REMOVED = ("The short summary could not be matched to the evidence, so it was removed. "
                        "The checked points below still hold.")


def _not_analysed(plan: dict) -> list[str]:
    out = []
    for d in plan.get("disabled_agents") or []:
        reason = d.get("reason", "")
        if "not needed" in reason or "out_of_scope" in reason:
            continue  # nothing the reader asked for; the plan view lists these
        name = AGENT_NAMES.get(d.get("agent"), d.get("agent"))
        said = reason.split('user said: "', 1)[1].split('"', 1)[0] if 'user said: "' in reason else None
        out.append(f"{name}: off because you said “{said}”." if said else f"{name}: turned off in your profile.")
    return out


def declined(run: Any) -> RunAnswer:
    return RunAnswer(
        kind="declined",
        short_answer=("AlphaForge can't do that. It is a simulation tool: it never places real trades and does not give "
                      "personal financial advice. It can test how risky a portfolio is, show how sensitive it is to "
                      "interest rates, check it against your profile or propose a simulated allocation."),
        not_analysed=_not_analysed(_plan(run)),
        note="The request was outside what the simulation can do, so no analysis was run.",
    )


def fallback(run: Any, catalog: dict[str, EvidenceItem], note: str) -> RunAnswer:
    """Evidence sentences only: nothing that the tools did not say themselves."""
    points = [{"text": item.say, "evidence": [item.id]} for item in catalog.values()
              if item.say and not item.verdict and item.agent == "orchestrator"][:6]
    if not points:
        points = [{"text": item.say, "evidence": [item.id]} for item in catalog.values() if item.say][:6]
    checks = [{"allocation": ", ".join(f"{t} {w:.0%}" for t, w in (g.weights or {}).items()) or "The portfolio checked",
               "verdict": g.verdict, "text": g.say or f"Governance verdict: {g.verdict}.", "evidence": [g.id]}
              for g in governance_items(catalog) if g.agent == "orchestrator"]
    return RunAnswer(
        kind="fallback",
        short_answer=("AlphaForge could not write the full explanation this time. Here is what the analysis "
                      "itself found, word for word." if points else
                      "The analysis did not produce any result this time. Try asking again."),
        key_points=points, checks=checks, not_analysed=_not_analysed(_plan(run)), note=note,
    )


def _plan(run: Any) -> dict:
    plan = getattr(run, "execution_plan", None) or {}
    return plan.model_dump(mode="json") if hasattr(plan, "model_dump") else dict(plan)


def _dump(x: Any) -> dict:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else dict(x or {})


def _final_verdicts(catalog: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    own = [g for g in governance_items(catalog) if g.agent == "orchestrator"]
    return own or governance_items(catalog)


def build_input(run: Any, orchestrator: dict, catalog: dict[str, EvidenceItem]) -> dict[str, Any]:
    profile, request, plan = _dump(run.profile), _dump(run.request), _plan(run)
    return {
        "question": request.get("user_message"),
        "task": plan.get("task"),
        "reader": {k: profile.get(k) for k in ("risk_band", "horizon_years", "experience", "wants_to_learn",
                                                "capital", "loss_capacity", "objective", "exclusions")},
        "needs_clarification": plan.get("needs_clarification", False),
        "plan": {"looked_at": [{"agent": a["agent"], "reason": a["reason"], "parameters": a.get("parameters")}
                               for a in plan.get("enabled_agents") or []],
                 "left_out": [{"agent": d["agent"], "reason": d["reason"]} for d in plan.get("disabled_agents") or []]},
        # The verdicts to report: the orchestrator's own checks (the user's request, the final proposal). Sub-agents'
        # verdicts are intermediate attempts (e.g. the allocation search): still citable, listed with the evidence.
        "governance": [{"id": g.id, "verdict": g.verdict, "weights": g.weights, "say": g.say,
                        "checks": g.facts.get("checks") if isinstance(g.facts, dict) else None}
                       for g in _final_verdicts(catalog)],
        "evidence": [i.for_model() for i in catalog.values() if i not in _final_verdicts(catalog)],
        "draft": orchestrator.get("summary") or "",
    }


def _feedback(issues) -> str:
    lines = [f"- {x.section}[{x.index}]: {x.reason}" for x in issues]
    return ("The automatic check of your answer found these problems:\n" + "\n".join(lines) +
            "\nReturn the whole answer again. Fix each item: cite the evidence that contains the number, use the "
            "number as the evidence states it, or leave the claim out.")


def explain(run: Any, orchestrator: Optional[dict], records: list[dict], *, client: Any = None,
            settings: Optional[Settings] = None, prompt_source: Any = None) -> RunAnswer:
    s = settings or get_settings()
    plan = _plan(run)
    if not plan.get("enabled_agents"):
        return declined(run)
    catalog = build_catalog(records)
    orchestrator = orchestrator or {}
    if orchestrator.get("status") == "FAILED" or not catalog:
        return fallback(run, catalog, f"The analysis did not finish: {orchestrator.get('error') or 'no evidence was produced'}.")

    start = time.perf_counter()
    try:
        template = load_prompt(run.prompt_ref, prompt_source)
    except PromptError as e:
        return fallback(run, catalog, f"The explanation template could not be used ({e}).")

    if client is None:
        from anthropic import AnthropicBedrock

        client = AnthropicBedrock(aws_region=s.aws_region)
    system = [{"type": "text", "text": template.body, "cache_control": {"type": "ephemeral"}}]
    payload = json.dumps(build_input(run, orchestrator, catalog), ensure_ascii=False, default=str)
    messages: list[dict] = [{"role": "user", "content": payload}]
    context = context_numbers(_dump(run.profile), _dump(run.request))

    def ask(msgs: list[dict]):
        response = client.messages.parse(model=s.model_explainer, max_tokens=16000, system=system, messages=msgs,
                                         output_format=ExplainerDraft, output_config={"effort": s.explainer_effort})
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"no usable answer (stop_reason={response.stop_reason})")
        return response.parsed_output

    try:
        draft = ask(messages)
        issues, unknown = check(draft, catalog, context)
        retried = False
        if issues:
            retried = True
            messages += [{"role": "assistant", "content": draft.model_dump_json()},
                         {"role": "user", "content": _feedback(issues)}]
            draft = ask(messages)
            issues, unknown = check(draft, catalog, context)
    except Exception as e:  # API error, refusal, invalid output: an honest fallback, never a failed run
        return fallback(run, catalog, f"The explanation model was unavailable ({type(e).__name__}).")

    claims = count_claims(draft)
    final = apply(draft, issues, catalog)
    short_failed = any(x.section == "short_answer" and not x.reason.startswith("cites evidence that does not exist")
                       for x in issues)
    removed = [RemovedClaim(section=x.section, text=x.text, reason=x.reason) for x in issues
               if not x.reason.startswith("cites evidence that does not exist")]
    clean = lambda items: [i.model_copy(update={"text": _clean(i.text)}) for i in items]  # noqa: E731
    return RunAnswer(
        kind="explained",
        short_answer=SHORT_ANSWER_REMOVED if short_failed else _clean(final.short_answer),
        short_answer_evidence=[] if short_failed else final.short_answer_evidence,
        key_points=clean(final.key_points), checks=clean(final.checks) or _verdict_checks(catalog),
        limitations=clean(final.limitations),
        glossary=final.glossary if run.prompt_ref.style == "learning" else [],
        clarifying_question=final.clarifying_question,
        not_analysed=_not_analysed(plan),
        verification=Verification(claims_checked=claims, removed=removed, unknown_ids_dropped=unknown, retried=retried),
        model=s.model_explainer, prompt=run.prompt_ref,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
