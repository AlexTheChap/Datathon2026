"""Explainer (box ⑤ EXP, links L7, L11, L16, L18): the final answer, in the reader's style, every claim checked.

    answer = explain(run, orchestrator_output, ledger_records)

1. Out of scope (the plan enables no analysis) → a fixed, polite decline; no model call.
2. No usable evidence (orchestrator failed) → an honest fallback built from the evidence sentences only.
3. Otherwise: load the Explainer template named by the run's PromptRef (part ④; refused if its version or
   sha256 changed), ask Claude Sonnet on Bedrock for an ExplainerDraft (structured output) from the question,
   the reader's profile, the plan, the governance verdicts and the evidence catalogue; run the claim check;
   then the output guardrail (Bedrock Guardrails: each statement grounded in the evidence it cites, relevant to the
   question, no denied topic; app.agents.output_guardrail, when AF_GUARDRAIL_ID is set);
   retry once with the problems listed; remove what still fails; record it all in `verification`.
A refusal or an API error falls back to (2) instead of failing the run.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from app.agents import plain_words
from app.agents.claim_check import apply, check, context_numbers, count_claims
from app.agents.evidence import TOP_LEVEL, EvidenceItem, build_catalog, governance_items
from app.agents.output_guardrail import apply_masks, screen
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
        if g.agent not in TOP_LEVEL or g.id in seen:
            continue
        seen.add(g.id)
        mix = ", ".join(f"{t} {w:.0%}" for t, w in sorted((g.weights or {}).items(), key=lambda kv: -kv[1]))
        blocked = [c.get("id") for c in (g.facts.get("checks") or []) if isinstance(c, dict) and c.get("status") == "BLOCK"] \
            if isinstance(g.facts, dict) else []
        text = g.say or (f"This goes against: {', '.join(x for x in blocked if x)}." if blocked
                         else PLAIN_VERDICT.get(g.verdict, PLAIN_VERDICT["UNVERIFIED"]))
        names = plain_words.names_in(f"{mix} {text}")
        out.append({"allocation": plain_words.plainify(mix, names) or "The portfolio checked", "verdict": g.verdict,
                    "text": plain_words.plainify(text, names), "evidence": [g.id]})
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
        short_answer=("Fitfolio can't do that. It is a simulation tool: it never places real trades and does not give "
                      "personal financial advice. It can test how risky a portfolio is, show how sensitive it is to "
                      "interest rates, check it against your profile or propose a simulated allocation."),
        not_analysed=_not_analysed(_plan(run)),
        note="The request was outside what the simulation can do, so no analysis was run.",
    )


# A verdict with no sentence of its own, in the reader's words (the code stays in the `verdict` field).
PLAIN_VERDICT = {"PASS": "This mix fits every limit of your profile.",
                 "WARN": "This mix stays within your profile's firm limits, with a point worth keeping an eye on.",
                 "BLOCK": "This mix goes against one of your profile's limits.",
                 "UNVERIFIED": "This mix could not be fully checked against your profile, so it is not confirmed."}


def fallback(run: Any, catalog: dict[str, EvidenceItem], note: str) -> RunAnswer:
    """Evidence sentences only: nothing that the tools did not say themselves."""
    points = [{"text": item.say, "evidence": [item.id]} for item in catalog.values()
              if item.say and not item.verdict and item.agent in TOP_LEVEL][:6]
    if not points:
        points = [{"text": item.say, "evidence": [item.id]} for item in catalog.values() if item.say][:6]
    checks = [{"allocation": ", ".join(f"{t} {w:.0%}" for t, w in (g.weights or {}).items()) or "The portfolio checked",
               "verdict": g.verdict, "text": g.say or PLAIN_VERDICT.get(g.verdict, PLAIN_VERDICT["UNVERIFIED"]),
               "evidence": [g.id]}
              for g in governance_items(catalog) if g.agent in TOP_LEVEL]
    for x in points + checks:                                   # the reader's words (plain_words.py)
        names = plain_words.names_in(" ".join(str(v) for v in x.values()))
        x.update({k: plain_words.plainify(x[k], names) for k in ("text", "allocation") if k in x})
    return RunAnswer(
        kind="fallback",
        short_answer=("Fitfolio could not write the full explanation this time. Here is what the analysis "
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
    own = [g for g in governance_items(catalog) if g.agent in TOP_LEVEL]
    return own or governance_items(catalog)


def build_input(run: Any, orchestrator: dict, catalog: dict[str, EvidenceItem]) -> dict[str, Any]:
    payload = _input(run, orchestrator, catalog)
    # Rule 9: name companies and funds, never bare tickers. Every ticker the input mentions, with its name.
    payload["names"] = plain_words.names_in(json.dumps(payload, ensure_ascii=False, default=str))
    return payload


def _input(run: Any, orchestrator: dict, catalog: dict[str, EvidenceItem]) -> dict[str, Any]:
    profile, request, plan = _dump(run.profile), _dump(run.request), _plan(run)
    return {
        "question": request.get("user_message"),
        "task": plan.get("task"),
        "reader": {**{k: profile.get(k) for k in ("risk_band", "horizon_years", "experience", "wants_to_learn",
                                                  "capital", "loss_capacity", "objective", "exclusions")},
                   # The holdings this question is about (the saved portfolio for "my portfolio", or the assets named),
                   # so the answer can say what the portfolio is made of.
                   "portfolio": [{"ticker": p.get("ticker"), "weight": p.get("weight")} for p in request.get("positions") or []]},
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


def _wording(draft: ExplainerDraft, names: dict[str, str]) -> list[tuple[str, int, list[str]]]:
    """Sentences that still use verdict codes, rule ids, bare tickers or system words (rule 9)."""
    texts = [("short_answer", 0, draft.short_answer)]
    texts += [("key_points", i, p.text) for i, p in enumerate(draft.key_points)]
    texts += [("checks", i, f"{c.allocation}. {c.text}") for i, c in enumerate(draft.checks)]
    texts += [("limitations", i, p.text) for i, p in enumerate(draft.limitations)]
    return [(sec, i, found) for sec, i, t in texts
            if (found := plain_words.problems(t or "", {**plain_words.names_in(t or ""), **names}))]


def _feedback(issues, wording=()) -> str:
    lines = [f"- {x.section}[{x.index}]: {x.reason}" for x in issues]
    lines += [f"- {section}[{i}]: uses {', '.join(found)}; rewrite it in the reader's words (rule 9), naming "
              "companies and funds from `names`" for section, i, found in wording]
    return ("The automatic check of your answer found these problems:\n" + "\n".join(lines) +
            "\nReturn the whole answer again. Fix each item: cite the evidence that contains the number, use the "
            "number as the evidence states it, rephrase so the statement says only what its cited evidence says, "
            "or leave the claim out.")


def explain(run: Any, orchestrator: Optional[dict], records: list[dict], *, client: Any = None,
            settings: Optional[Settings] = None, prompt_source: Any = None, guardrail_client: Any = None) -> RunAnswer:
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
    model_input = build_input(run, orchestrator, catalog)
    names = model_input["names"]
    payload = json.dumps(model_input, ensure_ascii=False, default=str)
    # cache_control on the payload: the retry ask re-sends this block unchanged, so Bedrock
    # skips re-reading the evidence catalogue (the system template above is cached too).
    messages: list[dict] = [{"role": "user",
                             "content": [{"type": "text", "text": payload,
                                          "cache_control": {"type": "ephemeral"}}]}]
    context = context_numbers(_dump(run.profile), _dump(run.request))
    question = _dump(run.request).get("user_message") or ""
    guarded = None  # the latest output-guardrail Screen (None = not configured)

    def verify(d: ExplainerDraft):
        """Claim check (numbers, ids, verdicts), then the output guardrail (grounding, topics). Never raises
        for the guardrail: screen() turns an AWS error into status "unavailable"."""
        nonlocal guarded
        found, unknown_ids = check(d, catalog, context)
        guarded = screen(d, catalog, question, settings=s, client=guardrail_client)
        if guarded is not None:
            failed = {(x.section, x.index) for x in found}
            found += [x for x in guarded.issues if (x.section, x.index) not in failed]
        return found, unknown_ids

    def ask(msgs: list[dict]):
        response = client.messages.parse(model=s.model_explainer, max_tokens=16000, system=system, messages=msgs,
                                         output_format=ExplainerDraft, output_config={"effort": s.explainer_effort})
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"no usable answer (stop_reason={response.stop_reason})")
        return response.parsed_output

    try:
        draft = ask(messages)
        issues, unknown = verify(draft)
        wording = _wording(draft, names)
        retried = False
        if issues or wording:
            retried = True
            messages += [{"role": "assistant", "content": draft.model_dump_json()},
                         {"role": "user", "content": _feedback(issues, wording)}]
            draft = ask(messages)
            issues, unknown = verify(draft)
    except Exception as e:  # API error, refusal, invalid output: an honest fallback, never a failed run
        return fallback(run, catalog, f"The explanation model was unavailable ({type(e).__name__}).")

    claims = count_claims(draft)
    if guarded is not None:
        draft = apply_masks(draft, guarded.masked)  # personal data anonymized by the guardrail
    final = apply(draft, issues, catalog)
    short_failed = any(x.section == "short_answer" and not x.reason.startswith("cites evidence that does not exist")
                       for x in issues)
    removed = [RemovedClaim(section=x.section, text=x.text, reason=x.reason) for x in issues
               if not x.reason.startswith("cites evidence that does not exist")]
    def plain(t):  # citations out of the text, then the last resort of rule 9 (codes, rule ids, tickers -> words)
        return plain_words.plainify(_clean(t), {**plain_words.names_in(t), **names}) if t else t   # any ticker

    def clean(items):
        return [i.model_copy(update={"text": plain(i.text),
                                     **({"allocation": plain(i.allocation)} if hasattr(i, "allocation") else {})})
                for i in items]
    return RunAnswer(
        kind="explained",
        short_answer=SHORT_ANSWER_REMOVED if short_failed else plain(final.short_answer),
        short_answer_evidence=[] if short_failed else final.short_answer_evidence,
        key_points=clean(final.key_points), checks=clean(final.checks) or _verdict_checks(catalog),
        limitations=clean(final.limitations),
        glossary=final.glossary if run.prompt_ref.style == "learning" else [],
        clarifying_question=plain(final.clarifying_question),
        not_analysed=_not_analysed(plan),
        verification=Verification(claims_checked=claims, removed=removed, unknown_ids_dropped=unknown, retried=retried,
                                  output_guardrail=guarded.status if guarded is not None else None),
        model=s.model_explainer, prompt=run.prompt_ref,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
