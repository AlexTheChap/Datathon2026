"""Claim check (box ⑤ "cites evidence only + claim verifier"): deterministic, no LLM.

For every text the Explainer wrote:
1. each cited id must exist in the run's evidence catalogue (unknown ids are dropped and counted);
2. each number it states must match a number of the evidence it cites, or of the reader's own profile and
   question (context), allowing only rounding and sign ("lost 24%" for a -0.2418 drawdown):
   - the match uses what the Explainer was SHOWN of each item (say, facts, weights: EvidenceItem.numbers), not
     every number buried in the raw envelope, so a large item (a correlation matrix) cannot back any figure;
   - the tolerance follows the stated precision: "24%" allows +/-0.6 point, "24.2%" +/-0.06;
   - a figure stated as a gain ("+5%", "gained 5%", "rose 5%") is not backed by negative values only. The reverse
     is allowed: losses are stored either signed (drawdown -0.24) or as positive amounts (VaR 0.021);
3. a governance check must state the verdict its cited governance evidence returned.

Years, ISO dates, evidence ids and small counts ("3 checks", "2 sub-agents") are not treated as claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from app.agents.evidence import EvidenceItem
from app.schemas.run_result import AnswerCheck, AnswerPoint, ExplainerDraft

_ID = re.compile(r"\b(?:ev-[0-9a-f]{6,}|E-\d{3})\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{4}-\d{2}\b")
_NUM = re.compile(
    r"(?P<sign>[-−–+])?\s?(?P<cur>\$|CAD\s?|C\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<unit>%|percent\b|pp\b|k\b|K\b|thousand\b|m\b|M\b|million\b|bn\b|billion\b)?"
)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
# A gain word just before a figure ("rose 5%", "up 12%", but not "up to 15%").
_GAIN_BEFORE = re.compile(r"\b(?:gain(?:ed|s)?|rose|rise[sn]?|rising|up(?!\s+to\b)|increase[sd]?|grew|grow(?:n|s|th)?|"
                          r"rall(?:ied|ies|y)|climb(?:ed|s)?|jump(?:ed|s)?|advance[sd]?)\b[^.\d]{0,20}$", re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    section: str
    index: int
    text: str
    reason: str


@dataclass(frozen=True)
class Stated:
    raw: str
    value: float
    kind: str  # "pct" | "money" | "plain"
    decimals: int = 0      # digits after the point as written: sets the rounding tolerance
    gain: bool = False     # written as a gain ("+5%", "rose 5%"): needs a non-negative value


def stated_numbers(text: str) -> list[Stated]:
    clean = _DATE.sub(" ", _ID.sub(" ", text))
    out = []
    for m in _NUM.finditer(clean):
        raw_num = m.group("num")
        num = float(raw_num.replace(",", ""))
        decimals = len(raw_num.split(".")[1]) if "." in raw_num else 0
        gain = m.group("sign") == "+" or bool(_GAIN_BEFORE.search(clean[:m.start()]))
        unit = (m.group("unit") or "").lower()
        cur = m.group("cur")
        if unit in ("%", "percent", "pp"):
            out.append(Stated(m.group(0).strip(), num, "pct", decimals, gain))
        elif cur or unit in _MULT:
            out.append(Stated(m.group(0).strip(), num * _MULT.get(unit, 1.0), "money", decimals))
        else:
            if (1900 <= num <= 2100 and num == int(num)) or (num == int(num) and num <= 12):
                continue  # a year, or a small count ("3 checks", "5 years" is checked via context anyway)
            out.append(Stated(m.group(0).strip(), num, "plain", decimals, gain))
    return out


def _supported(s: Stated, pool: set[tuple[str, float]]) -> bool:
    """A percentage needs a fraction from a data field (0.242 for "24%") or a percentage written as such in a text
    ("51%"): never a count or a plain number, so an invented "up 100%" is not backed by a stray "1".
    Rounding tolerance: 0.6 unit of the last digit written ("24%" +/-0.6 point, "24.2%" +/-0.06)."""
    t = abs(s.value)
    tol = 0.6 * 10 ** -s.decimals
    for kind, c in pool:
        if s.gain and c < 0:              # a gain is never backed by a loss
            continue
        a = abs(c)
        if s.kind == "pct":
            if (kind == "frac" and abs(a * 100 - t) <= tol) or (kind == "pct_text" and abs(a - t) <= tol):
                return True
        elif s.kind == "money":
            if kind != "pct_text" and t and abs(a - t) <= max(0.02 * t, 1.0):  # "$97k" for 96,712; "$400,000"
                return True
        else:
            if abs(a - t) <= max(tol, 0.02 * t) or (kind == "frac" and abs(a * 100 - t) <= tol):
                return True
    return False


def context_numbers(profile: dict, request: dict) -> set[tuple[str, float]]:
    """Numbers the reader gave themselves: capital, horizon, loss limit, weights named in the question."""
    from app.agents.evidence import _numbers

    pool: set[tuple[str, float]] = set()
    for key in ("capital", "loss_capacity", "horizon_years"):
        _numbers(profile.get(key), pool)
    for key in ("capital", "positions"):
        _numbers(request.get(key), pool)
    _numbers(request.get("user_message"), pool)
    return pool


def _texts(draft: ExplainerDraft) -> Iterable[tuple[str, int, str, list[str], Optional[AnswerCheck]]]:
    yield "short_answer", 0, draft.short_answer, draft.short_answer_evidence, None
    for i, p in enumerate(draft.key_points):
        yield "key_points", i, p.text, p.evidence, None
    for i, c in enumerate(draft.checks):
        yield "checks", i, f"{c.allocation}. {c.text}", c.evidence, c
    for i, p in enumerate(draft.limitations):
        yield "limitations", i, p.text, p.evidence, None


def check(draft: ExplainerDraft, catalog: dict[str, EvidenceItem],
          context: set[tuple[str, float]]) -> tuple[list[Issue], int]:
    """Return (issues, number of unknown ids cited). The draft itself is not modified."""
    issues: list[Issue] = []
    unknown = 0
    for section, i, text, ids, chk in _texts(draft):
        cited = [catalog[x] for x in ids if x in catalog]
        cited += [catalog[c.parent] for c in list(cited) if c.parent and c.parent in catalog]  # e.g. the verdict
        missing = [x for x in ids if x not in catalog]
        unknown += len(missing)
        if missing:
            issues.append(Issue(section, i, text, f"cites evidence that does not exist: {', '.join(missing)}"))
        pool = set(context)
        for item in cited:
            pool |= item.numbers
        bad = [s.raw for s in stated_numbers(text) if not _supported(s, pool)]
        if bad:
            where = "its cited evidence" if cited else "any cited evidence (none cited)"
            issues.append(Issue(section, i, text, f"states {', '.join(bad)}, not found in {where}"))
        if chk is not None:
            verdicts = {item.verdict for item in cited if item.verdict}
            if verdicts and chk.verdict not in verdicts:
                issues.append(Issue(section, i, text,
                                    f"says {chk.verdict} but the cited governance evidence says {'/'.join(sorted(verdicts))}"))
            if not verdicts:
                issues.append(Issue(section, i, text, "a governance check must cite the governance evidence it reports"))
    return issues, unknown


def count_claims(draft: ExplainerDraft) -> int:
    return 1 + len(draft.key_points) + len(draft.checks) + len(draft.limitations)


def apply(draft: ExplainerDraft, issues: list[Issue], catalog: dict[str, EvidenceItem]) -> ExplainerDraft:
    """Remove what failed; drop unknown ids from what passed. The short answer is replaced, never removed."""
    # An unknown id alone is just dropped (counted in Verification); a claim is removed only when a number or
    # a verdict fails, which also covers a claim whose only citations were unknown ids.
    failed = {(x.section, x.index) for x in issues if not x.reason.startswith("cites evidence that does not exist")}
    keep = lambda ids: [x for x in ids if x in catalog]  # noqa: E731
    points = [AnswerPoint(text=p.text, evidence=keep(p.evidence)) for i, p in enumerate(draft.key_points)
              if ("key_points", i) not in failed]
    checks = [c.model_copy(update={"evidence": keep(c.evidence)}) for i, c in enumerate(draft.checks)
              if ("checks", i) not in failed]
    limits = [AnswerPoint(text=p.text, evidence=keep(p.evidence)) for i, p in enumerate(draft.limitations)
              if ("limitations", i) not in failed]
    return draft.model_copy(update={"key_points": points, "checks": checks, "limitations": limits,
                                    "short_answer_evidence": keep(draft.short_answer_evidence)})
