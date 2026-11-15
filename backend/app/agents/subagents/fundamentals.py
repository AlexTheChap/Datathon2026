"""Fundamentals sub-agent — filing-based facts (architecture node ⑤).
The starter dataset has no income-statement fundamentals. The filing evidence available is: SEC 8-K events (earnings
releases, acquisitions, executive changes, impairments, restatements…) from the news corpus, and the documented
10-K/NBIM controversies. The sub-agent frames them as company-quality facts — nothing invented.
"""
from __future__ import annotations

from app.agents.subagents.base import event_validator, make_subagent_tool
from app.tools.analysis_tools import esg_screen
from app.tools.news_tools import news_search

SYSTEM_PROMPT = """You are the Fundamentals sub-agent of AlphaForge.
The prototype has NO income-statement or balance-sheet data. Using ONLY your tools, report the documented
company facts relevant to the brief:
- `news_search` with origins ["sec_8k"] and the tickers: dated SEC 8-K filings (financial results, acquisitions,
  executive departures, impairments, restatements…). They tell WHAT was filed and WHEN, not the figures inside.
- `esg_screen`: documented controversies from 10-K and NBIM extracts (quote, source, review status).
If no documented fact exists for a ticker, say so explicitly — never invent fundamentals, ratios or earnings.
Return ONLY a JSON object:
{
  "headline": "<one-sentence takeaway>",
  "details": ["<documented fact with its date, source and evidence_id>"],
  "severity": "info" | "warn" | "block",
  "items": [{"event_id": "N-...", "published_at": "YYYY-MM-DD", "title": "...", "source_url": "..."}],
  "no_data": ["<tickers with no documented facts>"]
}
Copy event ids, dates, titles and links exactly from the tool results. No investment advice.
"""

fundamentals_subagent = make_subagent_tool(
    "fundamentals_subagent",
    SYSTEM_PROMPT,
    tools=[news_search, esg_screen],
    extra_validator=event_validator,
    description="""Documented company facts from filings (SEC 8-K events, 10-K extracts, NBIM decisions) via the
    Fundamentals sub-agent. Deliberately narrow: no financial statements in the prototype, and it states explicitly
    when a ticker has no documented data. Cite its nested evidence_ids.
    Args:
        subject: e.g. "documented red flags relevant to the investor's constraints".
        tickers: assets to check, e.g. ["TSLA", "XOM"].
    """,
)
