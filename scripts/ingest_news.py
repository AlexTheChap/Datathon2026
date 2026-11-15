"""
Build a dated, sourced NEWS corpus for the Knowledge Base (News agent), supporting the
point-in-time filter "published <= as_of".

Three sources, from most to least reliable:
  macro  : Bank of Canada and Fed rate decisions, DERIVED from data already in the repo
           (data/curated/macro_pit.csv). No network access.
  sec8k  : company events filed with the SEC (Form 8-K) for universe stocks that file on
           EDGAR. Public domain.
  gdelt  : press headlines (GDELT DOC 2.0): title + link only, never the article body.
  gdelt_recent : last --days days (default 7): market-moving topics (markets, central banks,
           geopolitics, economy, corporate, crypto) + one query per company.

Outputs (in --out):
  news_index.csv                    one event per row
  kb/<event_id>.txt                 title + short summary (what the KB indexes)
  kb/<event_id>.txt.metadata.json   filtering metadata for Bedrock Knowledge Bases

Examples:
  python scripts/ingest_news.py --sources macro
  python scripts/ingest_news.py --sources macro sec8k --email you@example.com
  python scripts/ingest_news.py --sources gdelt --start 2022-01-01 --end 2022-12-31
  python scripts/ingest_news.py --sources gdelt_recent --days 7

sec8k and gdelt need network access (run on your machine / SageMaker).
Review the output by eye before loading it into the Knowledge Base.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- helpers

COLUMNS = ["event_id", "published_at", "title", "summary", "source", "source_url", "tickers",
           "theme", "language", "origin", "n_sources", "date_method", "retrieved_on"]
TODAY = date.today().isoformat()


def event_id(*parts) -> str:
    return "N-" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


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
    return pd.DataFrame(rows, columns=COLUMNS)


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
    h = {"User-Agent": f"AlphaForge Datathon {email}"}
    tick = requests.get("https://www.sec.gov/files/company_tickers.json", headers=h, timeout=30).json()
    cik = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in tick.values()}
    rows = []
    for t in universe.loc[universe["asset_class"] == "equity", "ticker"]:
        c = cik.get(t.replace(".TO", ""))         # Canadian stocks listed in New York often file
        if not c:                                 # 6-K/40-F (not handled); ENB files 8-Ks
            continue
        time.sleep(0.15)                          # SEC rule: max 10 requests/second
        f = requests.get(f"https://data.sec.gov/submissions/CIK{c}.json", headers=h, timeout=30).json()
        r = f["filings"]["recent"]
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


# ---------------------------------------------------------------- 3. GDELT (press headlines)

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_get(session, params: dict, pause: float, max_retries: int = 8, max_wait: float = 120.0):
    """GET one GDELT query and return the parsed JSON, or None if every attempt failed.

    GDELT answers HTTP 429 (or a plain-text body) when the client IP goes over its limit of one
    request every 5 seconds. The limit applies per IP, so it can trigger even when this script
    spaces its own calls, if the IP is shared (shared instance, NAT, other users). On 429 the
    SAME request is retried with exponential backoff (honouring Retry-After) instead of being
    skipped, so no month is silently lost."""
    import random
    import requests
    for attempt in range(max_retries + 1):
        wait = min(pause * 2 ** (attempt + 1), max_wait) + random.uniform(0, pause)
        try:
            r = session.get(GDELT, params=params, timeout=60)
        except requests.RequestException as e:
            print(f"[gdelt] network error ({e}); retry in {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = max(wait, float(retry_after))
            print(f"[gdelt] HTTP {r.status_code}; retry {attempt + 1}/{max_retries} in {wait:.0f}s")
            time.sleep(wait)
            continue
        try:
            return r.json()
        except ValueError:                       # plain-text body (rate limit message, bad query)
            print(f"[gdelt] non-JSON answer ({r.text[:80]!r}); retry in {wait:.0f}s")
            time.sleep(wait)
    return None


def gdelt_events(universe: pd.DataFrame, start: str, end: str, max_per_call: int = 75,
                 pause: float = 6.0, cache: Path | None = None) -> pd.DataFrame:
    """Per company and per month: English headlines. Deduplicated by normalized title;
    n_sources = number of domains that carried the same title on the same day.

    `cache` (JSON lines) stores every finished (ticker, month) query, so a re-run after an
    interruption or a failure resumes where it stopped. Failed queries are not cached."""
    import requests
    session = requests.Session()
    session.headers["User-Agent"] = "AlphaForge-Datathon/1.0 (educational; news ingestion)"
    months = pd.date_range(start, end, freq="MS")
    done: dict[str, list[dict]] = {}
    if cache is not None and cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            done[rec["key"]] = rec["articles"]
    hits, failed = [], []
    for _, a in universe[universe["asset_class"] == "equity"].iterrows():
        name = re.sub(r"\s*\(.*?\)", "", a["name"])
        for m0 in months:
            key = f"{a['ticker']}|{m0:%Y-%m}|{start}|{end}"
            if key not in done:
                m1 = min(m0 + pd.offsets.MonthEnd(0), pd.Timestamp(end))
                params = {"query": f'"{name}" sourcelang:english', "mode": "artlist", "format": "json",
                          "maxrecords": max_per_call, "sort": "hybridrel",
                          "startdatetime": m0.strftime("%Y%m%d000000"),
                          "enddatetime": m1.strftime("%Y%m%d235959")}
                j = gdelt_get(session, params, pause)
                time.sleep(pause)                # GDELT asks clients to space out requests
                if j is None:
                    failed.append(f"{a['ticker']} {m0:%Y-%m}")
                    print(f"[gdelt] GIVING UP on {a['ticker']} {m0:%Y-%m}")
                    continue
                done[key] = [{"title": art.get("title", ""), "url": art.get("url", ""),
                              "domain": art.get("domain", ""), "seendate": art.get("seendate", "")[:8],
                              "language": art.get("language", "")} for art in j.get("articles", [])]
                if cache is not None:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    with cache.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"key": key, "articles": done[key]}, ensure_ascii=False) + "\n")
            hits += [{"ticker": a["ticker"], **art} for art in done[key]]
    if failed:
        print(f"[gdelt] {len(failed)} query(ies) failed after retries; re-run to retry them: "
              + ", ".join(failed[:10]) + (" ..." if len(failed) > 10 else ""))
    return hits_to_events(hits)


def hits_to_events(hits: list[dict]) -> pd.DataFrame:
    """GDELT articles -> event rows. Deduplicated by (tag, day, normalized title);
    n_sources = number of domains with the same title. Optional "theme" key (default "press")."""
    if not hits:
        return pd.DataFrame(columns=COLUMNS)
    h = pd.DataFrame(hits)
    if "theme" not in h:
        h["theme"] = "press"
    h["published_at"] = pd.to_datetime(h["seendate"], format="%Y%m%d").dt.date.astype(str)
    h["key"] = h["title"].map(norm_title)
    g = h.groupby(["ticker", "published_at", "key"]).agg(
        title=("title", "first"), url=("url", "first"), domain=("domain", "first"),
        theme=("theme", "first"), n_sources=("domain", "nunique")).reset_index()
    return pd.DataFrame({
        "event_id": [event_id(r.ticker, r.published_at, r.key) for r in g.itertuples()],
        "published_at": g["published_at"], "title": g["title"],
        "summary": g["title"] + " (press headline; article not reproduced)",
        "source": g["domain"], "source_url": g["url"], "tickers": g["ticker"], "theme": g["theme"],
        "language": "en", "origin": "gdelt", "n_sources": g["n_sources"],
        "date_method": "first seen by GDELT (seendate)", "retrieved_on": TODAY,
    }, columns=COLUMNS)


# Market-moving topics for the "recent" mode. tag = value written in the `tickers` column.
TOPIC_QUERIES = {
    "markets": ("MARKET", '(stocks OR "stock market" OR "Wall Street" OR "S&P 500" OR TSX OR Nasdaq OR "bond yields")'),
    "central_banks": ("MACRO", '("Federal Reserve" OR "Bank of Canada" OR "interest rates" OR inflation OR "rate cut" OR "rate hike")'),
    "geopolitics": ("GEOPOLITICS", '(sanctions OR tariffs OR "trade war" OR war OR OPEC OR "oil prices" OR geopolitical)'),
    "economy": ("MACRO", '(recession OR "jobs report" OR GDP OR unemployment OR "consumer prices")'),
    "corporate": ("MARKET", '(earnings OR merger OR acquisition OR bankruptcy OR layoffs OR "profit warning")'),
    "crypto": ("CRYPTO", '(bitcoin OR ethereum OR cryptocurrency)'),
}


def gdelt_recent(universe: pd.DataFrame, days: int = 7, max_per_call: int = 40, pause: float = 6.0,
                 cache: Path | None = None, companies: bool = True) -> pd.DataFrame:
    """Last `days` days: market-moving topic headlines (TOPIC_QUERIES) + one query per company.
    Same cache, retries and deduplication as gdelt_events. Dates are real GDELT seendate days,
    so the point-in-time filter published <= as_of keeps working."""
    import requests
    session = requests.Session()
    session.headers["User-Agent"] = "AlphaForge-Datathon/1.0 (educational; news ingestion)"
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    start = end.normalize() - pd.Timedelta(days=days - 1)
    done: dict[str, list[dict]] = {}
    if cache is not None and cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            done[rec["key"]] = rec["articles"]
    jobs = [(f"topic:{label}", tag, label, f"{q} sourcelang:english")
            for label, (tag, q) in TOPIC_QUERIES.items()]
    for _, a in (universe[universe["asset_class"] == "equity"].iterrows() if companies else []):
        name = re.sub(r"\s*\(.*?\)", "", a["name"])
        jobs.append((f"co:{a['ticker']}", a["ticker"], "press", f'"{name}" sourcelang:english'))
    hits, failed = [], []
    for ident, tag, theme, query in jobs:
        key = f"{ident}|{start:%Y%m%d}|{end:%Y%m%d}"
        if key not in done:
            params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": max_per_call,
                      "sort": "hybridrel", "startdatetime": start.strftime("%Y%m%d000000"),
                      "enddatetime": end.strftime("%Y%m%d%H%M%S")}
            j = gdelt_get(session, params, pause)
            time.sleep(pause)
            if j is None:
                failed.append(ident)
                print(f"[gdelt] GIVING UP on {ident}")
                continue
            done[key] = [{"title": art.get("title", ""), "url": art.get("url", ""),
                          "domain": art.get("domain", ""), "seendate": art.get("seendate", "")[:8]}
                         for art in j.get("articles", [])]
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with cache.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "articles": done[key]}, ensure_ascii=False) + "\n")
            print(f"[gdelt] {ident}: {len(done[key])} articles")
        hits += [{"ticker": tag, "theme": theme, **art} for art in done[key]]
    if failed:
        print(f"[gdelt] {len(failed)} query(ies) failed after retries; re-run to retry them: " + ", ".join(failed))
    return hits_to_events(hits)


# ---------------------------------------------------------------- output

def write_outputs(df: pd.DataFrame, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    kb = out / "kb"
    kb.mkdir(exist_ok=True)
    idx = out / "news_index.csv"
    if idx.exists():                              # merge with an existing index (other sources)
        old = pd.read_csv(idx, dtype=str)
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
    ap.add_argument("--sources", nargs="+", default=["macro"], choices=["macro", "sec8k", "gdelt", "gdelt_recent"])
    ap.add_argument("--start", default="2021-11-12")
    ap.add_argument("--end", default="2026-11-12")
    ap.add_argument("--days", type=int, default=7, help="window for gdelt_recent (days up to today)")
    ap.add_argument("--topics-only", action="store_true", help="gdelt_recent: skip the per-company queries (fast)")
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
    if "gdelt" in a.sources:
        parts.append(gdelt_events(uni, a.start, a.end, cache=Path(a.out) / "gdelt_cache.jsonl"))
    if "gdelt_recent" in a.sources:
        parts.append(gdelt_recent(uni, a.days, cache=Path(a.out) / "gdelt_cache.jsonl", companies=not a.topics_only))
    write_outputs(pd.concat(parts, ignore_index=True), Path(a.out))


if __name__ == "__main__":
    main()