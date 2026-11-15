"""Universe ticker -> SEC CIK, shared by the SEC ingestion scripts (8-K events, XBRL fundamentals).

SEC's ticker file maps a bare ticker (the universe ticker without ".TO") to ONE company, and that is not always the company
of the universe. Two cases found on 2026-11-15, both fixed by OVERRIDES:
- CNR.TO is Canadian National Railway, but the SEC ticker "CNR" is Core Natural Resources (formerly CONSOL Energy): the first
  version of the 8-K and fundamentals files held that company's data under CNR.TO. Canadian National is "CNI" (CIK 16868).
- XOM now points to the new holding company ExxonMobil Holdings Corp (CIK 2093318), with no history: the filings are still
  made by Exxon Mobil Corp (CIK 34088), as scripts/fetch_10k.py already assumed.
Every script prints the SEC entity name next to each ticker: read it, a mismatch is visible at once.
"""
from __future__ import annotations

import requests

OVERRIDES = {"XOM": "0000034088", "CNR.TO": "0000016868"}


def cik_map(headers: dict) -> dict[str, str]:
    """SEC ticker file: {SEC ticker: 10-digit CIK}."""
    j = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=30).json()
    return {v["ticker"]: str(v["cik_str"]).zfill(10) for v in j.values()}


def cik_for(ticker: str, sec: dict[str, str]) -> str | None:
    """CIK of a universe ticker (override first, then the SEC ticker without ".TO"); None for non-SEC filers."""
    return OVERRIDES.get(ticker) or sec.get(ticker.replace(".TO", ""))
