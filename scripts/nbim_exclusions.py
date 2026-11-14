"""
Liste d'exclusion du fonds souverain norvégien (NBIM, recommandations du Conseil d'éthique)
→ data/external/nbim_exclusions.csv (liste complète, avec ticker quand on le retrouve)
→ data/curated/controversies.csv (critères de CONDUITE seulement, statut a_verifier)

    python scripts/nbim_exclusions.py        (après scripts/etf_holdings.py pour repérer aussi les titres des FNB)

Source : https://www.nbim.no/en/responsible-investment/ethical-exclusions/exclusion-of-companies/
  - « Product-based » (charbon, armes, tabac…) = ACTIVITÉ, pas une controverse : gardé dans nbim_exclusions.csv
    seulement (la part des revenus n'est pas publiée, donc rien n'est écrit dans esg_screen.csv).
  - « Conduct-based » (dommages environnementaux, GES, droits humains, corruption…) = controverse :
    Exclusion → gravite majeure ; Observation (sous surveillance, pas de conclusion) → moderee.
Formulation : « exclue (ou mise sous observation) par le fonds souverain norvégien pour <critère> ».
Rapprochement par NOM normalisé (la liste ne donne ni ticker ni ISIN) : vérifier chaque correspondance.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_controversies import COLUMNS, merge  # noqa: E402

URL = "https://www.nbim.no/en/responsible-investment/ethical-exclusions/exclusion-of-companies/"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SOURCE_DOC = "NBIM - Exclusion of companies (Conseil d'éthique)"
CATEGORIE = [  # (motif dans le critère, catégorie du gabarit)
    (r"environment|greenhouse|climate", "environnement"),
    (r"corruption|financial crime", "gouvernance"),
    (r"human rights|individuals' rights|war or conflict|ethical norms|labour|child", "social"),
]
SUFFIXES = r"\b(the|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|sa|ag|nv|se|asa|group|holdings?|cl [ab]|class [ab])\b"


def norm(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("+", " and ").replace("/the", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch() -> pd.DataFrame:
    html = requests.get(URL, headers=BROWSER, timeout=60).text
    t = pd.read_html(io.StringIO(html), extract_links="body")[0]
    t.columns = ["company", "former_name", "category", "criterion", "decision", "published"]
    out = pd.DataFrame({c: t[c].map(lambda x: x[0] if isinstance(x, tuple) else x) for c in t.columns})
    out["link"] = t["company"].map(lambda x: x[1] if isinstance(x, tuple) else None)
    out["link"] = out["link"].map(lambda l: (l if str(l).startswith("http") else f"https://www.nbim.no{l}")
                                  if l else URL)
    out["published"] = pd.to_datetime(out["published"], format="%d.%m.%Y", errors="coerce").dt.date.astype(str)
    for c in ["company", "former_name", "category", "criterion", "decision"]:
        out[c] = out[c].fillna("").astype(str).str.replace("\xa0", " ").str.strip()
    out["category"] = out["category"].str.replace(r"^(Product|Conduct)[\s-]+based$", r"\1-based", regex=True)
    return out


def name_map(universe: pd.DataFrame, holdings: pd.DataFrame | None) -> dict:
    """nom normalisé -> ticker (univers d'abord, puis titres détenus par les FNB)."""
    m = {}
    if holdings is not None:
        for r in holdings[holdings["ticker"] != ""].itertuples():
            m.setdefault(norm(r.name), r.ticker)
    for r in universe.itertuples():
        m[norm(r.name)] = r.ticker
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="data/curated/assets_universe.csv")
    ap.add_argument("--holdings", default="data/curated/etf_holdings.csv")
    ap.add_argument("--out-list", default="data/external/nbim_exclusions.csv")
    ap.add_argument("--out", default="data/curated/controversies.csv")
    a = ap.parse_args()
    uni = pd.read_csv(a.universe)
    hp = Path(a.holdings)
    hold = pd.read_csv(hp, dtype=str, keep_default_na=False) if hp.exists() else None
    names = name_map(uni, hold)

    df = fetch()
    df["ticker"] = [names.get(norm(c)) or names.get(norm(f)) or "" for c, f in zip(df.company, df.former_name)]
    df["in_universe"] = df["ticker"].isin(uni["ticker"])
    df["retrieved_on"] = pd.Timestamp.today().date().isoformat()
    Path(a.out_list).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out_list, index=False, encoding="utf-8")
    print(f"NBIM : {len(df)} sociétés ({(df.category == 'Conduct-based').sum()} pour conduite), "
          f"{(df.ticker != '').sum()} rapprochées d'un ticker → {a.out_list}")

    rows = []
    for r in df[(df.ticker != "") & (df.category == "Conduct-based")].itertuples():
        cat = next((c for pat, c in CATEGORIE if re.search(pat, r.criterion, re.I)), "autre")
        verbe = "exclue" if r.decision == "Exclusion" else "mise sous observation"
        rows.append({
            "ticker": r.ticker, "date_publication": r.published, "categorie": cat,
            "description_fr": (f"{r.company} : {verbe} par le fonds souverain norvégien (NBIM, recommandation du "
                               f"Conseil d'éthique) le {r.published}, critère « {r.criterion} »."),
            "citation_en": r.criterion,
            "gravite": "majeure" if r.decision == "Exclusion" else "moderee",
            "statut": "a_verifier", "devise_montant": "", "montant": None,
            "source_doc": SOURCE_DOC, "section": f"{r.category} / {r.decision}", "source_url": r.link,
            "reviseur": "", "date_revision": "",
        })
    for r in df[df.ticker != ""].itertuples():
        flag = "univers" if r.in_universe else "via FNB"
        print(f"  [{flag:<7}] {r.ticker:<8} {r.company} | {r.category} | {r.criterion} | {r.decision} {r.published}")
    if rows:
        out = Path(a.out)
        merged = merge(out, pd.DataFrame(rows)[COLUMNS])
        merged.to_csv(out, index=False, encoding="utf-8")
        print(f"→ {out} : {len(rows)} ligne(s) NBIM ajoutée(s)/remplacée(s) en a_verifier ({len(merged)} au total)")


if __name__ == "__main__":
    main()
