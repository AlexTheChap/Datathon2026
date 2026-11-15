"""
Downloads the latest annual report filed on EDGAR (10-K, or 40-F for Canadian issuers such as CNQ)
and extracts the sections below into text files ready for the Bedrock Knowledge Base (+ .metadata.json):

    section        10-K                                       40-F (annual information form + MD&A)
    item1a         Item 1A. Risk Factors                       D. RISK FACTORS (AIF)
    item3          Item 3. Legal Proceedings                   LEGAL PROCEEDINGS AND REGULATORY ACTIONS (AIF)
    contingencies  Note "Commitments and Contingencies"      Note "Commitments and Contingencies"
                   or "Litigation and Other Contingencies"
    mdna_legal     "Legal and Other Updates" (ENB), if present  "LEGAL PROCEEDINGS AND OTHER CONTINGENCIES"

Run on YOUR machine, in SageMaker or in Lambda (network access to sec.gov required):
    pip install requests beautifulsoup4 lxml
    python scripts/fetch_10k.py --tickers XOM ENB CNQ TSLA --as-of 2026-11-12 \
        --email your.email@example.com --out data/external/kb_filings

Files: {TICKER}_{10K|40F}_{fiscal_year}_{section}.txt
--as-of: only reports FILED on or before this date are used (point-in-time).
ALWAYS check the extracted text by eye: headings vary from one company to another.
SEDAR+ is not needed: the 40-F filed with the SEC contains the issuer's annual information form (AIF).
"""
import argparse
import json
import re
import time
from pathlib import Path

import requests
from enrich_8k import html_to_text  # same SEC HTML -> text cleaning as the 8-K excerpts

ANNUAL_FORMS = ("10-K", "40-F")


def headers(email):
    return {"User-Agent": f"Fitfolio Datathon {email}"}


# A ticker sometimes points to a new entity that has not (yet) filed an annual report.
# XOM: since 2026, company_tickers.json returns "ExxonMobil Holdings Corp" (CIK 2115436, no 10-K);
# the 10-Ks are still filed by Exxon Mobil Corp (CIK 34088). Checked on 2026-11-14.
CIK_OVERRIDES = {"XOM": "0000034088"}


def ticker_to_cik(email):
    j = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers(email), timeout=30).json()
    return {**{v["ticker"]: str(v["cik_str"]).zfill(10) for v in j.values()}, **CIK_OVERRIDES}


def list_annual(cik, email, forms=ANNUAL_FORMS):
    """Annual reports (10-K, 40-F), most recent first."""
    f = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers(email),
                     timeout=30).json()["filings"]["recent"]
    out = []
    for form, acc, doc, filed, period in zip(f["form"], f["accessionNumber"], f["primaryDocument"],
                                             f["filingDate"], f["reportDate"]):
        if form in forms:
            out.append({"form": form, "accession": acc, "filed": filed, "period": period,
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"})
    return out


def section(text, start, end):
    """Longest match of start...end (case-insensitive)."""
    m = re.findall(rf"{start}(.*?){end}", text, flags=re.I | re.S)
    return max(m, key=len).strip() if m else ""


def heading_section(text, start, end, cap=40000):
    """From the LAST occurrence of the `start` heading (compiled regex) to the first `end` that follows.
    The table of contents and cross-references always precede the real section."""
    starts = list(start.finditer(text))
    if not starts:
        return ""
    m = starts[-1]
    e = end.search(text, m.end() + 20)
    return text[m.start():e.start() if e else m.end() + cap].strip()


# Contingencies note: "Note 13 – Commitments and Contingencies" (TSLA), "30. COMMITMENTS AND
# CONTINGENCIES" (ENB), "Note 7. Litigation and Other Contingencies" (XOM), "19. Commitments and
# Contingencies" (CNQ). Ends at the heading of the next note (number + 1).
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


UPPER_HEADING = re.compile(r"\b[A-Z][A-Z,&\-'’]+(?: [A-Z][A-Z,&\-'’]+){2,}\b")   # 3+ uppercase words

EXTRACTORS = {
    "10-K": {
        "item1a": lambda t: section(t, r"Item\s*1A\.?\s*Risk\s*Factors", r"Item\s*1B\.?"),
        "item3": lambda t: section(t, r"Item\s*3\.?\s*Legal\s*Proceedings", r"Item\s*4\.?"),
        "contingencies": lambda t: contingencies_note(t)
        or section(t, r"Commitments\s+and\s+Contingencies", r"(?:NOTE|Note)\s*\d+\s*[\.\-—–:]"),
        # ENB details its legal matters in the MD&A, not in the note
        "mdna_legal": lambda t: heading_section(
            t, re.compile(r"LEGAL AND OTHER UPDATES|LEGAL PROCEEDINGS AND OTHER UPDATES"),
            re.compile(r"CRITICAL ACCOUNTING|LIQUIDITY AND CAPITAL|QUANTITATIVE AND QUALITATIVE|ITEM\s*7A"), 30000),
    },
    "40-F": {
        # AIF: "D. RISK FACTORS", then "E. ..." or "Form 51-101F1 Statement of Reserves Data" (CNQ)
        "item1a": lambda t: heading_section(t, re.compile(r"\b[A-H]\.\s+RISK FACTORS"),
                                            re.compile(r"\b[E-L]\.\s+[A-Z][A-Z ]{5,}|Form 51-101F1"), 80000),
        "item3": lambda t: heading_section(t, re.compile(r"LEGAL PROCEEDINGS AND REGULATORY ACTIONS"), UPPER_HEADING),
        "contingencies": contingencies_note,
        # MD&A: short section, often followed by a mixed-case heading ("Reserves")
        "mdna_legal": lambda t: heading_section(
            t, re.compile(r"LEGAL PROCEEDINGS AND OTHER CONTINGENCIES"),
            re.compile(r"Annual Report|\bReserves\b|" + UPPER_HEADING.pattern), 15000),
    },
}
OPTIONAL = {"mdna_legal"}   # no warning if missing


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
            print(f"[{t}] not found in company_tickers.json (not registered with the SEC: AIF is on SEDAR+)")
            continue
        filings = [f for f in list_annual(cik, a.email) if f["filed"] <= a.as_of]
        if not filings:
            print(f"[{t}] no 10-K or 40-F filed before {a.as_of} (CIK {cik}). New parent company? "
                  f"Add the right CIK to CIK_OVERRIDES.")
            continue
        f = filings[0]
        form = f["form"]
        time.sleep(0.2)  # SEC rule: max 10 requests/s
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
            warn = "  ⚠ EMPTY: check the headings" if not body else ""
            print(f"[{t}] {form} {name}: {len(body):>7} characters  (filed {f['filed']}){warn}")


if __name__ == "__main__":
    main()
