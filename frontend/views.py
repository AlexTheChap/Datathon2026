"""Dashboard logic without any UI framework (testable): evidence ledger → data for the cards.

The dashboard is personalised by RULES on the stored profile, not by persona name:
  - capital protection (low / low_moderate risk, short horizon) → loss limit, "how much could I hold?", verdict
  - growth (high risk)                                         → where the risk comes from, diversification
  - values (ESG preferences / exclusions)                       → ESG screen, controversies, news
Every card is built only from evidence records (tool outputs), so every number on screen has a source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

EV_RE = re.compile(r"\bev-[0-9a-f]{10}\b")

CARD_TITLES = {
    "verdict": "Governance verdict",
    "loss_limit": "Crisis loss vs your limit",
    "position_slider": "How much could you hold?",
    "allocation": "Current vs proposed allocation",
    "risk_contribution": "Where your risk comes from",
    "correlation": "Is your diversification real?",
    "concentration": "Exposure by sector",
    "metrics": "Asset risk metrics",
    "esg": "ESG screen",
    "news": "Dated news & filings",
    "macro": "Macro backdrop (point-in-time)",
}


def tool_name(record: dict) -> str:
    """'scenarios___stress' (AgentCore Gateway) → 'stress'."""
    return str(record.get("tool", "")).split("___")[-1]


def body(record: dict) -> Any:
    """The tool's returned object (the ledger keeps it under `payload`, often as a 1-element list)."""
    p = record.get("payload")
    if isinstance(p, list) and len(p) == 1:
        return p[0]
    return p


def output(record: dict) -> dict:
    b = body(record)
    if isinstance(b, dict):
        return b.get("output", b) if isinstance(b.get("output", b), dict) else {}
    return {}


@dataclass
class RunEvidence:
    records: list[dict]
    by_tool: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: list[dict]) -> "RunEvidence":
        ev = cls(records=records)
        for r in records:
            if r.get("error"):
                continue
            ev.by_tool.setdefault(tool_name(r), []).append(r)
        return ev

    def last(self, tool: str, agent: Optional[str] = "any") -> Optional[dict]:
        rs = self.by_tool.get(tool, [])
        if agent != "any":
            rs = [r for r in rs if r.get("agent") == agent]
        return rs[-1] if rs else None

    def all(self, tool: str) -> list[dict]:
        return self.by_tool.get(tool, [])


# ---------- personalisation ----------

ESG_EXCLUSIONS = {"controversy", "esg_activity"}  # an asset_class exclusion (e.g. "no crypto") is not ESG


def wants_esg(profile: dict) -> bool:
    prefs = " ".join(map(str, profile.get("preferences") or [])).lower()
    excl = any(isinstance(e, dict) and e.get("type") in ESG_EXCLUSIONS for e in profile.get("exclusions") or [])
    return excl or any(w in prefs for w in ("esg", "controvers", "ethic", "sustainab"))


def focus_for(profile: dict) -> list[str]:
    """Card order for this investor, from the profile's own fields."""
    risk = profile.get("risk_band") or "moderate"
    esg = wants_esg(profile)
    horizon = profile.get("horizon_years") or {}
    short = isinstance(horizon, dict) and (horizon.get("max") or 99) <= 5

    order: list[str] = ["verdict"]
    if risk in ("low", "low_moderate") or short:
        order += ["loss_limit", "position_slider", "allocation"]
    if risk == "high":
        order += ["risk_contribution", "correlation", "concentration", "metrics"]
    if esg:
        order += ["esg", "news"]
    order += ["loss_limit", "position_slider", "allocation", "risk_contribution", "concentration",
              "correlation", "metrics", "esg", "news", "macro"]
    seen: list[str] = []
    for c in order:
        if c not in seen:
            seen.append(c)
    return seen


def focus_label(profile: dict) -> str:
    risk = profile.get("risk_band") or "moderate"
    parts = []
    if risk in ("low", "low_moderate"):
        parts.append("capital protection")
    elif risk == "high":
        parts.append("growth with real diversification")
    else:
        parts.append("balanced growth")
    if wants_esg(profile):
        parts.append("ESG / controversy screen")
    return " · ".join(parts)


# ---------- card data ----------

def verdict(ev: RunEvidence) -> Optional[dict]:
    """Governance on the user's idea (first check) and on the final proposal (last orchestrator check).

    Both matter: "your 20% NVDA idea → BLOCK, the proposed 5% → PASS" is the story the user must see.
    """
    rs = ev.all("governance_check")
    if not rs:
        return None
    first = rs[0]
    final = [r for r in rs if not r.get("agent")][-1:] or rs[-1:]
    final = final[0]

    def unpack(r):
        o = body(r) if isinstance(body(r), dict) else {}
        weights = (r.get("tool_input") or {}).get("weights") if isinstance(r.get("tool_input"), dict) else None
        return {"verdict": o.get("verdict"), "checks": o.get("checks", []), "seq": r.get("seq"),
                "missing_info": o.get("missing_info"), "weights": weights}

    out = unpack(final)
    out["initial"] = unpack(first) if first is not final else None
    return out


def loss_limit(ev: RunEvidence, profile_limit: Optional[float]) -> Optional[dict]:
    rs = ev.all("stress")
    if not rs:
        return None
    rows = []
    for r in rs:
        o, b = output(r), body(r)
        port = o.get("portfolio") or {}
        rows.append({"evidence_id": r.get("evidence_id"), "max_drawdown": port.get("max_drawdown"),
                     "total_return": port.get("total_return"), "loss_at_trough": o.get("loss_at_trough"),
                     "capital": o.get("capital"), "window": o.get("window"),
                     "weights": (b.get("inputs") or {}).get("weights") if isinstance(b, dict) else None,
                     "per_asset": o.get("per_asset_total_return") or {}, "agent": r.get("agent")})
    limit = profile_limit
    pos = ev.last("max_position")
    if limit is None and pos:
        limit = output(pos).get("limit")
    return {"scenarios": rows, "limit": limit}


def position_curve(ev: RunEvidence, profile_limit: Optional[float] = None) -> Optional[dict]:
    """The max_position curve. `limit` is the PROFILE's rule (rules.yaml) when known: the agent may have tested
    another one (`tested_limit`), in which case its `max_weight` answers that other limit and is flagged."""
    r = ev.last("max_position")
    if not r:
        return None
    o = output(r)
    curve = o.get("curve") or []
    if not curve:
        return None
    asset = o.get("asset")
    tested = None  # the user's own idea (e.g. NVDA 20%) from a stress run that includes the asset
    for s in ev.all("stress"):
        w = ((body(s) or {}).get("inputs") or {}).get("weights") or {}
        if asset in w and w[asset] > curve[-1][0]:
            tested = {"weight": w[asset], "max_drawdown": (output(s).get("portfolio") or {}).get("max_drawdown"),
                      "evidence_id": s.get("evidence_id")}
    tested_limit = o.get("limit")
    limit = profile_limit if profile_limit is not None else tested_limit
    return {"asset": asset, "max_weight": o.get("max_weight"), "limit": limit, "tested_limit": tested_limit,
            "limit_differs": tested_limit is not None and limit is not None and abs(limit - tested_limit) > 1e-9,
            "window": o.get("window"), "curve": curve, "evidence_id": r.get("evidence_id"), "tested": tested}


def interpolate(curve: list[list[float]], w: float) -> float:
    if w <= curve[0][0]:
        return curve[0][1]
    for (w0, d0), (w1, d1) in zip(curve, curve[1:]):
        if w0 <= w <= w1:
            return d0 + (d1 - d0) * (w - w0) / (w1 - w0) if w1 > w0 else d0
    return curve[-1][1]


def allocation(ev: RunEvidence, current: Optional[dict[str, float]]) -> Optional[dict]:
    r = ev.last("allocation_subagent")
    proposed = output(r).get("weights") if r else None
    if not proposed and not current:
        return None
    return {"current": current or {}, "proposed": proposed or {},
            "evidence_id": r.get("evidence_id") if r else None,
            "headline": output(r).get("headline") if r else None}


def risk_contribution(ev: RunEvidence) -> Optional[dict]:
    r = ev.last("risk_contribution")
    if not r:
        return None
    o = output(r)
    return {"weights": o.get("weights", {}), "risk": o.get("risk_contribution", {}),
            "portfolio_vol": o.get("portfolio_vol"), "diversification_ratio": o.get("diversification_ratio"),
            "evidence_id": r.get("evidence_id")}


def correlation(ev: RunEvidence) -> Optional[dict]:
    r = ev.last("correlation")
    return {"matrix": output(r).get("matrix", {}), "evidence_id": r.get("evidence_id")} if r else None


def concentration(ev: RunEvidence) -> Optional[dict]:
    r = ev.last("concentration")
    if not r:
        return None
    o = output(r)
    return {"by_sector": o.get("by_sector", {}), "by_position": o.get("by_position", {}),
            "hhi": o.get("hhi"), "effective_positions": o.get("effective_positions"),
            "evidence_id": r.get("evidence_id")}


def metrics(ev: RunEvidence) -> Optional[dict]:
    r = ev.last("metrics")
    return {"assets": output(r), "evidence_id": r.get("evidence_id")} if r else None


def esg(ev: RunEvidence) -> Optional[dict]:
    r = ev.last("esg_screen")
    return {"output": output(r), "say": (body(r) or {}).get("say"), "evidence_id": r.get("evidence_id")} if r else None


def news(ev: RunEvidence) -> Optional[dict]:
    hits, seen = [], set()
    for r in ev.all("search_news"):
        for h in output(r).get("hits", []):
            key = (h.get("title"), h.get("published_date"))
            if key not in seen:
                seen.add(key)
                hits.append({**h, "evidence_id": r.get("evidence_id")})
    if not hits:
        return None
    hits.sort(key=lambda h: h.get("published_date") or "", reverse=True)
    return {"hits": hits}


def macro(ev: RunEvidence) -> Optional[dict]:
    m, fx = ev.last("macro_snapshot"), ev.last("fx_usdcad")
    if not m and not fx:
        return None
    return {"macro": output(m) if m else None, "fx": output(fx) if fx else None,
            "evidence_ids": [r.get("evidence_id") for r in (m, fx) if r]}


# ---------- claim check (every cited evidence_id must exist in the ledger) ----------

def claim_check(answer: str, records: list[dict]) -> list[dict]:
    top = {r.get("evidence_id") for r in records if r.get("evidence_id")}
    blob = " ".join(str(r.get("payload")) for r in records)
    out = []
    for eid in dict.fromkeys(EV_RE.findall(answer or "")):
        status = "verified" if eid in top else ("nested" if eid in blob else "not_found")
        out.append({"evidence_id": eid, "status": status})
    return out


def find_evidence(eid: str, records: list[dict]) -> Optional[dict]:
    """The ledger record that produced `eid` (top-level first, else the record whose payload contains it)."""
    for r in records:
        if r.get("evidence_id") == eid:
            return r
    for r in records:
        if eid in str(r.get("payload")):
            return r
    return None


def link_evidence(answer: str) -> str:
    """Markdown: every ev-… becomes a link to its card in the evidence explorer."""
    return EV_RE.sub(lambda m: f"[{m.group(0)}](#{m.group(0)})", answer or "")


def profile_loss_limit(profile: dict, rules: Optional[dict] = None) -> Optional[float]:
    """Max tolerated crisis loss for the profile's risk band (core/rules.yaml), as a negative number."""
    if not rules:
        return None
    v = ((rules.get("risk_bands") or {}).get(profile.get("risk_band")) or {}).get("max_drawdown_stress")
    return -abs(float(v)) if v is not None else None


# ---------- depth: how much to show, from the investor's financial knowledge ----------
# Same rule as the Explainer prompts (backend/app/prompts/explainer.<level>.md): a beginner gets plain words,
# money amounts and at most a few factors; an advanced investor gets every figure. Any reader can switch on
# "show all data", which is the expert view.

LEVELS = ("simple", "standard", "expert")
LEVEL_FOR_EXPERIENCE = {"beginner": "simple", "intermediate": "standard", "advanced": "expert"}
LEVEL_LABELS = {"simple": "Simple", "standard": "Standard", "expert": "Expert"}
LEVEL_CARDS = {
    "simple": ["loss_limit", "position_slider", "news"],
    "standard": ["verdict", "loss_limit", "position_slider", "allocation", "risk_contribution", "esg", "news"],
    "expert": list(CARD_TITLES),
}
NEWS_LIMIT = {"simple": 3, "standard": 5, "expert": 8}
# Plain-language titles for the simple view (no jargon: "drawdown" -> "drop", "allocation" -> "mix")
SIMPLE_TITLES = {
    "loss_limit": "What a bad year could cost you",
    "position_slider": "How much Nvidia is safe for you?",
    "news": "In the news",
}


def level_for(profile: dict, override: Optional[str] = None, show_all: bool = False) -> str:
    """Detail level: 'show all data' wins, then an explicit choice, then the profile's experience."""
    if show_all:
        return "expert"
    if override in LEVELS:
        return override
    return LEVEL_FOR_EXPERIENCE.get(str(profile.get("experience") or ""), "standard")


def cards_for(profile: dict, level: str) -> list[str]:
    """The profile's card order (focus_for), keeping only the cards this depth shows."""
    allowed = set(LEVEL_CARDS.get(level, LEVEL_CARDS["standard"]))
    return [c for c in focus_for(profile) if c in allowed]


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${abs(x):,.0f}"


def _pct(x: Optional[float], digits: int = 0) -> str:
    return "n/a" if x is None else f"{abs(x) * 100:.{digits}f}%"


def plain_summary(ev: RunEvidence, profile: dict, limit: Optional[float]) -> list[str]:
    """Three or four short sentences a beginner can read, built ONLY from the run's evidence (no LLM)."""
    out: list[str] = []
    v = verdict(ev)
    loss = loss_limit(ev, limit)
    curve = position_curve(ev, limit)
    alloc = allocation(ev, None)
    lim = loss["limit"] if loss else limit
    if loss and loss["scenarios"]:
        worst = min(loss["scenarios"], key=lambda s: s["max_drawdown"] or 0)
        dd, cad = worst["max_drawdown"], worst.get("loss_at_trough")
        money = f"about {_money(cad)} ({_pct(dd)} of your savings)" if cad else f"{_pct(dd)} of its value"
        sentence = f"In a bad year like 2022, the portfolio you asked about could have lost {money} at its worst."
        if lim is not None and dd is not None:
            sentence += (f" That is more than the {_pct(lim)} you said you could accept." if dd < lim
                         else f" That stays within the {_pct(lim)} you said you could accept.")
        out.append(sentence)
    if curve and curve.get("max_weight") is not None and not curve.get("limit_differs"):
        out.append(f"To stay within your limit, {curve['asset']} should be no more than about "
                   f"{_pct(curve['max_weight'])} of your portfolio.")
    if alloc and alloc.get("proposed"):
        top = sorted(alloc["proposed"].items(), key=lambda kv: -kv[1])
        asset = curve["asset"] if curve else None
        if asset and asset in alloc["proposed"]:
            out.append(f"AlphaForge suggests {_pct(alloc['proposed'][asset])} in {asset}, with the rest spread over "
                       f"{len(top) - 1} other investments.")
    if v and v.get("verdict"):
        init = (v.get("initial") or {}).get("verdict")
        if init == "BLOCK" and v["verdict"] == "PASS":
            out.append("Your original idea breaks your own safety rules; the suggested mix respects all of them.")
        elif v["verdict"] == "BLOCK":
            out.append("This idea breaks your own safety rules.")
        elif v["verdict"] == "PASS":
            out.append("This respects all of your safety rules.")
    return out


def answer_markdown(answer: Optional[dict]) -> str:
    """The Explainer's structured answer (run.result["answer"], box ⑤) as Markdown with `[ev-…]` citations, so the
    citation check and the evidence links above work on it. "" when the run has no answer yet."""
    if not answer:
        return ""

    def cite(ids) -> str:
        return f" [{', '.join(ids)}]" if ids else ""

    lines = [answer.get("short_answer", "") + cite(answer.get("short_answer_evidence"))]
    sections = [
        ("Profile checks", [f"**{c['verdict']}** · {c['allocation']}: {c['text']}{cite(c.get('evidence'))}"
                            for c in answer.get("checks") or []]),
        ("Key points", [p["text"] + cite(p.get("evidence")) for p in answer.get("key_points") or []]),
        ("Words used in this answer", [f"**{g['term']}**: {g['meaning']}" for g in answer.get("glossary") or []]),
        ("Limits to keep in mind", [p["text"] + cite(p.get("evidence")) for p in answer.get("limitations") or []]),
        ("Not analysed", list(answer.get("not_analysed") or [])),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"## {title}", *[f"- {i}" for i in items]]
    if answer.get("clarifying_question"):
        lines += ["", f"**Question for you:** {answer['clarifying_question']}"]
    if answer.get("disclaimer"):
        lines += ["", f"_{answer['disclaimer']}_"]
    return "\n".join(lines).strip()


def short_answer(answer: str) -> str:
    """The answer up to its first section break: what a beginner reads first (the rest stays one click away)."""
    text = (answer or "").strip()
    for sep in ("\n## ", "\n---"):
        i = text.find(sep)
        if i > 0:
            text = text[:i]
    return text.strip()
