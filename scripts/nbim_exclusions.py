"""
Exclusion list of the Norwegian sovereign wealth fund (NBIM, Council on Ethics recommendations)
→ data/external/nbim_exclusions.csv (full list, with ticker when found)
→ data/curated/controversies.csv (CONDUCT criteria only, status to_verify)

    python scripts/nbim_exclusions.py        (run after scripts/etf_holdings.py to also match ETF holdings)

Source : https://www.nbim.no/en/responsible-investment/ethical-exclusions/exclusion-of-companies/
  - "Product-based" (coal, weapons, tobacco...) = ACTIVITY, not a controversy: kept in nbim_exclusions.csv
    only (the revenue share is not published, so nothing is written to esg_screen.csv).
  - "Conduct-based" (environmental damage, GHG, human rights, corruption...) = controversy:
    Exclusion → severity major; Observation (under watch, no conclusion) → moderate.
Wording: "excluded (or placed under observation) by the Norwegian sovereign wealth fund for <criterion>".
Matching is by normalized NAME (the list gives neither ticker nor ISIN): check every match.
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
SOURCE_DOC = "NBIM - Exclusion of companies (Council on Ethics)"
CATEGORY_MAP = [  # (pattern in the criterion, template category)
    (r"environment|greenhouse|climate", "environment"),
    (r"corruption|financial crime", "governance"),
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
    """normalized name -> ticker (universe first, then ETF holdings)."""
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
    print(f"NBIM: {len(df)} companies ({(df.category == 'Conduct-based').sum()} conduct-based), "
          f"{(df.ticker != '').sum()} matched to a ticker → {a.out_list}")

    rows = []
    for r in df[(df.ticker != "") & (df.category == "Conduct-based")].itertuples():
        cat = next((c for pat, c in CATEGORY_MAP if re.search(pat, r.criterion, re.I)), "other")
        verb = "excluded" if r.decision == "Exclusion" else "placed under observation"
        rows.append({
            "ticker": r.ticker, "publication_date": r.published, "category": cat,
            "description": (f"{r.company}: {verb} by the Norwegian sovereign wealth fund (NBIM, Council on "
                            f"Ethics recommendation) on {r.published}, criterion \"{r.criterion}\"."),
            "quote": r.criterion,
            "severity": "major" if r.decision == "Exclusion" else "moderate",
            "status": "to_verify", "amount_currency": "", "amount": None,
            "source_doc": SOURCE_DOC, "section": f"{r.category} / {r.decision}", "source_url": r.link,
            "reviewer": "", "review_date": "",
        })
    for r in df[df.ticker != ""].itertuples():
        flag = "universe" if r.in_universe else "via ETF"
        print(f"  [{flag:<7}] {r.ticker:<8} {r.company} | {r.category} | {r.criterion} | {r.decision} {r.published}")
    if rows:
        out = Path(a.out)
        merged = merge(out, pd.DataFrame(rows)[COLUMNS])
        merged.to_csv(out, index=False, encoding="utf-8")
        print(f"→ {out} : {len(rows)} NBIM row(s) added/replaced as to_verify ({len(merged)} total)")


if __name__ == "__main__":
    main()
