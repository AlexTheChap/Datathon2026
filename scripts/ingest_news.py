"""
Build a dated, sourced NEWS corpus for the Knowledge Base (News agent), supporting the
point-in-time filter "published <= as_of".

Two primary sources (press headlines were dropped: too few days and too noisy to be useful evidence):
  macro  : Bank of Canada and Fed rate decisions, DERIVED from data already in the repo
           (data/curated/macro_pit.csv). No network access.
  sec8k  : company events filed with the SEC (Form 8-K) for universe stocks that file on
           EDGAR. Public domain.

Outputs (in --out):
  news_index.csv                    one event per row
  kb/<event_id>.txt                 title + short summary (what the KB indexes)
  kb/<event_id>.txt.metadata.json   filtering metadata for Bedrock Knowledge Bases

Examples:
  python scripts/ingest_news.py --sources macro
  python scripts/ingest_news.py --sources macro sec8k --email you@example.com

sec8k needs network access.
Review the output by eye before loading it into the Knowledge Base.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))   # sibling helper: sec_cik.py

# ---------------------------------------------------------------- helpers

COLUMNS = ["event_id", "published_at", "title", "summary", "source", "source_url", "tickers",
           "theme", "language", "origin", "n_sources", "date_method", "retrieved_on", "excerpt"]
TODAY = date.today().isoformat()


def event_id(*parts) -> str:
    return "N-" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- 1. macro (no network)

RATE_SERIES = {
    # series: (bank, change threshold, reference URL, rate name)
    "V39079": ("Bank of Canada", 0.10,
               "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/",
               "policy rate"),
    "DFF": ("Federal Reserve", 0.20,
            "https://www.federalreserve.gov/monetarypolicy/openmarket.htm",
            "federal funds rate"),
}


def macro_events(curated: Path, start: str, end: str) -> pd.DataFrame:
    """One rate change = one decision. The series shows the EFFECTIVE date; the announcement
    usually comes one business day earlier, so published_at = effective date - 1 business day
    (documented approximation, see date_method)."""
    m = pd.read_csv(curated / "macro_pit.csv", parse_dates=["date"])
    rows = []
    for sid, (bank, thr, url, label) in RATE_SERIES.items():
        s = m[m["series_id"] == sid].sort_values("date").set_index("date")["value"]
        ch = s.diff()
        for d, delta in ch[ch.abs() >= thr].items():
            pub = (d - pd.offsets.BDay(1)).date()
            if not (start <= pub.isoformat() <= end):
                continue
            verb = "raises" if delta > 0 else "cuts"
            bp = round(abs(delta) * 4) / 4 * 100          # round to the nearest quarter point (DFF = effective rate)
            if sid == "DFF":
                title = (f"{bank} {verb} its target range by about {bp:.0f} basis points "
                         f"(effective federal funds rate: {s[d]:.2f}%)")
            else:
                title = f"{bank} {verb} its {label} by {bp:.0f} basis points to {s[d]:.2f}%"
            rows.append({
                "event_id": event_id(sid, d.date()), "published_at": pub.isoformat(), "title": title,
                "summary": f"{title}. Change observed in series {sid} (effective {d.date()}).",
                "source": f"{bank} (series {sid})", "source_url": url, "tickers": "MACRO",
                "theme": "rates", "language": "en", "origin": "macro_derived", "n_sources": 1,
                "date_method": "announcement estimated as effective date - 1 business day",
                "retrieved_on": TODAY,
            })
    return pd.DataFrame(rows, columns=COLUMNS).fillna({"excerpt": ""})


# ---------------------------------------------------------------- 2. SEC 8-K

ITEMS_8K = {  # 8-K items kept (the most informative for an investor)
    "1.01": "material agreement", "1.03": "bankruptcy or receivership", "1.05": "cybersecurity incident",
    "2.01": "acquisition or disposition of assets", "2.02": "financial results",
    "2.05": "exit or restructuring costs", "2.06": "material impairment",
    "3.01": "delisting notice", "4.02": "non-reliance on financial statements (restatement)",
    "5.02": "executive departure or appointment", "8.01": "other material event",
}


def sec_8k_events(universe: pd.DataFrame, start: str, end: str, email: str) -> pd.DataFrame:
    import requests
    h = {"User-Agent": f"Fitfolio Datathon {email}"}
    from sec_cik import cik_for, cik_map
    sec = cik_map(h)
    rows = []
    for t in universe.loc[universe["asset_class"] == "equity", "ticker"]:
        c = cik_for(t, sec)                       # Canadian stocks listed in New York often file
        if not c:                                 # 6-K/40-F (not handled); ENB files 8-Ks
            continue
        time.sleep(0.15)                          # SEC rule: max 10 requests/second
        f = requests.get(f"https://data.sec.gov/submissions/CIK{c}.json", headers=h, timeout=30).json()
        r = f["filings"]["recent"]
        print(f"[sec8k] {t:<8} -> SEC entity {f.get('name')!r} (CIK {c})")
        for form, acc, filed, items, doc in zip(r["form"], r["accessionNumber"], r["filingDate"],
                                                r.get("items", [""] * len(r["form"])), r["primaryDocument"]):
            if form != "8-K" or not (start <= filed <= end):
                continue
            codes = [i.strip() for i in str(items).split(",") if i.strip() in ITEMS_8K]
            if not codes:
                continue
            labels = ", ".join(ITEMS_8K[i] for i in codes)
            url = f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{acc.replace('-', '')}/{doc}"
            title = f"{t}: Form 8-K ({labels})"
            rows.append({
                "event_id": event_id(t, acc), "published_at": filed, "title": title,
                "summary": f"{title}, filed with the SEC on {filed}. Items: {', '.join(codes)}.",
                "source": "SEC EDGAR (8-K)", "source_url": url, "tickers": t,
                "theme": "company", "language": "en", "origin": "sec_8k", "n_sources": 1,
                "date_method": "SEC filing date", "retrieved_on": TODAY,
            })
    return pd.DataFrame(rows, columns=COLUMNS)


def write_outputs(df: pd.DataFrame, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    kb = out / "kb"
    kb.mkdir(exist_ok=True)
    idx = out / "news_index.csv"
    if idx.exists():                              # merge with an existing index (other sources)
        old = pd.read_csv(idx, dtype=str).fillna("")
        df = df.astype(object).fillna("")
        if "excerpt" in old.columns:                # a refreshed event keeps what enrich_8k.py already read in it
            keep = old.set_index("event_id")[["excerpt", "summary"]]
            known = df["event_id"].isin(keep.index) & (df["excerpt"] == "")
            df.loc[known, "excerpt"] = df.loc[known, "event_id"].map(keep["excerpt"])
            df.loc[known, "summary"] = df.loc[known, "event_id"].map(keep["summary"])
        df = pd.concat([old[~old["event_id"].isin(df["event_id"])], df.astype(str)], ignore_index=True)
    df = df.sort_values(["published_at", "event_id"])
    df.to_csv(idx, index=False)
    for r in df.itertuples():
        (kb / f"{r.event_id}.txt").write_text(f"{r.title}\n\n{r.summary}\n\nSource: {r.source} - {r.source_url}\n",
                                              encoding="utf-8")
        meta = {"metadataAttributes": {
            "doc_type": "news", "ticker": str(r.tickers).split(";")[0], "tickers": str(r.tickers),
            "published_date": int(str(r.published_at).replace("-", "")), "theme": r.theme,
            "origin": r.origin, "n_sources": int(float(r.n_sources)), "language": r.language,
            "source_url": r.source_url}}
        (kb / f"{r.event_id}.txt.metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
    print(f"{len(df)} events in {idx}; KB documents in {kb}")
    print(df.groupby("origin").size().to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=["macro"], choices=["macro", "sec8k"])
    ap.add_argument("--start", default="2021-11-12")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--email", help="required for sec8k (User-Agent header required by the SEC)")
    ap.add_argument("--curated", default="data/curated")
    ap.add_argument("--out", default="data/external/news")
    a = ap.parse_args()
    curated = Path(a.curated)
    uni = pd.read_csv(curated / "assets_universe.csv")
    parts = []
    if "macro" in a.sources:
        parts.append(macro_events(curated, a.start, a.end))
    if "sec8k" in a.sources:
        if not a.email:
            ap.error("--email is required for sec8k")
        parts.append(sec_8k_events(uni, a.start, a.end, a.email))
    write_outputs(pd.concat(parts, ignore_index=True), Path(a.out))


if __name__ == "__main__":
    main()