"""
Extrait la composition de FNB américains (SPY, QQQ, ESGV…) depuis les jeux de données
N-PORT de la SEC (domaine public), et produit :
    nport_holdings.csv        fund, cusip, issuer_name, weight, report_date, accession
    nport_esg_screen.csv      titres de SPY absents d'ESGV (signal « a_verifier »)

1. Téléchargez manuellement un zip trimestriel sur la page SEC « Form N-PORT Data Sets »
   (https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets).
2. Décompressez-le, puis :
       python scripts/nport_holdings.py --dir chemin/vers/le/zip_decompresse --out data/external

Les noms de fichiers/colonnes ci-dessous suivent la documentation SEC (README du zip).
S'ils diffèrent, le script affiche les colonnes disponibles : ajustez COLS.
Pour un « as_of » de simulation, utilisez le trimestre dont la date de DÉPÔT ≤ as_of.
XIU.TO (FNB canadien) n'est PAS dans N-PORT : utiliser le fichier de positions d'iShares Canada.
"""
import argparse
from pathlib import Path

import pandas as pd

FUNDS = {  # libellé -> motif recherché dans le nom de la série
    "SPY": "SPDR S&P 500 ETF",
    "QQQ": "Invesco QQQ",
    "ESGV": "ESG U.S. Stock ETF",
}
COLS = {"series_name": "SERIES_NAME", "accession": "ACCESSION_NUMBER", "issuer": "ISSUER_NAME",
        "cusip": "ISSUER_CUSIP", "pct": "PERCENTAGE", "report_date": "REPORT_DATE"}


def read(dirpath: Path, name: str) -> pd.DataFrame:
    p = next(iter(dirpath.glob(f"{name}*.tsv")), None)
    if p is None:
        raise FileNotFoundError(f"{name}*.tsv introuvable dans {dirpath}")
    return pd.read_csv(p, sep="\t", dtype=str, low_memory=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="data/external")
    a = ap.parse_args()
    d, out = Path(a.dir), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    info = read(d, "FUND_REPORTED_INFO")
    hold = read(d, "FUND_REPORTED_HOLDING")
    sub = read(d, "SUBMISSION")
    for df, need in [(info, ["series_name", "accession"]), (hold, ["accession", "issuer", "cusip", "pct"])]:
        miss = [COLS[c] for c in need if COLS[c] not in df.columns]
        if miss:
            raise KeyError(f"Colonnes manquantes {miss}. Disponibles : {list(df.columns)}")

    rows = []
    for fund, pattern in FUNDS.items():
        f = info[info[COLS["series_name"]].str.contains(pattern, case=False, na=False)]
        if f.empty:
            print(f"[{fund}] série introuvable dans ce trimestre")
            continue
        acc = f[COLS["accession"]].iloc[-1]
        h = hold[hold[COLS["accession"]] == acc]
        rd = sub.loc[sub[COLS["accession"]] == acc, COLS["report_date"]].iloc[0] \
            if COLS["report_date"] in sub.columns else None
        rows.append(pd.DataFrame({
            "fund": fund, "cusip": h[COLS["cusip"]], "issuer_name": h[COLS["issuer"]],
            "weight": pd.to_numeric(h[COLS["pct"]], errors="coerce") / 100,
            "report_date": rd, "accession": acc}))
        print(f"[{fund}] {len(h)} positions, somme des poids = {rows[-1]['weight'].sum():.3f}")

    hol = pd.concat(rows, ignore_index=True)
    hol.to_csv(out / "nport_holdings.csv", index=False)

    if {"SPY", "ESGV"} <= set(hol["fund"]):
        spy, esg = hol[hol.fund == "SPY"], hol[hol.fund == "ESGV"]
        m = spy.merge(esg[["cusip"]].assign(in_esgv=True), on="cusip", how="left")
        m["in_esgv"] = m["in_esgv"].fillna(False)
        m["status"] = m["in_esgv"].map({True: "present_dans_esgv", False: "a_verifier"})
        m["note"] = ("Absent d'ESGV : retiré par le filtre FTSE Choice (activité, controverse ou critère "
                     "de diversité) OU écart d'univers/rapprochement CUSIP. Raison non déterminée ici.")
        m[["cusip", "issuer_name", "weight", "in_esgv", "status", "report_date", "accession", "note"]] \
            .to_csv(out / "nport_esg_screen.csv", index=False)
        ex = m[~m.in_esgv]
        print(f"SPY vs ESGV : {len(ex)} titres absents d'ESGV ({ex.weight.sum():.1%} du poids de SPY)")


if __name__ == "__main__":
    main()
