"""Evidence catalogue for the Explainer (L16): every citable item of a run's ledger, by id.

The ledger (L15) holds one record per tool call, but citable evidence is also nested: sub-agent findings embed
their tools' evidence ids, governance checks point at the evidence they used, and older governance
results carry no id at all. The catalogue indexes all of them:

- a dict with an `evidence_id` anywhere in a record's payload → one item under that id;
- a record with no id at all → one item under its ledger number (e.g. "E-015").

Each item keeps a compact view for the model (`say`, period, a few facts) and, separately, every number it
contains with its kind, which the claim check (claim_check.py) compares with the numbers the answer states:
"frac" (a float field: 0.242 for a 24.2% drop), "int" (an integer field: a count, an amount), "pct_text" (a number
written with % in a text: "51%") and "text" (any other number in a text).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# The run's top level: the orchestrator (tools mode) or the graph's synthesis node (graph mode, agents/graph.py).
# Their governance verdicts are the final ones; sub-agents' checks are intermediate attempts.
TOP_LEVEL = ("orchestrator", "synthesis")
NUMBER_IN_TEXT = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?P<pct>\s?%)?")
_MAX_FACTS = 30
_SKIP_KEYS = {"series", "daily", "returns", "values", "path", "history", "weights_path"}


@dataclass
class EvidenceItem:
    id: str
    tool: str
    agent: str
    say: str = ""
    period: Optional[str] = None
    as_of: Optional[str] = None
    facts: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    verdict: Optional[str] = None
    weights: Optional[dict[str, float]] = None
    numbers: set[tuple[str, float]] = field(default_factory=set)  # (kind, value), see the module docstring
    seq: Optional[str] = None
    parent: Optional[str] = None   # the item this one was found inside (e.g. a governance verdict)

    def for_model(self) -> dict[str, Any]:
        """What the Explainer sees: short, no raw series."""
        out: dict[str, Any] = {"id": self.id, "tool": self.tool, "agent": self.agent, "say": self.say}
        for key in ("period", "as_of", "verdict", "weights"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.facts:
            out["facts"] = self.facts
        if self.assumptions:
            out["assumptions"] = self.assumptions[:4]
        if self.data_sources:
            out["data_sources"] = self.data_sources[:3]
        return out

    def for_user(self) -> dict[str, Any]:
        """What the evidence panel shows (GET /v1/runs/{id}/evidence)."""
        out = self.for_model()
        out["assumptions"] = self.assumptions
        out["data_sources"] = self.data_sources
        return out


def _numbers(value: Any, out: set[tuple[str, float]]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        out.add(("int", float(value)))
    elif isinstance(value, float):
        out.add(("frac", value))
    elif isinstance(value, str):
        for m in NUMBER_IN_TEXT.finditer(value):
            number = m.group(0).removesuffix("%").strip()
            try:
                out.add(("pct_text" if m.group("pct") else "text", float(number.replace(",", ""))))
            except ValueError:
                pass
    elif isinstance(value, dict):
        for v in value.values():
            _numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _numbers(v, out)


def _flatten(value: Any, prefix: str = "", out: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Short leaves only (numbers, short strings), dotted paths; long lists and series are skipped."""
    out = {} if out is None else out
    if len(out) >= _MAX_FACTS:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _SKIP_KEYS or k in ("evidence_id", "nested_evidence_ids", "say", "assumptions", "data_sources"):
                continue
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list):
        if len(value) <= 8:
            for i, v in enumerate(value):
                _flatten(v, f"{prefix}[{i}]", out)
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, float):
        out[prefix] = round(value, 6)
    elif isinstance(value, int):
        out[prefix] = value
    elif isinstance(value, str) and len(value) <= 160:
        out[prefix] = value
    return out


def _envelopes(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return []


def _nested_with_id(value: Any, top: dict) -> Iterable[dict]:
    """Dicts carrying an evidence_id inside an envelope (not the envelope itself)."""
    if isinstance(value, dict):
        if value is not top and "evidence_id" in value and isinstance(value.get("evidence_id"), str):
            yield value
        for v in value.values():
            yield from _nested_with_id(v, top)
    elif isinstance(value, list):
        for v in value:
            yield from _nested_with_id(v, top)


def _item_from_envelope(env: dict, record: dict) -> EvidenceItem:
    output = env.get("output")
    is_governance = "verdict" in env and "checks" in env
    say = env.get("say") or (output.get("headline") if isinstance(output, dict) else "") or record.get("say") or ""
    facts_source = output if output is not None else {k: v for k, v in env.items() if k not in ("inputs", "profile")}
    if is_governance:
        facts_source = {"verdict": env.get("verdict"),
                        "checks": [{k: c.get(k) for k in ("id", "status", "value", "limit", "detail")}
                                   for c in env.get("checks") or [] if isinstance(c, dict)]}
    weights = (env.get("inputs") or {}).get("weights") if isinstance(env.get("inputs"), dict) else None
    item = EvidenceItem(
        id=env["evidence_id"], tool=str(env.get("tool") or record.get("tool")),
        agent=record.get("agent") or "orchestrator", say=str(say),
        period=env.get("period") or record.get("period"), as_of=env.get("as_of") or record.get("as_of"),
        facts=_flatten(facts_source), assumptions=list(env.get("assumptions") or []),
        data_sources=list(env.get("data_sources") or []), verdict=env.get("verdict"),
        weights=weights if isinstance(weights, dict) else None, seq=record.get("seq"),
    )
    item.numbers = _shown_numbers(item)
    return item


def _shown_numbers(item: EvidenceItem) -> set[tuple[str, float]]:
    """The numbers of what the Explainer is shown of this item (for_model: say, facts, weights, assumptions):
    the only numbers a claim citing it may state."""
    nums: set[tuple[str, float]] = set()
    view = item.for_model()
    _numbers({k: view.get(k) for k in ("say", "facts", "weights", "assumptions")}, nums)
    return nums


def _item_from_nested(d: dict, parent: EvidenceItem) -> EvidenceItem:
    """An id referenced inside another item (e.g. the evidence behind one governance check)."""
    label = d.get("id") or d.get("metric") or d.get("tool") or "detail"
    status = f": {d['status']}" if d.get("status") else ""
    value = f" (value {d['value']}, limit {d.get('limit')})" if "value" in d else ""
    say = d.get("say") or f"{label}{status}{value}"
    item = EvidenceItem(id=d["evidence_id"], tool=str(d.get("tool") or f"{parent.tool} detail"), agent=parent.agent,
                        say=str(say), period=d.get("period") or parent.period, as_of=d.get("as_of") or parent.as_of,
                        facts=_flatten({k: v for k, v in d.items() if k != "justification"}),
                        seq=parent.seq, parent=parent.id or None)
    item.numbers = _shown_numbers(item)
    return item


def build_catalog(records: list[dict]) -> dict[str, EvidenceItem]:
    catalog: dict[str, EvidenceItem] = {}
    for rec in records:
        found = False
        for env in _envelopes(rec.get("payload")):
            if isinstance(env.get("evidence_id"), str):
                item = _item_from_envelope(env, rec)
                old = catalog.get(item.id)
                # Same id twice (e.g. a sub-agent and the top level checked the same weights): keep the top level's.
                if old is None or not old.say or (item.agent in TOP_LEVEL and old.agent not in TOP_LEVEL):
                    catalog[item.id] = item
                found = True
                parent = item
            else:
                parent = None
            for d in _nested_with_id(env, env):
                if d["evidence_id"] not in catalog:
                    # A record without its own id is indexed under its ledger number (below): that is the parent.
                    base = parent or EvidenceItem(id=rec.get("seq") or "", tool=rec.get("tool", "tool"),
                                                  agent=rec.get("agent") or "orchestrator",
                                                  period=rec.get("period"), as_of=rec.get("as_of"), seq=rec.get("seq"))
                    catalog[d["evidence_id"]] = _item_from_nested(d, base)
        if not found and rec.get("seq") and not rec.get("error"):
            envs = _envelopes(rec.get("payload"))
            env = dict(envs[0]) if envs else {"output": rec.get("payload")}
            env["evidence_id"] = rec["seq"]
            env.setdefault("tool", rec.get("tool"))
            catalog[rec["seq"]] = _item_from_envelope(env, rec)
    return catalog


def governance_items(catalog: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    return [i for i in catalog.values() if i.verdict]
