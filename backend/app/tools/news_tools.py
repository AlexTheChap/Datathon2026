"""News search tool (node ⑥, link L13): dated events from data/external/news/news_index.csv.

Deterministic, no LLM. Point-in-time: only events with `published_at <= as_of` are ever returned, where
`as_of` comes from the run (never from the model). Every call returns an evidence envelope, so the News and
Fundamentals sub-agents cite real events (`event_id`, source URL, publication date), never invented ones.

Sources in the index (`origin`): sec_8k (SEC 8-K filings, public domain; `excerpt` = the first words of the item text, read by
scripts/enrich_8k.py), macro_derived (central bank rate
decisions derived from the curated macro data). No press headlines: too few days and too noisy to be evidence.
Local stand-in of the Bedrock Knowledge Base (same filter: published <= as_of).
"""
from __future__ import annotations

import re
from functools import lru_cache

import pandas as pd
from app.core.config import get_settings
from app.tools.macro_tools import _envelope
from app.tools.context import effective_as_of
from strands import tool

ORIGINS = ("sec_8k", "macro_derived")
_COLUMNS = ["event_id", "published_at", "title", "excerpt", "source", "source_url", "tickers", "theme", "origin",
            "n_sources"]
MAX_LIMIT = 20


@lru_cache
def _load(path: str, mtime: float) -> pd.DataFrame:  # mtime in the key: reload when the corpus is rebuilt
    df = pd.read_csv(path, dtype=str).fillna("")
    df["n_sources"] = pd.to_numeric(df["n_sources"], errors="coerce").fillna(1).astype(int)
    if "excerpt" not in df.columns:               # an index built before scripts/enrich_8k.py
        df["excerpt"] = ""
    return df


def _index() -> pd.DataFrame:
    s = get_settings()
    path = s.abs_path(s.news_dir) / "news_index.csv"
    if not path.is_file():
        raise ValueError(f"News index not found: {path}. Build it with scripts/ingest_news.py.")
    return _load(str(path), path.stat().st_mtime)


def search_news(query: str | None = None, tickers: list[str] | None = None, themes: list[str] | None = None,
                origins: list[str] | None = None, days_back: int | None = None, limit: int = 10,
                as_of: str | None = None) -> dict:
    """Plain function behind the `news_search` tool (also used by tests and scripts)."""
    unknown = [o for o in (origins or []) if o not in ORIGINS]
    if unknown:
        raise ValueError(f"Unknown origins {unknown}; allowed: {list(ORIGINS)}.")
    limit = max(1, min(int(limit), MAX_LIMIT))
    cut = effective_as_of(as_of)
    df = _index()
    if "excerpt" not in df.columns:               # an index built before scripts/enrich_8k.py
        df = df.assign(excerpt="")
    df = df[df["published_at"] <= cut]                              # point-in-time filter
    n_before_window = len(df)
    if days_back:
        start = str((pd.Timestamp(cut) - pd.Timedelta(days=int(days_back))).date())
        df = df[df["published_at"] >= start]
    if tickers:
        wanted = set(tickers)
        df = df[df["tickers"].map(lambda t: bool(wanted & set(re.split(r"[;,\s]+", t))))]
    if themes:
        df = df[df["theme"].isin(themes)]
    if origins:
        df = df[df["origin"].isin(origins)]
    terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2]
    if terms:
        text = (df["title"] + " " + df["excerpt"] + " " + df["source"]).str.lower()
        df = df.assign(_score=text.map(lambda x: sum(t in x for t in terms)))
        df = df[df["_score"] > 0]
    else:
        df = df.assign(_score=0)
    df = df.sort_values(["_score", "n_sources", "published_at", "event_id"], ascending=[False, False, False, True])
    events = df.head(limit)[_COLUMNS].to_dict("records")
    inputs = {"query": query, "tickers": tickers, "themes": themes, "origins": origins,
              "days_back": days_back, "limit": limit, "as_of": cut}
    out = {"events": events, "n_matches": len(df), "n_returned": len(events),
           "as_of": cut, "n_events_available_at_as_of": int(n_before_window)}
    if events:
        say = (f"{len(df)} news event(s) matched as of {cut}; most relevant: "
               f"{events[0]['published_at']} {events[0]['title']} ({events[0]['source']}).")
    else:
        say = f"No news event matched as of {cut}."
    return {**_envelope(
        "news_search", inputs, out, say,
        ["Only events published on or before as_of are returned (point-in-time).",
         ("The corpus is incomplete: SEC 8-K filings of the universe's SEC filers (the `excerpt` is the first ~700 "
          "characters of the reported item, not the whole filing), central bank rate decisions derived from the "
          "macro data. "
          "No match is not proof that nothing happened."),
         "8-K filings and rate decisions are primary sources; no press coverage is included."],
        ["data/external/news/news_index.csv (SEC EDGAR 8-K, Bank of Canada / FRED derived)"]),
        "as_of": cut}


@tool
def news_search(query: str | None = None, tickers: list[str] | None = None, themes: list[str] | None = None,
                origins: list[str] | None = None, days_back: int | None = None, limit: int = 10) -> dict:
    """Search the dated news corpus (point-in-time: never after the run's reference date).
    Returns real events with their publication date, source and link, plus an evidence record.
    Args:
        query: Optional keywords matched against titles, e.g. "interest rates inflation".
        tickers: Optional tickers (or MARKET / MACRO / GEOPOLITICS / CRYPTO tags) to filter on.
        themes: Optional themes: company, rates, press, markets, central_banks, geopolitics, economy, corporate, crypto.
        origins: Optional sources: "sec_8k" (SEC filings, most reliable), "macro_derived" (rate decisions).
        days_back: Optional window in days before the reference date.
        limit: Maximum events returned (default 10, at most 20).
    """
    try:
        return search_news(query, tickers, themes, origins, days_back, limit)
    except (KeyError, ValueError) as e:
        return {"error": str(e), "note": "Fix the arguments and call the tool again."}
