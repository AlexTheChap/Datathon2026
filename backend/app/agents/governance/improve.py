"""Improve the fit: the mix closest to the person's own that fits their profile better. Deterministic, no LLM.

    result = improve(weights, profile, rules_path, rt, data_dir)

Three plain moves, in order, each one said in the answer:
1. remove what breaks a rule (crypto above the band's limit, an excluded asset class, a company with a verified
   controversy or an excluded activity, as reported by the governance check itself);
2. split the money between growth (stocks, equity funds, crypto) and defensive assets (bonds, gold) at each step
   of 5%, keeping the person's own picks and their proportions inside each part (bonds only: a Canadian bond fund is
   added; stocks only: broad Canadian and US index funds are added);
3. cap any single stock, and crypto in all, below the band's limits; the excess goes to a broad index fund.
Every candidate is scored with the real fit score (fit_score.py), so the suggestion is never better on paper only.
The best score wins; between equal scores (within half a point), the one that changes the least.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .fit_score import RISK_WORDS, fit_score

GROWTH = {"equity", "etf_equity", "crypto"}
DEFENSIVE = {"etf_bond", "etf_commodity"}
BROAD_BY_CURRENCY = {"USD": "SPY", "CAD": "XIU.TO"}
BOND_FUND = "XBB.TO"
HEADROOM = 0.9                           # cap a single stock at 90% of its limit (a pass close to it loses points)


def _norm(w: dict) -> dict:
    s = sum(w.values())
    return {k: v / s for k, v in w.items() if v > 0} if s > 0 else {}


def _turnover(a: dict, b: dict) -> float:
    return sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b)) / 2


def _removals(base: dict, w: dict, uni, band_rules: dict, profile: dict) -> tuple[set, list[str]]:
    """What the governance check says breaks a rule, and why (one plain sentence per holding removed)."""
    out, said = set(), []
    for part in base["parts"]:
        if part["key"] != "rules":
            continue
        for i in part["items"]:
            if i["status"] not in ("BLOCK", "WARN") and not (i["id"] == "CRYPTO" and i["status"] != "PASS"):
                continue
            if i["id"] == "CRYPTO" and band_rules.get("max_crypto", 0) == 0:
                for t in (t for t in w if uni.loc[t, "asset_class"] == "crypto"):
                    out.add(t); said.append(f"Removed {t}: crypto isn't allowed at a {RISK_WORDS[profile['risk_band']]} risk level.")
            elif i["id"].startswith("EXCLUSION_") and isinstance(i.get("measured"), list) and i["status"] == "BLOCK":
                why = "a verified major controversy" if i["id"] == "EXCLUSION_CONTROVERSY" else "excluded in your profile"
                for t in (t for t in i["measured"] if t in w):
                    out.add(t); said.append(f"Removed {t}: {why}.")
    return out, said


def _cap(w: dict, uni, max_single: float, exempt: set, blocked: set,
         max_crypto: float = 0.0) -> tuple[dict, list[tuple[str, float, str]]]:
    """Cap each single stock at HEADROOM × its limit, and crypto in all at HEADROOM × the crypto limit; the excess goes
    to a broad index fund (the stock's currency; USD for crypto)."""
    out, moved = dict(w), []
    crypto = [t for t in w if uni.loc[t, "asset_class"] == "crypto"]
    total_crypto = sum(w[t] for t in crypto)
    if crypto and total_crypto > max_crypto * HEADROOM and max_crypto > 0:
        keep = max_crypto * HEADROOM / total_crypto
        fund = next((f for f in ("SPY", "XIU.TO") if f not in blocked), None)
        if fund:
            for t in crypto:
                out[t] = w[t] * keep
                moved.append((t, out[t], fund))
            out[fund] = out.get(fund, 0) + total_crypto * (1 - keep)
    limit = max_single * HEADROOM
    for t, v in list(w.items()):
        if uni.loc[t, "asset_class"] != "equity" or t in exempt or v <= limit:
            continue
        fund = BROAD_BY_CURRENCY.get(uni.loc[t, "currency"], "SPY")
        if fund in blocked:
            fund = next((f for f in BROAD_BY_CURRENCY.values() if f not in blocked), None)
        if fund is None:
            continue
        out[t] = limit
        out[fund] = out.get(fund, 0) + (v - limit)
        moved.append((t, limit, fund))
    return out, moved


def improve(weights: dict, profile: dict, rules_path: str | Path, rt, data_dir: str | Path = "data") -> dict[str, Any]:
    rules = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
    band = profile["risk_band"]
    band_rules = rules["risk_bands"][band]
    exempt = set(rules["broad_funds_exempt_from_single_position"])
    uni = rt.universe
    w0 = _norm({k: v for k, v in weights.items() if v and v > 0})
    if not w0:
        raise ValueError("Give at least one weight above zero.")
    score = lambda w: fit_score(w, profile, rules_path, rt, data_dir)  # noqa: E731
    base = score(w0)

    # 1. What breaks a rule goes.
    removed, steps = _removals(base, w0, uni, band_rules, profile)
    excluded_classes = {e.get("value") for e in profile.get("exclusions", []) if e.get("type") == "asset_class"}
    for t in list(w0):
        if uni.loc[t, "asset_class"] in excluded_classes and t not in removed:
            removed.add(t); steps.append(f"Removed {t}: excluded in your profile.")
    kept = {t: v for t, v in w0.items() if t not in removed}

    # 2. The two parts, the person's own picks in their own proportions.
    growth = _norm({t: v for t, v in kept.items() if uni.loc[t, "asset_class"] in GROWTH})
    defensive = _norm({t: v for t, v in kept.items() if uni.loc[t, "asset_class"] in DEFENSIVE})
    added = []
    if not defensive:
        defensive = {BOND_FUND: 1.0}; added.append(f"Added {BOND_FUND} (Canadian government and company bonds) for the bond part.")
    if not growth:
        growth = {"XIU.TO": 0.5, "SPY": 0.5}; added.append("Added XIU.TO and SPY (broad Canadian and US index funds) for the stock part.")

    best = None
    for step in range(0, 21):
        g = step / 20
        mix = _norm({t: g * growth.get(t, 0) + (1 - g) * defensive.get(t, 0) for t in set(growth) | set(defensive)})
        mix, caps = _cap(mix, uni, band_rules["max_single_position"], exempt, removed, band_rules.get("max_crypto", 0.0))
        mix = _norm(mix)
        fit = score(mix)
        cand = {"weights": mix, "fit": fit, "growth": g, "caps": caps, "turnover": _turnover(mix, w0)}
        if best is None or fit["score"] > best["fit"]["score"] + 0.5 or (
                abs(fit["score"] - best["fit"]["score"]) <= 0.5 and cand["turnover"] < best["turnover"]):
            best = cand

    if best["fit"]["score"] <= base["score"]:
        return {"found": False, "base": base, "best": None, "weights": None, "changes": [], "steps": [],
                "message": "No mix built from your holdings fits your profile better than this one."}

    g0 = sum(v for t, v in w0.items() if uni.loc[t, "asset_class"] in GROWTH)
    lo, hi = rules["fit_score"]["asset_mix"][band]
    if abs(best["growth"] - g0) >= 0.01:
        steps.append(f"Moved the share in stocks from {g0:.0%} to {best['growth']:.0%} (the usual for a "
                     f"{RISK_WORDS[band]} risk level is {lo:.0%}–{hi:.0%}); your picks keep their proportions inside each part.")
    if any(t in best["weights"] for t in (BOND_FUND, "XIU.TO", "SPY")):
        steps += [a for a in added if any(t in a for t in best["weights"])]
    for t, cap, fund in best["caps"]:
        limit = (f"crypto is limited to {band_rules.get('max_crypto', 0):.0%} in all" if uni.loc[t, "asset_class"] == "crypto"
                 else f"a single company is limited to {band_rules['max_single_position']:.0%}")
        steps.append(f"Capped {t} at {cap:.1%}".replace(".0%", "%") + f" ({limit} at your risk level); the rest goes to "
                     f"{fund}, a broad index fund.")
    weights_out = {k: round(v, 4) for k, v in sorted(best["weights"].items(), key=lambda kv: -kv[1]) if v >= 0.0005}
    changes = sorted(({"ticker": t, "from": round(w0.get(t, 0), 4), "to": weights_out.get(t, 0)}
                      for t in set(w0) | set(weights_out) if abs(w0.get(t, 0) - weights_out.get(t, 0)) >= 0.0005),
                     key=lambda c: -abs(c["to"] - c["from"]))
    return {"found": True, "base": base, "best": best["fit"], "weights": weights_out, "changes": changes,
            "steps": steps, "turnover": round(best["turnover"], 4),
            "method": "Remove what breaks a rule, split stocks and bonds in 5% steps keeping your picks, cap single "
                      "stocks; every candidate scored with the fit score, best score kept (the smallest change on a tie)."}
