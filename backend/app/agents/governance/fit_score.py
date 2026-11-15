"""Fit score: how well a portfolio fits THIS investor's profile, out of 100. Deterministic, no LLM.

    result = fit_score(weights, profile, rules_path, rt, data_dir)

Five parts, thresholds in rules.yaml (`fit_score`, team assumptions); every point lost names its reason, the measured
value and the target, so the score can be checked by hand:

- rules (30): every governance check of `governance_check.check` (same rules, same data): a PASS keeps its points
  (minus a little when close to its limit), a WARN keeps 40%, an UNVERIFIED 50%, a BLOCK none and caps the total at 49;
- risk_level (20): annualised volatility of the portfolio vs the range of the person's risk band (too cautious loses
  points too: a growth investor given only bonds is not a fit);
- asset_mix (20): the share of growth assets (stocks, equity funds, crypto) vs the usual range of the risk band;
- loss (15): the worst peak-to-trough drop of the portfolio vs the loss the person accepts (profile.loss_capacity,
  else the band's typical figure, and the answer says so);
- horizon (5): how long the portfolio stayed below its previous high after that drop vs the shortest horizon;
- diversification (10): effective number of companies in the stock part, equity ETFs looked through.

Measured on the portfolio rebalanced daily to its weights, total returns in CAD, over the data window up to `as_of`.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from . import governance_check as gov

TITLES = {"rules": "Your profile's rules", "risk_level": "Risk level", "asset_mix": "Stocks and bonds",
          "loss": "Loss you can accept",
          "horizon": "Time to recover", "diversification": "Diversification"}
RISK_WORDS = {"low": "low", "low_moderate": "low to moderate", "moderate": "moderate", "high": "high"}
MIN_RULES = {"SHORT_HORIZON_DEFENSIVE"}
GROWTH = {"equity", "etf_equity", "crypto"}   # growth assets; bonds and gold are not   # checks whose limit is a floor (value must be >= limit)


def _pct(x: float, d: int = 1) -> str:
    return f"{x * 100:.{d}f}%".replace(".0%", "%")


def _years(days: float) -> str:
    y = days / 365.25
    return f"{y:.1f} years" if y >= 1 else f"{round(days / 30.4)} months"


def band_of(score: float) -> str:
    return "Good fit" if score >= 85 else "Acceptable fit" if score >= 70 else "Weak fit" if score >= 50 else "Poor fit"


def _persona(profile: dict) -> dict:
    """The profile in the shape `governance_check.check` reads (an "id", the horizon as [min, max])."""
    p = dict(profile)
    p.setdefault("id", p.get("actor_id"))
    h = p.get("horizon_years")
    if isinstance(h, dict):
        p["horizon_years"] = [h.get("min"), h.get("max")]
    elif not h:
        p["horizon_years"] = [None, None]
    return p


# ---------- the five parts ----------

def _rules_part(checks: list[dict], pts: float, cfg: dict) -> tuple[list[dict], bool]:
    items, blocked = [], False
    each = pts / max(1, len(checks))
    for c in checks:
        status = c["status"]
        kept = cfg["kept_share"].get(status, 0.0)
        why = {"PASS": "respected", "WARN": "warning", "UNVERIFIED": "could not be verified",
               "BLOCK": "broken"}.get(status, status.lower())
        value, limit = c.get("value"), c.get("limit")
        if status == "PASS" and isinstance(value, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            use = (limit / value if value > 0 else 0.0) if c["id"] in MIN_RULES else value / limit
            start = cfg["near_limit_from"]
            if use > start:
                kept = 1.0 - cfg["near_limit_max_loss"] * min(1.0, (use - start) / (1.0 - start))
                why = f"respected, but at {use:.0%} of its limit"
        blocked |= status == "BLOCK"
        items.append({"id": c["id"], "status": status, "measured": value, "target": limit, "why": why,
                      "lost": round(each * (1 - kept), 2), "max": round(each, 2), "detail": c.get("detail", "")})
    return items, blocked


def _risk_part(vol: float, band: str, pts: float, cfg: dict) -> dict:
    lo, hi = cfg[band]
    width = (hi - lo) * cfg["zero_at_distance"]
    dist = lo - vol if vol < lo else vol - hi if vol > hi else 0.0
    lost = pts * min(1.0, dist / width) if width > 0 else (pts if dist else 0.0)
    why = (f"{_pct(vol)} a year, inside the {_pct(lo, 0)}–{_pct(hi, 0)} range of a {RISK_WORDS[band]} risk level"
           if not dist else
           f"{_pct(vol)} a year: {'too cautious' if vol < lo else 'too bold'} for a {RISK_WORDS[band]} risk level "
           f"({_pct(lo, 0)}–{_pct(hi, 0)})")
    return {"id": "VOLATILITY", "measured": round(vol, 4), "target": [lo, hi], "why": why,
            "lost": round(lost, 2), "max": pts}


def _mix_part(w: dict, uni, band: str, pts: float, cfg: dict) -> dict:
    growth = sum(v for t, v in w.items() if uni.loc[t, "asset_class"] in GROWTH)
    lo, hi = cfg[band]
    dist = lo - growth if growth < lo else growth - hi if growth > hi else 0.0
    lost = pts * min(1.0, dist / cfg["zero_at_distance"])
    rng = f"{_pct(lo, 0)}–{_pct(hi, 0)}"
    why = (f"{_pct(growth, 0)} in stocks, inside the {rng} usual for a {RISK_WORDS[band]} risk level" if not dist
           else f"{_pct(growth, 0)} in stocks: {'too little' if growth < lo else 'too much'} for a "
                f"{RISK_WORDS[band]} risk level ({rng})")
    return {"id": "GROWTH_SHARE", "measured": round(growth, 4), "target": [lo, hi], "why": why,
            "lost": round(lost, 2), "max": pts}


def _loss_part(mdd: float, profile: dict, band: str, pts: float, cfg: dict) -> dict:
    said = profile.get("loss_capacity")
    cap = said if said is not None else cfg["typical_by_band"][band]
    whose = "the loss you accept" if said is not None else f"the usual limit for a {RISK_WORDS[band]} risk level (your profile gives none)"
    over = max(0.0, mdd - cap)
    lost = pts * min(1.0, over / (cap * (cfg["zero_at_ratio"] - 1))) if cap > 0 else (pts if over else 0.0)
    why = (f"worst drop {_pct(mdd)}, within {whose} ({_pct(cap, 0)})" if not over
           else f"worst drop {_pct(mdd)}, more than {whose} ({_pct(cap, 0)})")
    return {"id": "MAX_DRAWDOWN", "measured": round(-mdd, 4), "target": -cap, "why": why,
            "lost": round(lost, 2), "max": pts, "assumed_limit": said is None,
            "double": bool(said is not None and mdd >= cap * cfg["zero_at_ratio"])}


def _horizon_part(underwater_days: float, recovered: bool, profile: dict, pts: float, cfg: dict) -> dict:
    h = profile.get("horizon_years") or {}
    years = h.get("min") if isinstance(h, dict) else None
    years = years if years is not None else (h.get("max") if isinstance(h, dict) else None)
    spent = _years(underwater_days) + ("" if recovered else ", and not yet recovered")
    if not years:
        return {"id": "RECOVERY", "measured": round(underwater_days), "target": None, "lost": 0.0, "max": pts,
                "why": f"{spent} below its previous high; no horizon in your profile to compare with"}
    share = underwater_days / (years * 365.25)
    full, zero = cfg["full_until_share"], cfg["zero_at_share"]
    # Not recovered yet: the time so far is a floor (the real figure can only be longer).
    lost = pts * min(1.0, max(0.0, (share - full) / (zero - full)))
    why = f"{spent} below its previous high, for a horizon of {years} year{'s' if years != 1 else ''}"
    return {"id": "RECOVERY", "measured": round(underwater_days), "target": round(years * 365.25 * full),
            "why": why, "lost": round(lost, 2), "max": pts}


def _diversification_part(rt, w: dict, pts: float, cfg: dict) -> dict:
    """Effective number of companies (1/HHI) in the stock part. Equity ETFs are decomposed with their holdings; one
    that can't be is counted as a basket of `full_from` equal companies (a broad fund is diversified by design)."""
    uni = rt.universe
    lt = rt.lookthrough(w) if getattr(rt, "holdings", None) else None
    rows = ([(i, float(r.weight), r.asset_class) for i, r in lt.iterrows()] if lt is not None
            else [(t, v, uni.loc[t, "asset_class"]) for t, v in w.items()])
    stock = [(i, v, c) for i, v, c in rows if c in ("equity", "etf_equity", "crypto") and v > 0]
    share = sum(v for _, v, _ in stock)
    if share < 0.05:
        return {"id": "EFFECTIVE_COMPANIES", "measured": None, "target": cfg["full_from"], "lost": 0.0, "max": pts,
                "why": "almost no stocks: nothing to spread"}
    hhi = sum((v / share) ** 2 / (cfg["full_from"] if c == "etf_equity" else 1) for _, v, c in stock)
    eff = 1 / hhi
    full, zero = cfg["full_from"], cfg["zero_at"]
    lost = pts * min(1.0, max(0.0, (math.log(full) - math.log(eff)) / (math.log(full) - math.log(zero))))
    why = (f"the stock part is spread like {eff:.0f} equal companies" if eff >= full
           else f"the stock part is spread like only {eff:.1f} equal companies (full points from {full})")
    return {"id": "EFFECTIVE_COMPANIES", "measured": round(eff, 1), "target": full, "why": why,
            "lost": round(lost, 2), "max": pts}


# ---------- the score ----------

def fit_score(weights: dict, profile: dict, rules_path: str | Path, rt, data_dir: str | Path = "data") -> dict[str, Any]:
    rules = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
    cfg = rules["fit_score"]
    pts = cfg["points"]
    w = {k: v for k, v in weights.items() if v and v > 0}
    if not w:
        raise ValueError("Give at least one weight above zero.")
    total = sum(w.values())
    w = {k: v / total for k, v in w.items()}
    band = profile["risk_band"]

    verdict = gov.check(w, _persona(profile), rules_path, rt, data_dir)
    returns = rt.portfolio_returns(w)
    stats = rt._stats(returns)
    wealth = (1 + returns).cumprod()
    peak, trough = wealth.loc[:stats["dd_trough"]].idxmax(), stats["dd_trough"]
    recovered = stats["dd_recovery"] is not None
    end = stats["dd_recovery"] or str(returns.index.max().date())
    underwater = (returns.index[returns.index <= end].max() - peak).days

    rule_items, blocked = _rules_part(verdict["checks"], pts["rules"], cfg["rules"])
    parts = [
        {"key": "rules", "items": rule_items},
        {"key": "risk_level", "items": [_risk_part(stats["ann_vol"], band, pts["risk_level"], cfg["risk_level"])]},
        {"key": "asset_mix", "items": [_mix_part(w, rt.universe, band, pts["asset_mix"], cfg["asset_mix"])]},
        {"key": "loss", "items": [_loss_part(-stats["max_drawdown"], profile, band, pts["loss"], cfg["loss"])]},
        {"key": "horizon", "items": [_horizon_part(underwater, recovered, profile, pts["horizon"], cfg["horizon"])]},
        {"key": "diversification", "items": [_diversification_part(rt, w, pts["diversification"], cfg["diversification"])]},
    ]
    for p in parts:
        p["title"] = TITLES[p["key"]]
        p["max"] = pts[p["key"]]
        p["points"] = round(max(0.0, p["max"] - sum(i["lost"] for i in p["items"])), 1)
    raw = sum(p["points"] for p in parts)
    # Caps: a broken rule, or a worst drop at least twice the loss the person said they can take, can't read as a fit.
    caps = []
    if blocked:
        caps.append({"at": cfg["rules"]["block_caps_total_at"], "why": "a rule of your profile is broken"})
    if parts[3]["items"][0]["double"]:
        caps.append({"at": cfg["loss"]["stated_double_caps_total_at"],
                     "why": "its worst drop is at least twice the loss you said you can take"})
    limit = min((c["at"] for c in caps), default=100)
    score = int(round(min(raw, limit)))
    return {
        "score": score, "out_of": 100, "band": band_of(score), "caps": [c for c in caps if raw > c["at"]],
        "verdict": verdict["verdict"], "parts": parts,
        "measured": {"ann_vol": stats["ann_vol"], "max_drawdown": stats["max_drawdown"],
                     "drawdown_peak": str(peak.date()), "drawdown_trough": trough, "recovered_on": stats["dd_recovery"],
                     "days_below_previous_high": underwater},
        "weights": {k: round(v, 4) for k, v in sorted(w.items(), key=lambda kv: -kv[1])},
        "period": f"{returns.index.min().date()}/{returns.index.max().date()}",
        "method": "Out of 100: your profile's limits 30, risk level 20, stocks and bonds 20, loss 15, horizon 5, "
                  "diversification 10. Measured on the portfolio's past returns in Canadian dollars, dividends "
                  "reinvested, rebalanced to its weights every day.",
        "assumptions": ["These limits are guidelines to help keep your portfolio suited to your profile. They are not regulatory requirements.",
                        "Past returns do not predict future returns: the worst drop and the volatility are historical.",
                        "Funds are looked at through the companies they hold today."],
        "rules_version": rules["version"], "fit_version": cfg["version"],
    }
