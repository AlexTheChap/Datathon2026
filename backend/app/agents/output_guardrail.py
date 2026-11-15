"""Output guardrail (link L18): Bedrock Guardrails on the Explainer's answer, after the deterministic claim check.

The claim check (app.agents.claim_check) verifies numbers and citation ids only: "a low-risk, defensive holding
(24% vol) [ev-…]" passes it when 24% is in the evidence, even though nothing there says "low-risk". This check asks
Bedrock Guardrails (ApplyGuardrail, source=OUTPUT) about every statement of the draft:

- contextual GROUNDING: is the statement supported by the evidence it cites (what the Explainer was shown of each
  item, EvidenceItem.for_model, plus the parent of nested items, as in the claim check)?
- RELEVANCE: does it answer the reader's question?
- the guardrail's other output filters: denied topics (guaranteed returns, personal buy/sell orders, tax or legal
  advice), content filters, personal data (anonymized: the masked text replaces the original, nothing is removed).

A statement that cites nothing (typically a limitation) gets the filters only, no grounding: an honest caveat is not
removed for having no source. Thresholds live in the guardrail itself (scripts/deploy_guardrail.py).

    result = screen(draft, catalog, question, settings=s)   # None when not configured

Never raises: any AWS error (throttling, timeout, missing permission) gives status "unavailable" and no issues,
so a guardrail outage never removes a claim nor turns a good answer into a fallback.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agents.claim_check import Issue, _texts
from app.agents.evidence import EvidenceItem
from app.core.config import Settings
from app.schemas.run_result import ExplainerDraft

log = logging.getLogger(__name__)

# ApplyGuardrail limits for the contextual grounding check (characters).
MAX_SOURCE = 100_000
MAX_QUERY = 1_000
MAX_CONTENT = 5_000
_WORKERS = 8

_GROUNDING_REASON = {
    "GROUNDING": "not supported by its cited evidence",
    "RELEVANCE": "does not answer the question",
}


@dataclass
class Screen:
    issues: list[Issue] = field(default_factory=list)
    masked: dict[tuple[str, int], str] = field(default_factory=dict)  # personal data anonymized: replace, not remove
    status: str = "passed"                                              # passed | flagged | unavailable


def enabled(settings: Settings) -> bool:
    return bool(settings.guardrail_id) and settings.output_guardrail


def _runtime_client(region: str):
    import boto3

    try:  # on Windows, botocore does not always see the system certificates (CERTIFICATE_VERIFY_FAILED)
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
    return boto3.client("bedrock-runtime", region_name=region)


def _cited(ids: list[str], catalog: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    """The cited items plus their parents (e.g. the governance verdict a nested check was found in)."""
    out = [catalog[x] for x in ids if x in catalog]
    for item in list(out):
        if item.parent and item.parent in catalog and catalog[item.parent] not in out:
            out.append(catalog[item.parent])
    return out


def build_content(text: str, cited: list[EvidenceItem], question: str) -> list[dict[str, Any]]:
    """ApplyGuardrail content: evidence + question + statement when it cites something, else the statement alone."""
    guard = {"text": {"text": text[:MAX_CONTENT], "qualifiers": ["guard_content"]}}
    if not cited or not question.strip():
        return [guard]
    source = json.dumps([i.for_model() for i in cited], ensure_ascii=False, default=str)[:MAX_SOURCE]
    return [{"text": {"text": source, "qualifiers": ["grounding_source"]}},
            {"text": {"text": question[:MAX_QUERY], "qualifiers": ["query"]}},
            guard]


def _reasons(response: dict) -> tuple[list[str], Optional[str]]:
    """(why the statement is blocked, masked text when only personal data was anonymized)."""
    if response.get("action") != "GUARDRAIL_INTERVENED":
        return [], None
    reasons: list[str] = []
    anonymized = False
    for a in response.get("assessments") or []:
        for f in (a.get("contextualGroundingPolicy") or {}).get("filters") or []:
            if f.get("action") == "BLOCKED":
                what = _GROUNDING_REASON.get(f.get("type"), str(f.get("type")).lower())
                reasons.append(f"{what} ({str(f.get('type')).lower()} {f.get('score', 0):.2f} < {f.get('threshold', 0):.2f})")
        for t in (a.get("topicPolicy") or {}).get("topics") or []:
            if t.get("action") == "BLOCKED":
                reasons.append(f"guardrail: denied topic {t.get('name')}")
        for c in (a.get("contentPolicy") or {}).get("filters") or []:
            if c.get("action") == "BLOCKED":
                reasons.append(f"guardrail: {str(c.get('type')).lower()} content")
        words = (a.get("wordPolicy") or {})
        if any(w.get("action") == "BLOCKED" for w in (words.get("customWords") or []) + (words.get("managedWordLists") or [])):
            reasons.append("guardrail: blocked wording")
        pii = a.get("sensitiveInformationPolicy") or {}
        for p in (pii.get("piiEntities") or []) + (pii.get("regexes") or []):
            if p.get("action") == "BLOCKED":
                reasons.append(f"guardrail: personal data ({str(p.get('type') or p.get('name')).lower()})")
            elif p.get("action") == "ANONYMIZED":
                anonymized = True
    masked = None
    if not reasons and anonymized:
        outputs = response.get("outputs") or []
        masked = outputs[0].get("text") if outputs else None
    return reasons, masked


def screen(draft: ExplainerDraft, catalog: dict[str, EvidenceItem], question: str, *,
           settings: Settings, client: Any = None) -> Optional[Screen]:
    """Check every statement of the draft; None when the output guardrail is not configured."""
    if not enabled(settings):
        return None
    items = list(_texts(draft))
    try:
        rt = client or _runtime_client(settings.aws_region)

        def one(entry):
            _section, _i, text, ids, _chk = entry
            return rt.apply_guardrail(guardrailIdentifier=settings.guardrail_id,
                                      guardrailVersion=settings.guardrail_version, source="OUTPUT",
                                      content=build_content(text, _cited(ids, catalog), question or ""))

        with ThreadPoolExecutor(max_workers=min(_WORKERS, max(1, len(items)))) as pool:
            responses = list(pool.map(one, items))
    except Exception as e:  # noqa: BLE001 — an outage must never block or degrade the answer
        log.warning("output guardrail unavailable: %s: %s", type(e).__name__, e)
        return Screen(status="unavailable")

    result = Screen()
    for (section, i, text, _ids, _chk), response in zip(items, responses):
        reasons, masked = _reasons(response)
        if reasons:
            result.issues.append(Issue(section, i, text, "; ".join(reasons)))
        elif masked and section != "checks":  # a check's text is "allocation. text": not safely splittable
            result.masked[(section, i)] = masked
    result.status = "flagged" if result.issues else "passed"
    return result


def apply_masks(draft: ExplainerDraft, masked: dict[tuple[str, int], str]) -> ExplainerDraft:
    """Replace the texts whose personal data the guardrail anonymized."""
    if not masked:
        return draft
    update: dict[str, Any] = {}
    if ("short_answer", 0) in masked:
        update["short_answer"] = masked[("short_answer", 0)]
    for section in ("key_points", "limitations"):
        points = getattr(draft, section)
        if any((section, i) in masked for i in range(len(points))):
            update[section] = [p.model_copy(update={"text": masked[(section, i)]}) if (section, i) in masked else p
                               for i, p in enumerate(points)]
    return draft.model_copy(update=update)
