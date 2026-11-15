"""Claim verifier (node ⑤, after the Explainer): deterministic code, no LLM.

The Explainer may only cite evidence that exists. This module checks the written answer against the run's
evidence ledger:
- every citation `[ev-…]` resolves to an evidence id of the run (the short form `[ev-3f2a]` is accepted when it
  identifies one id without ambiguity). An unknown citation is an ERROR: the answer cannot be trusted;
- every percentage or dollar amount in a sentence that carries a citation appears in the cited evidence (or in the
  evidence it is built from), within the rounding of the sentence. A mismatch is a WARNING;
- a sentence with a percentage or dollar amount and no citation is a WARNING (a number without evidence).

`ok` is True only when no citation is unknown. Warnings are returned for the trace and for the repair prompt.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

TOKEN = re.compile(r"\bev-[0-9a-f]{4,10}\b")
BRACKET = re.compile(r"\[([^\[\]\n]*\bev-[0-9a-f]{4,10}\b[^\[\]\n]*)\]")
NUM = r"[+\-−–]?(?:\d{1,3}(?:[,\u202f\u00a0]\d{3})+|\d+)(?:\.\d+)?"       # 12.4  -1,234.5  84,232
PERCENT = re.compile(rf"({NUM})\s?%")
MONEY = re.compile(rf"\$\s?({NUM})\s?(billion|million|thousand|bn|[kKmMbB])?(?![A-Za-z])")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
NOISE = re.compile(r"\b(?:ev|N)-[0-9a-f]{4,12}\b|\b\d{4}-\d{2}-\d{2}\b")   # ids and ISO dates are not figures


@dataclass
class VerificationResult:
    ok: bool
    n_citations: int
    resolved: dict[str, str] = field(default_factory=dict)          # token in the text -> full evidence id
    unknown: list[str] = field(default_factory=list)
    uncited_numbers: list[str] = field(default_factory=list)        # sentences with a number and no citation
    unmatched_numbers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_token(token: str, known: set[str]) -> str | None:
    """Full evidence id for a citation token (exact, or unique prefix), else None."""
    if token in known:
        return token
    matches = [k for k in known if k.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def _numbers(value: Any, out: list[float]) -> None:
    """Every number found in a JSON-like value (also inside strings, e.g. in a `say` sentence)."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        out.extend(float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", NOISE.sub(" ", value))
                   if x.strip("-,."))
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers(v, out)


def _parse(raw: str, suffix: str | None = None) -> float | None:
    s = raw.replace("−", "-").replace("–", "-").replace("\u202f", "").replace("\u00a0", "").replace(",", "")
    try:
        x = float(s)
    except ValueError:
        return None
    return x * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
                "b": 1e9, "bn": 1e9, "billion": 1e9}.get((suffix or "").lower(), 1.0)


def _matches(x: float, kind: str, pool: list[float]) -> bool:
    """x as written (`12.4` for 12.4%, or `84232` for $84,232) against the numbers of the evidence."""
    for n in pool:
        candidates = (n * 100, n) if kind == "percent" else (n,)
        for c in candidates:
            tol = 0.06 if kind == "percent" else max(0.5, 0.0051 * abs(c))
            if abs(abs(x) - abs(c)) <= tol:
                return True
    return False


def verify_answer(text: str, evidence: dict[str, dict], split_sentences: bool = True) -> VerificationResult:
    """`evidence`: evidence_id -> full evidence dict of the run (a sub-agent envelope may reference its
    `nested_evidence_ids`, whose numbers also count for the sentences that cite it).
    `split_sentences=False` checks the text as ONE unit (a sub-agent `details` line: its citation may sit at the
    end of the line, after several sentences)."""
    known = set(evidence)
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    uncited, unmatched = [], []
    n_citations = 0

    def pool_for(ids: list[str]) -> list[float]:
        seen, stack, pool = set(), list(ids), []
        while stack:
            eid = stack.pop()
            if eid in seen or eid not in evidence:
                continue
            seen.add(eid)
            ev = evidence[eid]
            _numbers([ev.get("output"), ev.get("say"), ev.get("inputs")], pool)
            stack.extend(ev.get("nested_evidence_ids") or [])
        return pool

    units = SENTENCE.split(text) if split_sentences else [text]
    for sentence in (s.strip() for s in units):
        if not sentence:
            continue
        ids: list[str] = []
        for group in BRACKET.findall(sentence):
            for token in TOKEN.findall(group):
                n_citations += 1
                full = resolve_token(token, known)
                if full is None:
                    if token not in unknown:
                        unknown.append(token)
                else:
                    resolved[token] = full
                    ids.append(full)
        body = BRACKET.sub("", sentence)
        figures = [("percent", _parse(m.group(1)), m.group(0).strip()) for m in PERCENT.finditer(body)]
        figures += [("money", _parse(m.group(1), m.group(2)), m.group(0).strip()) for m in MONEY.finditer(body)]
        figures = [(k, v, raw) for k, v, raw in figures if v is not None]
        if not figures:
            continue
        if not ids:
            uncited.append(sentence[:200])
            continue
        pool = pool_for(ids)
        for kind, value, raw in figures:
            if not _matches(value, kind, pool):
                unmatched.append({"sentence": sentence[:200], "number": raw, "cited": ids})
    return VerificationResult(ok=not unknown, n_citations=n_citations, resolved=resolved, unknown=unknown,
                              uncited_numbers=uncited, unmatched_numbers=unmatched)


def strip_unknown(text: str, unknown: list[str]) -> str:
    """Replace the citations that do not exist by `[unverified]` so nothing unverifiable looks sourced."""
    for token in unknown:
        text = re.sub(rf"\b{re.escape(token)}\b", "unverified", text)
    return text
