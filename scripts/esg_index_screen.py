"""
Quelles sociétés les indices ESG laissent-ils de côté ? → data/curated/esg_index_screen.csv

    python scripts/esg_index_screen.py            (après scripts/etf_holdings.py)

Comparaisons (fonds « parent » large vs fonds ESG du même marché) :
  US      SPY (S&P 500)  vs  ESGV  (FTSE US All Cap Choice : exclusions par activité, controverses, diversité)
  Canada  XIU (S&P/TSX 60) vs XCSR (MSCI Canada IMI Extended ESG Leaders : exclusions + meilleures notes ESG)
          XESG (MSCI Canada IMI Extended ESG Focus) en information : indice OPTIMISÉ, il garde presque tout et
          ne fait que sur/sous-pondérer ; une absence de XESG est rare et plus parlante.

CE QUE CE FICHIER DIT, ET NE DIT PAS
  - « absent_indice_esg » = la société est dans le fonds parent mais PAS dans le fonds ESG. La composition
    ne dit PAS POURQUOI (activité, controverse, note ESG, critère de diversité, univers ou liquidité).
    Formulation à utiliser : « exclue de l'indice FTSE US All Cap Choice (ESGV) », JAMAIS « ne respecte pas l'ESG ».
  - Comparaison par TICKER, pas par CUSIP : SPY détient Alphabet en classe A (GOOGL) et ESGV en classe C (GOOG),
    deux CUSIP différents ; les tickers sont normalisés (GOOG→GOOGL). En repli, les 6 premiers caractères du
    CUSIP (code de l'émetteur, commun à toutes les catégories d'actions) évitent d'autres faux positifs (FOX/FOXA…).
  - Dates : SPY/XIU/XCSR/XESG au jour du téléchargement, ESGV au dernier N-PORT (≈ 2-3 mois avant). Une société
    ajoutée au S&P 500 après la date d'ESGV est marquée « indetermine_ajout_recent ».
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etf_holdings import fetch_ishares_ca, sp500_table  # noqa: E402

CLASS_ALIASES = {"GOOG": "GOOGL", "FOX": "FOXA", "NWS": "NWSA", "BRK-A": "BRK-B"}
REASON = "Non déterminée par la composition seule : activité, controverse, note ESG, diversité ou règle d'univers."
INDEX_LABEL = {
    "ESGV": "FTSE US All Cap Choice (Vanguard ESGV)",
    "XCSR.TO": "MSCI Canada IMI Extended ESG Leaders (iShares XCSR)",
    "XESG.TO": "MSCI Canada IMI Extended ESG Focus (iShares XESG)",
}


def compare(parent: pd.DataFrame, esg: pd.DataFrame, parent_name: str, esg_name: str,
            added: dict | None = None) -> pd.DataFrame:
    p, e = parent.copy(), esg.copy()
    for d in (p, e):
        d["tk"] = d["ticker"].replace(CLASS_ALIASES)
        d["issuer"] = d["cusip"].fillna("").str[:6]
    e_w = e[e["tk"] != ""].groupby("tk")["weight"].sum()
    e_issuers = set(e.loc[e["issuer"].str.len() == 6, "issuer"])
    p = p.groupby("tk", as_index=False).agg(name=("name", "first"), issuer=("issuer", "first"),
                                            weight=("weight", "sum"), date=("holdings_date", "first"))
    esg_date = str(e["holdings_date"].iloc[0])
    rows = []
    for r in p.itertuples():
        in_esg = r.tk in e_w.index or (len(r.issuer) == 6 and r.issuer in e_issuers)
        if in_esg:
            statut, raison = "present", ""
        elif added and str(added.get(r.tk, "")) > esg_date:
            statut = "indetermine_ajout_recent"
            raison = f"Ajoutée au fonds parent le {added[r.tk]}, après la composition ESG du {esg_date}."
        else:
            statut, raison = "absent_indice_esg", REASON
        rows.append({
            "ticker": r.tk, "name": r.name, "parent_fund": parent_name, "esg_fund": esg_name,
            "esg_index": INDEX_LABEL[esg_name], "in_esg_fund": in_esg, "statut": statut,
            "weight_parent": round(float(r.weight), 6),
            "weight_esg": round(float(e_w.get(r.tk, 0.0)), 6) if in_esg else 0.0,
            "parent_date": r.date, "esg_date": esg_date, "raison": raison,
            "formulation": (f"Exclue de l'indice {INDEX_LABEL[esg_name]} (raison non publiée dans la composition)"
                            if statut == "absent_indice_esg" else ""),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdings", default="data/curated/etf_holdings.csv")
    ap.add_argument("--universe", default="data/curated/assets_universe.csv")
    ap.add_argument("--out", default="data/curated/esg_index_screen.csv")
    a = ap.parse_args()
    h = pd.read_csv(a.holdings, dtype={"ticker": str, "cusip": str}, keep_default_na=False)
    h["weight"] = h["weight"].astype(float)
    uni = pd.read_csv(a.universe)
    parts = []

    # US : SPY vs ESGV
    try:
        sp = sp500_table()
        added = dict(zip(sp["Symbol"].replace(CLASS_ALIASES), sp["Date added"].astype(str)))
    except Exception as ex:
        print(f"(dates d'ajout au S&P 500 indisponibles : {ex})")
        added = None
    parts.append(compare(h[h.fund == "SPY"], h[h.fund == "ESGV"], "SPY", "ESGV", added))

    # Canada : XIU vs XCSR (et XESG)
    xiu = h[h.fund == "XIU.TO"]
    for esg_fund in ["XCSR.TO", "XESG.TO"]:
        try:
            parts.append(compare(xiu, fetch_ishares_ca(esg_fund), "XIU.TO", esg_fund))
        except Exception as ex:
            print(f"[{esg_fund}] ÉCHEC : {ex}")

    df = pd.concat(parts, ignore_index=True)
    df["in_universe"] = df["ticker"].isin(uni["ticker"])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8")

    for (pf, ef), g in df.groupby(["parent_fund", "esg_fund"]):
        ab = g[g.statut == "absent_indice_esg"]
        print(f"{pf} vs {ef} : {len(ab)}/{len(g)} absents ({ab.weight_parent.sum():.1%} du poids de {pf}), "
              f"{(g.statut == 'indetermine_ajout_recent').sum()} ajouts récents indéterminés")
        mine = g[g.in_universe]
        if len(mine):
            print("   univers AlphaForge : " + ", ".join(
                f"{r.ticker} {'✓' if r.in_esg_fund else '✗'}" for r in mine.itertuples()))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
