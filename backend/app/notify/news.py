"""News about the stocks a person HOLDS: where the SMS alerts get their facts.

Two sources, same output (NewsEvent):
  SecFilingsSource  live: SEC EDGAR Form 8-K filings of the held tickers, filed in (since, until]. Same items and
                    same CIK rules as scripts/ingest_news.py / scripts/sec_cik.py (copied here because the Lambda
                    does not ship scripts/ nor pandas/requests: standard library only).
  CorpusNewsSource  the dated corpus already in the repo (data/external/news/news_index.csv): local demos/tests.

Only primary, dated facts (a company filing with the SEC), never commentary or price targets. Limits, stated in
the README: TSX-only companies file on SEDAR+ (no free API) and ETFs file no 8-K, so they get no alert.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

# from scripts/ingest_news.py (the most informative 8-K items for an investor)
ITEMS_8K = {
    "1.01": "material agreement", "1.03": "bankruptcy or receivership", "1.05": "cybersecurity incident",
    "2.01": "acquisition or disposition of assets", "2.02": "financial results",
    "2.05": "exit or restructuring costs", "2.06": "material impairment",
    "3.01": "delisting notice", "4.02": "non-reliance on financial statements (restatement)",
    "5.02": "executive departure or appointment", "8.01": "other material event",
}
# from scripts/sec_cik.py: the SEC's ticker file points these tickers to another company
CIK_OVERRIDES = {"XOM": "0000034088", "CNR.TO": "0000016868"}
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    ticker: str
    published_at: str      # YYYY-MM-DD
    headline: str          # short, factual: "financial results"
    source: str
    url: str


class NewsSource(Protocol):
    def events(self, tickers: Iterable[str], since: str, until: str) -> list[NewsEvent]: ...


def _event_id(*parts: str) -> str:  # same scheme as scripts/ingest_news.py
    return "N-" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


class SecFilingsSource:
    """SEC EDGAR, 10 requests/second at most, with the contact the SEC requires in the User-Agent
    (AF_SEC_CONTACT, e.g. "Fitfolio Datathon team@example.com")."""

    def __init__(self, contact: str | None = None, pause: float = 0.15, opener=None):
        self.contact = contact or os.environ.get("AF_SEC_CONTACT")
        if not self.contact:
            raise ValueError("AF_SEC_CONTACT is required by the SEC (User-Agent with a contact email).")
        self.pause = pause
        self._open = opener or urllib.request.urlopen
        self._ciks: dict[str, str] | None = None

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": self.contact, "Accept": "application/json"})
        with self._open(req, timeout=30) as r:
            return json.loads(r.read())

    def cik(self, ticker: str) -> str | None:
        if ticker in CIK_OVERRIDES:
            return CIK_OVERRIDES[ticker]
        if self._ciks is None:
            j = self._get("https://www.sec.gov/files/company_tickers.json")
            self._ciks = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in j.values()}
        return self._ciks.get(ticker.replace(".TO", "").replace("-", "."))

    def events(self, tickers: Iterable[str], since: str, until: str) -> list[NewsEvent]:
        out = []
        for t in sorted(set(tickers)):
            c = self.cik(t)
            if not c:
                continue
            time.sleep(self.pause)
            r = self._get(f"https://data.sec.gov/submissions/CIK{c}.json")["filings"]["recent"]
            for form, acc, filed, items, doc in zip(r["form"], r["accessionNumber"], r["filingDate"],
                                                    r.get("items", [""] * len(r["form"])), r["primaryDocument"]):
                if form != "8-K" or not (since < filed <= until):
                    continue
                codes = [i.strip() for i in str(items).split(",") if i.strip() in ITEMS_8K]
                if not codes:
                    continue
                out.append(NewsEvent(
                    event_id=_event_id(t, acc), ticker=t, published_at=filed,
                    headline=", ".join(ITEMS_8K[i] for i in codes), source="SEC 8-K filing",
                    url=f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{acc.replace('-', '')}/{doc}"))
        return sorted(out, key=lambda e: (e.published_at, e.ticker))


class CorpusNewsSource:
    """news_index.csv (scripts/ingest_news.py): company events only (macro rate decisions concern no holding)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or REPO_ROOT / "data" / "external" / "news" / "news_index.csv")

    def events(self, tickers: Iterable[str], since: str, until: str) -> list[NewsEvent]:
        held = set(tickers)
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("theme") != "company" or not (since < r["published_at"] <= until):
                    continue
                for t in (x.strip() for x in re.split(r"[|,;]", r["tickers"]) if x.strip()):
                    if t in held:
                        title = r["title"]
                        head = title.split("(", 1)[1].rstrip(")") if "(" in title else title
                        out.append(NewsEvent(event_id=r["event_id"], ticker=t, published_at=r["published_at"],
                                             headline=head, source=r["source"], url=r["source_url"]))
        return sorted(out, key=lambda e: (e.published_at, e.ticker))
