"""News sub-agent — SIMULATED placeholder (architecture node ⑤).

No news feed exists in the prototype (the starter dataset ships no news and no
licence to scrape it). The sub-agent runs without tools and invents clearly
fictional headlines, flagged SIMULATED end to end — the orchestrator must label
them as such. Replace with a real RAG feed (Bedrock Knowledge Base, published
<= as_of) when a corpus exists.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool

SYSTEM_PROMPT = """You are the News sub-agent of AlphaForge (SIMULATED mode).

The prototype has no news feed, so you invent a SHORT plausible news digest for
the subject — clearly fictional, market-moving style (macro headlines, sector
news, company news). Every item must be presented as simulated.

Return ONLY a JSON object:
{
  "headline": "<one-sentence digest>",
  "details": ["[SIMULATED] <headline> (<assumed date>)"],
  "severity": "info",
  "disclaimer": "All items are fictional; no real news feed exists in this prototype."
}
"""

news_subagent = make_subagent_tool(
    "news_subagent",
    SYSTEM_PROMPT,
    tools=[],   # no real feed yet: fictional content, labeled
    simulated=True,
    description="""Recent-news digest from the News sub-agent — SIMULATED content
    (no real news feed in the prototype): always label it as fictional when you
    use it, and never mix it with real tool evidence.

    Args:
        subject: e.g. "news backdrop for NVDA and the chip sector".
        tickers: optional assets of particular interest.
    """,
)
