"""
Télécharge le dernier rapport annuel déposé sur EDGAR (10-K, ou 40-F pour les émetteurs canadiens
comme CNQ) et en extrait, en fichiers texte prêts pour la Knowledge Base Bedrock (+ .metadata.json) :

    section        10-K                                       40-F (notice annuelle + rapport de gestion)
    item1a         Item 1A. Risk Factors                       D. RISK FACTORS (notice annuelle)
    item3          Item 3. Legal Proceedings                   LEGAL PROCEEDINGS AND REGULATORY ACTIONS (notice)
    contingencies  Note « Commitments and Contingencies »      Note « Commitments and Contingencies »
                   ou « Litigation and Other Contingencies »
    mdna_legal     « Legal and Other Updates » (ENB), si présent  « LEGAL PROCEEDINGS AND OTHER CONTINGENCIES »

À lancer sur VOTRE machine, dans SageMaker ou Lambda (accès réseau à sec.gov requis) :
    pip install requests beautifulsoup4 lxml
    python scripts/fetch_10k.py --tickers XOM ENB CNQ TSLA --as-of 2026-11-12 \
        --email votre.courriel@exemple.com --out data/external/kb_filings

Fichiers : {TICKER}_{10K|40F}_{exercice}_{section}.txt
--as-of : seuls les rapports DÉPOSÉS au plus tard à cette date sont pris (point-in-time).
Vérifiez TOUJOURS le texte extrait à l'œil : les titres varient d'une entreprise à l'autre.
Pas besoin de SEDAR+ : le 40-F déposé à la SEC contient la notice annuelle (AIF) de l'émetteur.
"""
import argparse
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ANNUAL_FORMS = ("10-K", "40-F")


def headers(email):
    return {"User-Agent": f"AlphaForge Datathon {email}"}


# Le ticker pointe parfois vers une nouvelle entité qui n'a pas (encore) déposé de rapport annuel.
# XOM : depuis 2026, company_tickers.json renvoie « ExxonMobil Holdings Corp » (CIK 2115436, aucun 10-K) ;
# les 10-K sont toujours déposés par Exxon Mobil Corp (CIK 34088). Vérifié le 2026-11-14.
CIK_OVERRIDES = {"XOM": "0000034088"}


def ticker_to_cik(email):
    j = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers(email), timeout=30).json()
    return {**{v["ticker"]: str(v["cik_str"]).zfill(10) for v in j.values()}, **CIK_OVERRIDES}


def list_annual(cik, email, forms=ANNUAL_FORMS):
    """Rapports annuels (10-K, 40-F), du plus récent au plus ancien."""
    f = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers(email),
                     timeout=30).json()["filings"]["recent"]
    out = []
    for form, acc, doc, filed, period in zip(f["form"], f["accessionNumber"], f["primaryDocument"],
                                             f["filingDate"], f["reportDate"]):
        if form in forms:
            out.append({"form": form, "accession": acc, "filed": filed, "period": period,
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
    return out


def list_10k(cik, email):  # compatibilité
    return list_annual(cik, email, ("10-K",))


def html_to_text(html):
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display:\s*none")):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ").replace("\xa0", " "))


def section(text, start, end):
    """Plus longue correspondance start…end (insensible à la casse)."""
    m = re.findall(rf"{start}(.*?){end}", text, flags=re.I | re.S)
    return max(m, key=len).strip() if m else ""


def heading_section(text, start, end, cap=40000):
    """De la DERNIÈRE occurrence du titre `start` (regex compilée) jusqu'au premier `end` qui suit.
    La table des matières et les renvois précèdent toujours la vraie section."""
    starts = list(start.finditer(text))
    if not starts:
        return ""
    m = starts[-1]
    e = end.search(text, m.end() + 20)
    return text[m.start():e.start() if e else m.end() + cap].strip()


# Note des éventualités : « Note 13 – Commitments and Contingencies » (TSLA), « 30. COMMITMENTS AND
# CONTINGENCIES » (ENB), « Note 7. Litigation and Other Contingencies » (XOM), « 19. Commitments and
# Contingencies » (CNQ). Fin : le titre de la note suivante (numéro + 1).
NOTE_START = re.compile(r"(?:Note\s*)?\b(\d{1,2})\s*[\.\-—–:]\s*(?:Commitments\s+and|Litigation\s+and\s+Other)\s+"
                        r"Contingencies", re.I)


def contingencies_note(text):
    starts = list(NOTE_START.finditer(text))
    if not starts:
        return ""
    m = starts[-1]
    n = int(m.group(1)) + 1
    end = re.compile(rf"(?:NOTE|Note)\s*{n}\s*[\.\-—–:]|\b{n}\s*[\.\-—–:]\s+[A-Z]")
    e = end.search(text, m.end() + 50)
    return text[m.start():e.start() if e else m.end() + 40000].strip()


UPPER_HEADING = re.compile(r"\b[A-Z][A-Z,&\-'’]+(?: [A-Z][A-Z,&\-'’]+){2,}\b")   # ≥ 3 mots en majuscules

EXTRACTORS = {
    "10-K": {
        "item1a": lambda t: section(t, r"Item\s*1A\.?\s*Risk\s*Factors", r"Item\s*1B\.?"),
        "item3": lambda t: section(t, r"Item\s*3\.?\s*Legal\s*Proceedings", r"Item\s*4\.?"),
        "contingencies": lambda t: contingencies_note(t)
        or section(t, r"Commitments\s+and\s+Contingencies", r"(?:NOTE|Note)\s*\d+\s*[\.\-—–:]"),
        # ENB détaille ses litiges dans le rapport de gestion, pas dans la note
        "mdna_legal": lambda t: heading_section(
            t, re.compile(r"LEGAL AND OTHER UPDATES|LEGAL PROCEEDINGS AND OTHER UPDATES"),
            re.compile(r"CRITICAL ACCOUNTING|LIQUIDITY AND CAPITAL|QUANTITATIVE AND QUALITATIVE|ITEM\s*7A"), 30000),
    },
    "40-F": {
        # notice annuelle : « D. RISK FACTORS », puis « E. … » ou « Form 51-101F1 Statement of Reserves Data » (CNQ)
        "item1a": lambda t: heading_section(t, re.compile(r"\b[A-H]\.\s+RISK FACTORS"),
                                            re.compile(r"\b[E-L]\.\s+[A-Z][A-Z ]{5,}|Form 51-101F1"), 80000),
        "item3": lambda t: heading_section(t, re.compile(r"LEGAL PROCEEDINGS AND REGULATORY ACTIONS"), UPPER_HEADING),
        "contingencies": contingencies_note,
        # rapport de gestion : section courte, souvent suivie d'un titre en casse mixte (« Reserves »)
        "mdna_legal": lambda t: heading_section(
            t, re.compile(r"LEGAL PROCEEDINGS AND OTHER CONTINGENCIES"),
            re.compile(r"Annual Report|\bReserves\b|" + UPPER_HEADING.pattern), 15000),
    },
}
OPTIONAL = {"mdna_legal"}   # pas d'avertissement si absente


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--out", default="data/external/kb_filings")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ciks = ticker_to_cik(a.email)
    for t in a.tickers:
        cik = ciks.get(t)
        if not cik:
            print(f"[{t}] introuvable dans company_tickers.json (pas inscrit à la SEC : notice annuelle sur SEDAR+)")
            continue
        filings = [f for f in list_annual(cik, a.email) if f["filed"] <= a.as_of]
        if not filings:
            print(f"[{t}] aucun 10-K ni 40-F déposé avant {a.as_of} (CIK {cik}). Nouvelle société mère ? "
                  f"Ajouter le bon CIK dans CIK_OVERRIDES.")
            continue
        f = filings[0]
        form = f["form"]
        time.sleep(0.2)  # règle SEC : max 10 requêtes/s
        text = html_to_text(requests.get(f["url"], headers=headers(a.email), timeout=120).text)
        for name, extract in EXTRACTORS[form].items():
            body = extract(text)
            if not body and name in OPTIONAL:
                continue
            stem = f"{t}_{form.replace('-', '')}_{f['period'][:4]}_{name}"
            (out / f"{stem}.txt").write_text(body, encoding="utf-8")
            meta = {"metadataAttributes": {
                "ticker": t, "form": form, "doc_type": f"{form.replace('-', '').lower()}_{name}",
                "fiscal_period_end": f["period"], "published_date": int(f["filed"].replace("-", "")),
                "accession": f["accession"], "source_url": f["url"], "language": "en"}}
            (out / f"{stem}.txt.metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            warn = "  ⚠ VIDE : vérifier les titres" if not body else ""
            print(f"[{t}] {form} {name}: {len(body):>7} caractères  (déposé {f['filed']}){warn}")


if __name__ == "__main__":
    main()
