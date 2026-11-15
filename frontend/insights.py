"""What the dashboard says ABOUT a run, by reader level: fit score, Venn sets, explanation, key parameters.

All deterministic and built from the run's evidence (and its crisis replay): no LLM writes these words or
numbers, so each one traces back to a tool result.
"""
from __future__ import annotations

from typing import Optional

from views import RunEvidence, body, concentration, output, position_curve, risk_contribution, verdict


# ---------- fit score out of 10 ----------
# How well an idea fits THIS profile's own rules (not a judgement of the asset). Every point lost has a reason.

def fit_score(checks: list[dict], risk: Optional[dict] = None) -> dict:
    score, why = 10.0, []
    for c in checks or []:
        status, rid = c.get("status"), c.get("id") or c.get("rule")
        val, lim = c.get("value"), c.get("limit")
        if status == "BLOCK" and rid == "MAX_STRESS_LOSS" and isinstance(val, (int, float)) \
                and isinstance(lim, (int, float)) and lim:
            excess = (abs(val) - abs(lim)) / abs(lim)
            pts = 3 + min(2.0, 5 * max(0.0, excess))   # 3 for breaking the limit, up to 2 more for how far
            why.append({"reason": f"Crisis loss {abs(val):.1%} is past the {abs(lim):.0%} limit ({excess:.0%} over)",
                        "points": -round(pts, 1), "evidence_id": c.get("evidence_id") or ""})
            score -= pts
        elif status == "BLOCK":
            why.append({"reason": f"Rule {rid} blocks it", "points": -2.0, "evidence_id": c.get("evidence_id") or ""})
            score -= 2
        elif status == "WARN":
            why.append({"reason": f"Rule {rid} warns", "points": -1.0, "evidence_id": c.get("evidence_id") or ""})
            score -= 1
    if risk and risk.get("risk"):
        top = max(risk["risk"], key=risk["risk"].get)
        if risk["risk"][top] > 0.5:
            why.append({"reason": f"{top} carries {risk['risk'][top]:.0%} of the portfolio's risk",
                        "points": -1.0, "evidence_id": risk.get("evidence_id") or ""})
            score -= 1
    score = max(0.0, min(10.0, round(score * 2) / 2))
    band = ("Poor fit" if score < 3.5 else "Risky fit" if score < 6.5 else "Acceptable fit" if score < 8.5
            else "Good fit")
    return {"score": score, "band": band, "why": why}


def scores(ev: RunEvidence) -> dict:
    """Score of the user's idea (first governance check) and of the final proposal (last check)."""
    v = verdict(ev)
    if not v:
        return {}
    rc = risk_contribution(ev)
    idea_checks = (v.get("initial") or v)["checks"]
    out = {"idea": fit_score(idea_checks, rc)}
    if v.get("initial"):
        out["proposal"] = fit_score(v["checks"])  # its own risk split was not measured by a tool: not scored
    return out


# ---------- Venn diagrams ----------

def venn_idea_vs_allowed(profile: dict, curve: Optional[dict], replay: Optional[dict]) -> Optional[dict]:
    """Your idea ∩ what your profile allows = the suggested mix."""
    series = (replay or {}).get("series") or {}
    idea, prop = series.get("idea"), series.get("proposal")
    if not idea:
        return None
    asset = (curve or {}).get("asset")
    left = []
    if asset and asset in idea["weights"]:
        left.append(f"{asset} {idea['weights'][asset]:.0%}")
    left.append(f"worst drop {abs(idea['max_drawdown']):.0%}")
    right = []
    if curve and asset and not curve.get("limit_differs"):
        right.append(f"{asset} ≤ {curve['max_weight']:.0%}")
    if curve and curve.get("limit") is not None:
        right.append(f"drop ≤ {abs(curve['limit']):.0%}")
    for e in profile.get("exclusions") or []:
        if isinstance(e, dict) and e.get("type") == "asset_class" and e.get("value"):
            right.append(f"no {e['value']}")
    both = []
    if prop:
        if asset and asset in prop["weights"]:
            both.append(f"{asset} {prop['weights'][asset]:.0%}")
        both.append(f"drop {abs(prop['max_drawdown']):.0%}")
    return {"left_title": "Your idea", "right_title": "What your profile allows", "both_title": "Suggested",
            "left": left, "right": right, "both": both}


def venn_holdings(current: Optional[dict], proposed: Optional[dict]) -> Optional[dict]:
    """Current portfolio ∩ proposed portfolio: kept (with its change), sold, added."""
    cur = {k: v for k, v in (current or {}).items() if v > 0}
    pro = {k: v for k, v in (proposed or {}).items() if v > 0}
    if not cur or not pro:
        return None
    return {"left_title": "Current", "right_title": "Proposed", "both_title": "Kept",
            "left": [f"{t} {cur[t]:.0%}" for t in sorted(set(cur) - set(pro))],
            "right": [f"{t} {pro[t]:.0%}" for t in sorted(set(pro) - set(cur), key=lambda t: -pro[t])],
            "both": [f"{t} {cur[t]:.0%}→{pro[t]:.0%}" for t in sorted(set(cur) & set(pro), key=lambda t: -pro[t])]}


def venn_risk_drivers(risk: Optional[dict], per_asset: Optional[dict], threshold: float = 0.10) -> Optional[dict]:
    """Big weight ∩ big share of risk ∩ big 2022 loss: whatever sits in the centre is the problem."""
    if not risk or not risk.get("risk"):
        return None
    w, r, loss = risk.get("weights") or {}, risk["risk"], per_asset or {}
    sets = {"A": {t for t, v in w.items() if v >= threshold},
            "B": {t for t, v in r.items() if v >= threshold},
            "C": {t for t, v in loss.items() if v <= -threshold}}
    universe = set(w) | set(r) | set(loss)
    regions: dict[str, list[str]] = {}
    for t in sorted(universe):
        key = "".join(k for k in "ABC" if t in sets[k])
        if key:
            regions.setdefault(key, []).append(t)
    return {"titles": {"A": f"Weight ≥ {threshold:.0%}", "B": f"Risk share ≥ {threshold:.0%}",
                       "C": f"Lost ≥ {threshold:.0%} in 2022"},
            "regions": regions, "outside": sorted(universe - set().union(*sets.values()))}


# ---------- explanation by level ----------

def analysis_paragraph(ev: RunEvidence, profile: dict, limit: Optional[float], replay: Optional[dict]) -> str:
    """Standard / expert explanation: technical vocabulary, one paragraph."""
    parts = []
    series = (replay or {}).get("series") or {}
    idea, prop = series.get("idea"), series.get("proposal")
    curve = position_curve(ev, limit)
    rc = risk_contribution(ev)
    v = verdict(ev)
    band = str(profile.get("risk_band") or "").replace("_", "-")
    if idea:
        cap = (replay or {}).get("capital") or 0
        parts.append(f"A 2022 replay (stocks and bonds falling together) of the requested allocation gives a maximum "
                     f"drawdown of {idea['max_drawdown']:.1%} (≈ ${cap * abs(idea['max_drawdown']):,.0f} peak to "
                     f"trough, low on {idea['trough_date']})"
                     + (f", against the {abs(limit):.0%} limit of a {band} profile" if limit else "") + ".")
    if rc and rc.get("risk"):
        top = max(rc["risk"], key=rc["risk"].get)
        parts.append(f"{top} accounts for {rc['risk'][top]:.0%} of portfolio risk for {rc['weights'].get(top, 0):.0%} "
                     f"of capital (volatility {rc['portfolio_vol']:.1%}, diversification ratio "
                     f"{rc['diversification_ratio']}).")
    if curve:
        parts.append(f"The max-position search caps {curve['asset']} at ≈ {curve['max_weight']:.1%} for a "
                     f"{abs(curve['tested_limit']):.0%} loss limit.")
    if prop and v and curve:
        parts.append(f"The allocation agent proposes {prop['weights'].get(curve['asset'], 0):.0%} {curve['asset']}; "
                     f"it draws down {prop['max_drawdown']:.1%} in the same replay and governance rates it "
                     f"{v['verdict']}.")
    return " ".join(parts)


# ---------- the parameters an advanced reader checks ----------

def key_parameters(ev: RunEvidence, replay: Optional[dict]) -> list[dict]:
    rows: list[dict] = []

    def add(name, value, why, eid=None):
        if value not in (None, ""):
            rows.append({"parameter": name, "value": str(value), "why it matters": why, "evidence": eid or ""})

    worst = None
    for r in ev.all("stress"):
        dd = (output(r).get("portfolio") or {}).get("max_drawdown")
        if dd is not None and (worst is None or dd < (output(worst).get("portfolio") or {}).get("max_drawdown")):
            worst = r
    if worst:
        p, eid = output(worst).get("portfolio") or {}, worst.get("evidence_id")
        add("Max drawdown (2022 replay)", f"{p.get('max_drawdown', 0):.1%}", "Worst peak-to-trough loss in the crisis", eid)
        add("Total return (2022 replay)", f"{p.get('total_return', 0):+.1%}", "Where the year ended", eid)
        add("Annualised volatility", f"{p.get('ann_vol', 0):.1%}", "How much the value swings", eid)
        add("Downside volatility", f"{p.get('downside_vol', 0):.1%}", "Swings on falling days only", eid)
        add("Daily VaR 95%", f"{p.get('var95_daily', 0):.2%}", "A bad day (1 in 20) loses at least this", eid)
        add("Daily CVaR 95%", f"{p.get('cvar95_daily', 0):.2%}", "Average loss on those bad days", eid)
        add("Recovery date", p.get("dd_recovery") or "not recovered in the window", "When the loss was made back", eid)
    rc = risk_contribution(ev)
    if rc:
        add("Portfolio volatility (5 years)", f"{rc['portfolio_vol']:.1%}", "Long-run risk of the requested mix",
            rc["evidence_id"])
        add("Diversification ratio", rc["diversification_ratio"], "Above 1: holdings offset each other", rc["evidence_id"])
    cn = concentration(ev)
    if cn:
        add("Effective number of positions", cn["effective_positions"], "1/HHI, ETFs looked through", cn["evidence_id"])
        add("HHI concentration", cn["hhi"], "0 = spread out, 1 = a single position", cn["evidence_id"])
    cv = position_curve(ev)
    if cv:
        add(f"Max {cv['asset']} weight", f"{cv['max_weight']:.1%} at a {abs(cv['tested_limit']):.0%} limit",
            "Largest position keeping the crisis loss within the limit", cv["evidence_id"])
    for r in ev.all("search_news")[:1]:
        add("News date filter", f"published ≤ {(body(r) or {}).get('as_of')}",
            "No information from after the analysis date", r.get("evidence_id"))
    if replay:
        add("Scenario", replay.get("label"), "A historical replay, not a forecast")
    return rows
