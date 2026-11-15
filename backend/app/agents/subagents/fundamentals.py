"""Fundamentals sub-agent — filing-based qualitative facts (architecture node ⑤).

The starter dataset has no income-statement fundamentals; the curated 10-K/NBIM
extracts (controversies.csv: section, quote, source_doc) are the filing evidence
available. The sub-agent frames them as company-quality facts — nothing invented.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools.analysis_tools import esg_screen

SYSTEM_PROMPT = """You are the Fundamentals sub-agent of AlphaForge.

The prototype has NO income-statement data. Using ONLY the esg_screen tool's
"controversies" part (10-K and NBIM extracts with quotes and sources), report
documented company-quality facts relevant to the brief. If no documented fact
exists for a ticker, say so explicitly — never invent fundamentals.

Return ONLY a JSON object:
{
  "headline": "<one-sentence takeaway>",
  "details": ["<documented fact with its evidence_id and source_doc>"],
  "severity": "info" | "warn" | "block",
  "no_data": ["<tickers with no documented facts>"]
}
No investment advice.
"""

fundamentals_subagent = make_subagent_tool(
    "fundamentals_subagent",
    SYSTEM_PROMPT,
    tools=[esg_screen],
    description="""Documented company facts from filings (10-K extracts, NBIM decisions)
    via the Fundamentals sub-agent. Deliberately narrow: states explicitly when a
    ticker has no documented data. Cite its nested evidence_ids.

    Args:
        subject: e.g. "documented red flags relevant to the investor's constraints".
        tickers: assets to check, e.g. ["TSLA", "XOM"].
    """,
)
