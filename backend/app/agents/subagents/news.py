"""News sub-agent — real dated events from the news corpus, point-in-time (architecture node ⑤).
The corpus (data/external/news/news_index.csv, built by scripts/ingest_news.py) mixes SEC 8-K filings, central bank
rate decisions and press headlines. The sub-agent searches it with `news_search` (never after the run's as_of)
and may only report events the search returned: the output is checked by code (see event_validator).
"""
from __future__ import annotations

from app.agents.subagents.base import event_validator, make_subagent_tool
from app.tools.news_tools import news_search

SYSTEM_PROMPT = """You are the News sub-agent of AlphaForge.
Using ONLY the `news_search` tool, find the dated news relevant to the brief. Search with the investor's tickers
and/or themes and keywords; try origins ["sec_8k"] for company events (primary sources), ["macro_derived"] for
central bank decisions and ["gdelt"] for press headlines. Never use anything that the tool did not return.
Distinguish reliability: SEC filings and rate decisions are facts; press headlines are unreviewed, noisy signals.
If nothing relevant is found, say so: the corpus is incomplete, so absence is not proof that nothing happened.
Return ONLY a JSON object:
{
  "headline": "<one-sentence news takeaway>",
  "details": ["<date> <title> (<origin>) [event_id] — <evidence_id of the search>"],
  "severity": "info" | "warn" | "block",
  "items": [{"event_id": "N-...", "published_at": "YYYY-MM-DD", "title": "...", "origin": "sec_8k|macro_derived|gdelt",
             "source_url": "..."}],
  "no_news": ["<tickers or topics with no matching event>"]
}
Copy event_id, date, title and source_url exactly from the tool results. No investment advice, no forecasts.
"""

news_subagent = make_subagent_tool(
    "news_subagent",
    SYSTEM_PROMPT,
    tools=[news_search],
    extra_validator=event_validator,
    description="""Dated news from the News sub-agent: SEC 8-K filings, central bank rate decisions and press
    headlines, point-in-time (nothing after the run's reference date). Every item is a real event with its source
    link; the corpus is incomplete, so the agent says when it finds nothing. Cite its nested evidence_ids.
    Args:
        subject: e.g. "news backdrop for NVDA and the chip sector".
        tickers: optional assets of particular interest.
    """,
)
