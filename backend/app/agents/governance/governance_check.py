"""
Reference check of an allocation against a profile and backend/app/core/rules.yaml.
Deterministic code, no LLM. Returns a PASS / WARN / BLOCK verdict with every check.

    from risk_tools import RiskTools  # add backend/app/tools to sys.path
    from governance_check import check
    rt = RiskTools("data/curated")
    check({"XIU.TO": .3, "NVDA": .2, ...}, persona_dict, "backend/app/core/rules.yaml", rt)

ESG/controversy exclusions use data/curated/esg_screen.csv and
controversies.csv if they exist; otherwise the check returns UNVERIFIED (never PASS).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

RANK = {"PASS": 0, "UNVERIFIED": 1, "WARN": 2, "BLOCK": 3}


SEVERITY_RANK = {"minor": 1, "moderate": 2, "major": 3}


def _controversy_check(w: dict, uni: pd.DataFrame, path: Path, ex: dict, block_status: str,
                       lt: pd.DataFrame | None = None, indirect: dict | None = None):
    """Only rows with status=verified (reviewed by a human) count. A directly held stock
    with no verified row stays UNVERIFIED: absence of evidence ≠ evidence of absence.
    With ETF decomposition (`lt`), a controversial company held VIA ETFs above
    indirect["max_weight"] gets indirect["severity"] (WARN by default): it is a fund choice, not a stock choice.
    Returns (id, status, value, limit, detail) for add()."""
    df = pd.read_csv(path, dtype=str).fillna("")
    min_rank = SEVERITY_RANK[ex.get("min_severity", "major")]
    flagged = df[(df["status"] == "verified") & (df["severity"].map(SEVERITY_RANK).fillna(0) >= min_rank)]
    stocks = [k for k in w if uni.loc[k, "asset_class"] == "equity"]
    verified = df[(df["status"] == "verified") & df["ticker"].isin(stocks)]
    hits = flagged[flagged["ticker"].isin(stocks)]
    blocked = sorted(hits["ticker"].unique())
    unverified = sorted(set(stocks) - set(verified["ticker"]))
    parts = []
    if blocked:
        status = block_status
        parts.append("Direct: " + "; ".join(f"{r.ticker} ({r.severity}): {r.description} [{r.source_doc}]"
                                                for r in hits.itertuples()))
    elif unverified:
        status = "UNVERIFIED"
        parts.append(f"No verified row for: {', '.join(unverified)}.")
    else:
        status = "PASS"
    if lt is not None:
        ind = indirect or {}
        lim, ind_sev = float(ind.get("max_weight", 0.01)), ind.get("severity", "WARN")
        via = lt.loc[lt.index.isin(flagged["ticker"]) & (lt["via_etf"] > 0), "via_etf"]
        over = via[via > lim]
        if len(via):
            parts.append("Via ETFs: " + ", ".join(f"{t} {v:.1%}" for t, v in via.sort_values(ascending=False).items())
                         + f" (threshold {lim:.0%}).")
        if len(over) and RANK[ind_sev] > RANK[status]:
            status = ind_sev
        parts.append("Within ETFs, only companies with verified rows are detected.")
    else:
        parts.append("Directly held stocks only (etf_holdings.csv missing: ETFs not decomposed).")
    return "EXCLUSION_CONTROVERSY", status, blocked, ex.get("min_severity", "major"), " ".join(parts)


def _esg_activity_check(w: dict, uni: pd.DataFrame, path: Path, ex: dict, block_status: str,
                        lt: pd.DataFrame | None, cfg: dict):
    """Activity-based exclusion (e.g. "fossil_fuels") from curated/esg_screen.csv.
    Only reviewed rows (non-empty reviewer) count; revenue_share as a fraction (0.25) or in % (25).
    - directly held stock with revenue_share ≥ threshold -> BLOCK;
    - directly held stock with no reviewed row for this activity -> UNVERIFIED;
    - excluded company held via decomposed ETFs above indirect.max_weight -> WARN.
    A reviewed row with revenue_share = 0 proves the activity is absent."""
    act = ex["value"]
    thr = float(ex.get("min_revenue_share", cfg.get("min_revenue_share", 0.10)))
    df = pd.read_csv(path, dtype=str).fillna("")
    df = df[(df["activity"] == act) & (df["reviewer"].str.strip() != "")].copy()
    share = pd.to_numeric(df["revenue_share"].str.replace(",", ".").str.rstrip("%"), errors="coerce")
    df["share"] = share.where(share <= 1, share / 100)
    df = df.sort_values("fiscal_year").groupby("ticker").tail(1)        # most recent fiscal year
    excluded = df.loc[df["share"] >= thr].set_index("ticker")["share"]

    stocks = [k for k in w if uni.loc[k, "asset_class"] == "equity"]
    blocked = sorted(set(stocks) & set(excluded.index))
    unverified = sorted(set(stocks) - set(df["ticker"]))
    parts = []
    if blocked:
        status = block_status
        parts.append("Direct: " + ", ".join(f"{t} ({excluded[t]:.0%} of revenue)" for t in blocked) + ".")
    elif unverified:
        status = "UNVERIFIED"
        parts.append(f"No reviewed row for \"{act}\": {', '.join(unverified)}.")
    else:
        status = "PASS"
    if lt is not None:
        ind = cfg.get("indirect", {})
        lim, ind_sev = float(ind.get("max_weight", 0.01)), ind.get("severity", "WARN")
        via = lt.loc[lt.index.isin(excluded.index) & (lt["via_etf"] > 0), "via_etf"]
        if len(via):
            parts.append("Via ETFs: " + ", ".join(f"{t} {v:.1%}" for t, v in via.sort_values(ascending=False).items())
                         + f" (threshold {lim:.0%}).")
        if (via > lim).any() and RANK[ind_sev] > RANK[status]:
            status = ind_sev
        parts.append("Within ETFs, only companies with a reviewed row are detected.")
    else:
        parts.append("Directly held stocks only (etf_holdings.csv missing).")
    return f"EXCLUSION_ESG_{act.upper()}", status, blocked, thr, " ".join(parts)


def _esg_index_check(w: dict, uni: pd.DataFrame, path: Path, cfg: dict, lt: pd.DataFrame | None):
    """Directly held stocks missing from the reference ESG index (by currency) -> cfg["severity"].
    Wording: "excluded from the X index", never "fails ESG standards" (the reason is not published).
    The share of broad ETFs (SPY, XIU) missing from the ESG index is described without changing the status."""
    s = pd.read_csv(path, keep_default_na=False)
    s["in_esg_fund"] = s["in_esg_fund"].astype(str).str.lower() == "true"
    by_ccy = cfg.get("reference_by_currency", {})
    flagged, unknown, parts = [], [], []
    for k in w:
        if uni.loc[k, "asset_class"] != "equity":
            continue
        ref = by_ccy.get(uni.loc[k, "currency"])
        row = s[(s["ticker"] == k) & (s["esg_fund"] == ref)]
        if row.empty or row["status"].iloc[0] == "undetermined_recent_addition":
            unknown.append(k)
        elif not row["in_esg_fund"].iloc[0]:
            flagged.append(k)
            parts.append(f"{k} : {row['wording'].iloc[0]}.")
    status = cfg.get("severity", "WARN") if flagged else ("UNVERIFIED" if unknown else "PASS")
    if unknown:
        parts.append(f"No information: {', '.join(unknown)}.")
    if lt is not None:
        for parent, ref in cfg.get("reference_by_parent", {}).items():
            if parent not in w:
                continue
            g = s[(s["parent_fund"] == parent) & (s["esg_fund"] == ref)]
            share = float(g.loc[g["status"] == "absent_from_esg_index", "weight_parent"].sum())
            parts.append(f"Via {parent}: {share:.0%} of the fund ({share * w[parent]:.1%} of the portfolio) "
                         f"is absent from {ref}; {ref} would be the ESG equivalent.")
    return "ESG_INDEX_PREFERENCE", status, flagged, None, " ".join(parts) or "No directly held stocks."


def _band(profile: dict, rules: dict) -> dict:
    return rules["risk_bands"][profile["risk_band"]]


# ---------- the verdict in the reader's words (tool `say`, optimizer messages) ----------

def _plain_pct(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "the set limit"


def plain_check(c: dict) -> str:
    """One governance check in the reader's words: what the rule protects, never its id or status code."""
    cid, value, limit = c["id"], c.get("value"), c.get("limit")
    if c["status"] == "UNVERIFIED":
        return "one of your exclusions could not be checked (the data is missing)"
    if cid == "SINGLE_POSITION":
        return f"one company is {_plain_pct(value)} of the money, above your {_plain_pct(limit)} limit for a single company"
    if cid == "SECTOR":
        return f"one part of the economy is {_plain_pct(value)} of the money, above your {_plain_pct(limit)} limit"
    if cid == "CRYPTO":
        return f"crypto is {_plain_pct(value)} of the money, above your {_plain_pct(limit)} limit"
    if cid == "SHORT_HORIZON_VOLATILITY":
        return (f"you need the money within a few years, and very volatile holdings are {_plain_pct(value)} of it, "
                f"above the {_plain_pct(limit)} cap")
    if cid == "SHORT_HORIZON_DEFENSIVE":
        return (f"you need the money within a few years, and only {_plain_pct(value)} is in bond funds, "
                f"below the {_plain_pct(limit)} that steadies a short horizon")
    if cid == "ESG_INDEX_PREFERENCE":
        return "some holdings are left out of a reference ESG index, which matters for your ESG preference"
    if cid.startswith("EXCLUSION"):
        return "it holds something your profile excludes"
    return "one of your profile's limits is not met"


def plain_verdict(result: dict) -> str:
    """The overall verdict as a sentence a non-expert reads (the codes stay in `verdict` and `checks`)."""
    by = {s: [plain_check(c) for c in result["checks"] if c["status"] == s] for s in ("BLOCK", "WARN", "UNVERIFIED")}
    watch = f" Worth keeping an eye on: {'; '.join(by['WARN'])}." if by["WARN"] else ""
    unknown = f" Not confirmed: {'; '.join(by['UNVERIFIED'])}." if by["UNVERIFIED"] else ""
    if by["BLOCK"]:
        return f"This mix goes against your profile: {'; '.join(by['BLOCK'])}.{watch}{unknown}"
    if by["UNVERIFIED"]:
        return f"This mix could not be fully checked against your profile, so it is not confirmed.{watch}{unknown}"
    if by["WARN"]:
        return f"This mix stays within your profile's firm limits.{watch}"
    return "This mix fits every limit of your profile."


def check(weights: dict, profile: dict, rules_path: str | Path, rt, data_dir: str | Path = "data") -> dict:
    rules = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
    band = _band(profile, rules)
    sev = rules["severities"]
    tot = sum(weights.values())
    w = {k: v / tot for k, v in weights.items()}
    uni = rt.universe
    checks, missing = [], []

    def add(cid, status, value, limit, rule, evidence_id=None, detail=""):
        checks.append({"id": cid, "status": status, "value": value, "limit": limit,
                       "rule": rule, "justification": rules["justifications"].get(rule, ""),
                       "evidence_id": evidence_id, "detail": detail})

    # 1-2. Single position and sector. With data/curated/etf_holdings.csv: equity ETFs are decomposed
    # (NVDA held directly + via SPY/QQQ counts as a single exposure).
    exempt = set(rules["broad_funds_exempt_from_single_position"])
    single = {k: v for k, v in w.items() if k not in exempt}
    lt = rt.lookthrough(w) if rt.holdings else None
    if lt is not None:
        eq = lt[lt["asset_class"] == "equity"]
        if len(eq):
            top, r = eq.index[0], eq.iloc[0]
            add("SINGLE_POSITION", "PASS" if r.weight <= band["max_single_position"] else sev["max_single_position"],
                round(float(r.weight), 4), band["max_single_position"], "max_single_position",
                detail=f"{top}: {r.direct:.1%} direct + {r.via_etf:.1%} via ETFs (equity ETFs decomposed)")
            sec = eq.groupby("sector")["weight"].sum()
            unclassified = float(sec.pop("Unclassified")) if "Unclassified" in sec else 0.0
            if len(sec):
                s_top = sec.idxmax()
                add("SECTOR", "PASS" if sec.max() <= band["max_sector"] else sev["max_sector"],
                    round(float(sec.max()), 4), band["max_sector"], "max_sector",
                    detail=f"{s_top} (equity ETFs decomposed; unclassified: {unclassified:.1%} of the portfolio)")
    elif single:
        top = max(single, key=single.get)
        add("SINGLE_POSITION", "PASS" if single[top] <= band["max_single_position"] else sev["max_single_position"],
            round(single[top], 4), band["max_single_position"], "max_single_position", detail=top)

    # 3. Sector (without ETF decomposition: broad ETFs excluded from the calculation)
    sec = pd.Series({k: v for k, v in single.items()}).groupby(uni["sector"]).sum() \
        if (single and lt is None) else pd.Series(dtype=float)
    if len(sec):
        s_top = sec.idxmax()
        add("SECTOR", "PASS" if sec.max() <= band["max_sector"] else sev["max_sector"],
            round(float(sec.max()), 4), band["max_sector"], "max_sector",
            detail=f"{s_top} (direct holdings only; ETF decomposition required for the full figure)")

    # 4. Crypto
    crypto = sum(v for k, v in w.items() if uni.loc[k, "asset_class"] == "crypto")
    add("CRYPTO", "PASS" if crypto <= band["max_crypto"] else sev["max_crypto"],
        round(crypto, 4), band["max_crypto"], "max_crypto")

    # 5. Horizon rules
    h_min, h_max = profile["horizon_years"]
    h_ref = h_min  # prudence: use the lower bound of the horizon (nearest need)
    met = rt.metrics(list(w))["output"]
    for hr in rules["horizon_rules"]:
        if h_ref is not None and h_ref < hr["when"]["horizon_years_max_lt"]:
            if "max_weight_high_vol_assets" in hr:
                hv = sum(v for k, v in w.items() if met[k]["ann_vol"] > hr["high_vol_threshold"])
                add(hr["id"], "PASS" if hv <= hr["max_weight_high_vol_assets"] else hr["severity"],
                    round(hv, 4), hr["max_weight_high_vol_assets"], "horizon_rules",
                    detail=hr["justification"])
            if "min_defensive_weight" in hr:
                d = sum(v for k, v in w.items() if uni.loc[k, "asset_class"] == "etf_bond")
                add(hr["id"], "PASS" if d >= hr["min_defensive_weight"] else hr["severity"],
                    round(d, 4), hr["min_defensive_weight"], "horizon_rules", detail=hr["justification"])

    # 6. User exclusions
    for ex in profile.get("exclusions", []):
        if ex["type"] == "asset_class":
            bad = [k for k in w if uni.loc[k, "asset_class"] == ex["value"]]
            add(f"EXCLUSION_{ex['value'].upper()}", "PASS" if not bad else sev["exclusions"], bad, [], "exclusions")
        else:
            f = Path(data_dir) / ("curated/esg_screen.csv" if ex["type"] == "esg_activity" else "curated/controversies.csv")
            if not f.exists():
                add(f"EXCLUSION_{ex['type'].upper()}", "UNVERIFIED", None, None, "exclusions",
                    detail=f"{f.name} missing: cannot verify (never assumed compliant by default).")
                missing.append(f.name)
            elif ex["type"] == "controversy":
                cid, status, value, limit, detail = _controversy_check(
                    w, uni, f, ex, sev["exclusions"], lt, rules.get("controversy_indirect"))
                add(cid, status, value, limit, "exclusions", detail=detail)
            elif ex["type"] == "esg_activity":
                cid, status, value, limit, detail = _esg_activity_check(
                    w, uni, f, ex, sev["exclusions"], lt, rules.get("esg_activity", {}))
                add(cid, status, value, limit, "exclusions", detail=detail)
            else:
                add(f"EXCLUSION_{ex['type'].upper()}", "UNVERIFIED", None, None, "exclusions",
                    detail=f"Unknown exclusion type: {ex['type']} (never assumed compliant by default).")

    # 7. ESG preference (signal, never blocking): positions left out by a reference ESG index
    cfg = rules.get("esg_index_screen")
    kw = (cfg or {}).get("applies_if_preference", "")
    if cfg and kw and any(kw.lower() in str(p).lower() for p in profile.get("preferences", [])):
        f = Path(data_dir) / "curated/esg_index_screen.csv"
        if not f.exists():
            add("ESG_INDEX_PREFERENCE", "UNVERIFIED", None, None, "esg_index_screen",
                detail="esg_index_screen.csv missing (scripts/esg_index_screen.py).")
            missing.append(f.name)
        else:
            cid, status, value, limit, detail = _esg_index_check(w, uni, f, cfg, lt)
            add(cid, status, value, limit, "esg_index_screen", detail=detail)

    if profile.get("loss_capacity") is None:
        missing.append("loss_capacity")

    verdict = max((c["status"] for c in checks), key=RANK.get)
    return {"verdict": verdict, "checks": checks, "missing_info": missing,
            "profile": profile["id"], "rules_version": rules["version"]}
