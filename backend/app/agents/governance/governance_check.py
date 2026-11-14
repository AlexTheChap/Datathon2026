"""
Vérification de référence d'une allocation contre un profil et backend/app/core/rules.yaml.
Code déterministe, sans LLM. Renvoie un verdict PASS / WARN / BLOCK avec chaque contrôle.

    from risk_tools import RiskTools  # ajouter backend/app/tools au sys.path
    from governance_check import check
    rt = RiskTools("data/curated")
    check({"XIU.TO": .3, "NVDA": .2, ...}, persona_dict, "backend/app/core/rules.yaml", rt)

Les exclusions ESG/controverses utilisent data/curated/esg_screen.csv et
controversies.csv s'ils existent ; sinon le contrôle renvoie NON_VERIFIE (jamais PASS).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

RANK = {"PASS": 0, "NON_VERIFIE": 1, "WARN": 2, "BLOCK": 3}


SEVERITY_RANK = {"mineure": 1, "moderee": 2, "majeure": 3}


def _controversy_check(w: dict, uni: pd.DataFrame, path: Path, ex: dict, block_status: str,
                       lt: pd.DataFrame | None = None, indirect: dict | None = None):
    """Seules les lignes statut=verifie (relues par un humain) comptent. Une action détenue
    directement sans aucune ligne vérifiée reste NON_VERIFIE : absence de preuve ≠ preuve d'absence.
    Avec la décomposition des FNB (`lt`), une société controversée détenue VIA des FNB au-delà de
    indirect["max_weight"] donne indirect["severity"] (WARN par défaut) : c'est un choix de fonds, pas de titre.
    Renvoie (id, statut, valeur, limite, detail) pour add()."""
    df = pd.read_csv(path, dtype=str).fillna("")
    min_rank = SEVERITY_RANK[ex.get("min_severity", "majeure")]
    flagged = df[(df["statut"] == "verifie") & (df["gravite"].map(SEVERITY_RANK).fillna(0) >= min_rank)]
    stocks = [k for k in w if uni.loc[k, "asset_class"] == "equity"]
    verified = df[(df["statut"] == "verifie") & df["ticker"].isin(stocks)]
    hits = flagged[flagged["ticker"].isin(stocks)]
    blocked = sorted(hits["ticker"].unique())
    unverified = sorted(set(stocks) - set(verified["ticker"]))
    parts = []
    if blocked:
        status = block_status
        parts.append("En direct : " + "; ".join(f"{r.ticker} ({r.gravite}) : {r.description_fr} [{r.source_doc}]"
                                                for r in hits.itertuples()))
    elif unverified:
        status = "NON_VERIFIE"
        parts.append(f"Aucune ligne vérifiée pour : {', '.join(unverified)}.")
    else:
        status = "PASS"
    if lt is not None:
        ind = indirect or {}
        lim, ind_sev = float(ind.get("max_weight", 0.01)), ind.get("severity", "WARN")
        via = lt.loc[lt.index.isin(flagged["ticker"]) & (lt["via_etf"] > 0), "via_etf"]
        over = via[via > lim]
        if len(via):
            parts.append("Via FNB : " + ", ".join(f"{t} {v:.1%}" for t, v in via.sort_values(ascending=False).items())
                         + f" (seuil {lim:.0%}).")
        if len(over) and RANK[ind_sev] > RANK[status]:
            status = ind_sev
        parts.append("Seules les sociétés ayant des lignes vérifiées sont repérées dans les FNB.")
    else:
        parts.append("Titres détenus directement seulement (etf_holdings.csv absent : FNB non décomposés).")
    return "EXCLUSION_CONTROVERSY", status, blocked, ex.get("min_severity", "majeure"), " ".join(parts)


def _esg_activity_check(w: dict, uni: pd.DataFrame, path: Path, ex: dict, block_status: str,
                        lt: pd.DataFrame | None, cfg: dict):
    """Exclusion par activité (ex. « combustibles_fossiles ») à partir de curated/esg_screen.csv.
    Seules les lignes relues (reviseur non vide) comptent ; part_revenus en fraction (0,25) ou en % (25).
    - action détenue en direct avec part_revenus ≥ seuil -> BLOCK ;
    - action détenue en direct sans ligne relue pour cette activité -> NON_VERIFIE ;
    - société exclue détenue via des FNB décomposés au-delà de indirect.max_weight -> WARN.
    Une ligne relue avec part_revenus = 0 prouve l'absence de l'activité."""
    act = ex["value"]
    thr = float(ex.get("min_revenue_share", cfg.get("min_revenue_share", 0.10)))
    df = pd.read_csv(path, dtype=str).fillna("")
    df = df[(df["activite"] == act) & (df["reviseur"].str.strip() != "")].copy()
    share = pd.to_numeric(df["part_revenus"].str.replace(",", ".").str.rstrip("%"), errors="coerce")
    df["share"] = share.where(share <= 1, share / 100)
    df = df.sort_values("annee_fiscale").groupby("ticker").tail(1)        # exercice le plus récent
    excluded = df.loc[df["share"] >= thr].set_index("ticker")["share"]

    stocks = [k for k in w if uni.loc[k, "asset_class"] == "equity"]
    blocked = sorted(set(stocks) & set(excluded.index))
    unverified = sorted(set(stocks) - set(df["ticker"]))
    parts = []
    if blocked:
        status = block_status
        parts.append("En direct : " + ", ".join(f"{t} ({excluded[t]:.0%} des revenus)" for t in blocked) + ".")
    elif unverified:
        status = "NON_VERIFIE"
        parts.append(f"Aucune ligne relue « {act} » pour : {', '.join(unverified)}.")
    else:
        status = "PASS"
    if lt is not None:
        ind = cfg.get("indirect", {})
        lim, ind_sev = float(ind.get("max_weight", 0.01)), ind.get("severity", "WARN")
        via = lt.loc[lt.index.isin(excluded.index) & (lt["via_etf"] > 0), "via_etf"]
        if len(via):
            parts.append("Via FNB : " + ", ".join(f"{t} {v:.1%}" for t, v in via.sort_values(ascending=False).items())
                         + f" (seuil {lim:.0%}).")
        if (via > lim).any() and RANK[ind_sev] > RANK[status]:
            status = ind_sev
        parts.append("Dans les FNB, seules les sociétés ayant une ligne relue sont repérées.")
    else:
        parts.append("Titres détenus directement seulement (etf_holdings.csv absent).")
    return f"EXCLUSION_ESG_{act.upper()}", status, blocked, thr, " ".join(parts)


def _esg_index_check(w: dict, uni: pd.DataFrame, path: Path, cfg: dict, lt: pd.DataFrame | None):
    """Actions détenues en direct absentes de l'indice ESG de référence (selon la devise) -> cfg["severity"].
    Formulation : « exclue de l'indice X », jamais « ne respecte pas l'ESG » (la raison n'est pas publiée).
    La part des FNB larges (SPY, XIU) absente de l'indice ESG est décrite, sans changer le statut."""
    s = pd.read_csv(path, keep_default_na=False)
    s["in_esg_fund"] = s["in_esg_fund"].astype(str).str.lower() == "true"
    by_ccy = cfg.get("reference_by_currency", {})
    flagged, unknown, parts = [], [], []
    for k in w:
        if uni.loc[k, "asset_class"] != "equity":
            continue
        ref = by_ccy.get(uni.loc[k, "currency"])
        row = s[(s["ticker"] == k) & (s["esg_fund"] == ref)]
        if row.empty or row["statut"].iloc[0] == "indetermine_ajout_recent":
            unknown.append(k)
        elif not row["in_esg_fund"].iloc[0]:
            flagged.append(k)
            parts.append(f"{k} : {row['formulation'].iloc[0]}.")
    status = cfg.get("severity", "WARN") if flagged else ("NON_VERIFIE" if unknown else "PASS")
    if unknown:
        parts.append(f"Sans information : {', '.join(unknown)}.")
    if lt is not None:
        for parent, ref in cfg.get("reference_by_parent", {}).items():
            if parent not in w:
                continue
            g = s[(s["parent_fund"] == parent) & (s["esg_fund"] == ref)]
            share = float(g.loc[g["statut"] == "absent_indice_esg", "weight_parent"].sum())
            parts.append(f"Via {parent} : {share:.0%} du fonds ({share * w[parent]:.1%} du portefeuille) "
                         f"absent de {ref} ; {ref} serait l'équivalent ESG.")
    return "PREFERENCE_ESG_INDICE", status, flagged, None, " ".join(parts) or "Aucune action détenue en direct."


def _band(profile: dict, rules: dict) -> dict:
    return rules["risk_bands"][profile["risk_band"]]


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

    # 1. Perte maximale en scénario de crise
    ev = rt.stress(w, rules["stress_window"])
    mdd = ev["output"]["portfolio"]["max_drawdown"]
    lim = band["max_drawdown_stress"]
    add("PERTE_MAX_STRESS", "PASS" if mdd >= -lim else sev["max_drawdown_stress"],
        mdd, -lim, "max_drawdown_stress", ev["evidence_id"])

    # 2-3. Position unique et secteur. Avec data/curated/etf_holdings.csv : FNB d'actions décomposés
    # (NVDA détenu en direct + via SPY/QQQ compte comme une seule exposition).
    exempt = set(rules["broad_funds_exempt_from_single_position"])
    single = {k: v for k, v in w.items() if k not in exempt}
    lt = rt.lookthrough(w) if rt.holdings else None
    if lt is not None:
        eq = lt[lt["asset_class"] == "equity"]
        if len(eq):
            top, r = eq.index[0], eq.iloc[0]
            add("POSITION_UNIQUE", "PASS" if r.weight <= band["max_single_position"] else sev["max_single_position"],
                round(float(r.weight), 4), band["max_single_position"], "max_single_position",
                detail=f"{top} : {r.direct:.1%} en direct + {r.via_etf:.1%} via FNB (FNB d'actions décomposés)")
            sec = eq.groupby("sector")["weight"].sum()
            unclassified = float(sec.pop("non classé")) if "non classé" in sec else 0.0
            if len(sec):
                s_top = sec.idxmax()
                add("SECTEUR", "PASS" if sec.max() <= band["max_sector"] else sev["max_sector"],
                    round(float(sec.max()), 4), band["max_sector"], "max_sector",
                    detail=f"{s_top} (FNB d'actions décomposés ; non classé : {unclassified:.1%} du portefeuille)")
    elif single:
        top = max(single, key=single.get)
        add("POSITION_UNIQUE", "PASS" if single[top] <= band["max_single_position"] else sev["max_single_position"],
            round(single[top], 4), band["max_single_position"], "max_single_position", detail=top)

    # 3. Secteur (sans décomposition des FNB : FNB larges exclus du calcul)
    sec = pd.Series({k: v for k, v in single.items()}).groupby(uni["sector"]).sum() \
        if (single and lt is None) else pd.Series(dtype=float)
    if len(sec):
        s_top = sec.idxmax()
        add("SECTEUR", "PASS" if sec.max() <= band["max_sector"] else sev["max_sector"],
            round(float(sec.max()), 4), band["max_sector"], "max_sector",
            detail=f"{s_top} (titres directs seulement ; décomposition des FNB requise pour le chiffre complet)")

    # 4. Crypto
    crypto = sum(v for k, v in w.items() if uni.loc[k, "asset_class"] == "crypto")
    add("CRYPTO", "PASS" if crypto <= band["max_crypto"] else sev["max_crypto"],
        round(crypto, 4), band["max_crypto"], "max_crypto")

    # 5. Règles d'horizon
    h_min, h_max = profile["horizon_years"]
    h_ref = h_min  # prudence : on retient la borne basse de l'horizon (besoin le plus proche)
    met = rt.metrics(list(w))["output"]
    for hr in rules["horizon_rules"]:
        if h_ref is not None and h_ref < hr["when"]["horizon_years_max_lt"]:
            if "max_weight_high_vol_assets" in hr:
                hv = sum(v for k, v in w.items() if met[k]["ann_vol"] > hr["high_vol_threshold"])
                add(hr["id"], "PASS" if hv <= hr["max_weight_high_vol_assets"] else hr["severity"],
                    round(hv, 4), hr["max_weight_high_vol_assets"], "max_drawdown_stress",
                    detail=hr["justification"])
            if "min_defensive_weight" in hr:
                d = sum(v for k, v in w.items() if uni.loc[k, "asset_class"] == "etf_bond")
                add(hr["id"], "PASS" if d >= hr["min_defensive_weight"] else hr["severity"],
                    round(d, 4), hr["min_defensive_weight"], "max_drawdown_stress", detail=hr["justification"])

    # 6. Exclusions de l'usager
    for ex in profile.get("exclusions", []):
        if ex["type"] == "asset_class":
            bad = [k for k in w if uni.loc[k, "asset_class"] == ex["value"]]
            add(f"EXCLUSION_{ex['value'].upper()}", "PASS" if not bad else sev["exclusions"], bad, [], "exclusions")
        else:
            f = Path(data_dir) / ("curated/esg_screen.csv" if ex["type"] == "esg_activity" else "curated/controversies.csv")
            if not f.exists():
                add(f"EXCLUSION_{ex['type'].upper()}", "NON_VERIFIE", None, None, "exclusions",
                    detail=f"{f.name} absent : impossible de vérifier (jamais considéré conforme par défaut).")
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
                add(f"EXCLUSION_{ex['type'].upper()}", "NON_VERIFIE", None, None, "exclusions",
                    detail=f"Type d'exclusion inconnu : {ex['type']} (jamais considéré conforme par défaut).")

    # 7. Préférence ESG (signal, jamais bloquant) : positions écartées par un indice ESG de référence
    cfg = rules.get("esg_index_screen")
    kw = (cfg or {}).get("applies_if_preference", "")
    if cfg and kw and any(kw.lower() in str(p).lower() for p in profile.get("preferences", [])):
        f = Path(data_dir) / "curated/esg_index_screen.csv"
        if not f.exists():
            add("PREFERENCE_ESG_INDICE", "NON_VERIFIE", None, None, "esg_index_screen",
                detail="esg_index_screen.csv absent (scripts/esg_index_screen.py).")
            missing.append(f.name)
        else:
            cid, status, value, limit, detail = _esg_index_check(w, uni, f, cfg, lt)
            add(cid, status, value, limit, "esg_index_screen", detail=detail)

    if profile.get("capacite_perte") is None:
        missing.append("capacite_perte")

    verdict = max((c["status"] for c in checks), key=RANK.get)
    return {"verdict": verdict, "checks": checks, "missing_info": missing,
            "profile": profile["id"], "rules_version": rules["version"]}
