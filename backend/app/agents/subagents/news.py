"""News sub-agent (architecture node ⑤, link L13 → Bedrock Knowledge Base).

Two modes, chosen at each run by app.tools.toolsets (AF_KB_ID):
- KB mode: search_news on the Bedrock Knowledge Base "news & docs" (S3 Vectors, Titan v2 embeddings):
  dated SEC 8-K events, press headlines, central-bank rate decisions and annual-report risk/legal
  sections, filtered published_date <= as_of. Every fact carries an evidence_id and a date.
- SIMULATED mode (no AF_KB_ID: tests, offline runs): no tools, clearly fictional headlines,
  flagged SIMULATED end to end — the orchestrator must label them as such.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools.toolsets import news_kb_enabled, news_toolset

KB_PROMPT = """You are the News sub-agent of AlphaForge (datathon prototype).

Use ONLY the search_news tool. Always pass the as_of date given in the brief: nothing published
after it may be used. Search at least twice: (1) company news and annual-report risk factors for the
tickers of interest, (2) the market or macro backdrop (tickers "MARKET", "MACRO", "GEOPOLITICS",
"CRYPTO"). Every fact you state must come from a tool result and carry its date and evidence_id.
Press items are headlines only: never claim details the headline does not state.
If nothing relevant is found, say so; never invent news.

Return ONLY a JSON object, no prose around it:
{
  "headline": "<one-sentence news backdrop>",
  "details": ["<YYYY-MM-DD> <fact> [ev-...]"],
  "severity": "info" | "warn",
  "sources": [{"title": "...", "published_date": "YYYY-MM-DD", "source_url": "..."}]
}
Keep it under 8 details. No investment advice.
"""

SIMULATED_PROMPT = """You are the News sub-agent of AlphaForge (SIMULATED mode).

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

# Kept for backward compatibility (earlier imports of news.SYSTEM_PROMPT).
SYSTEM_PROMPT = SIMULATED_PROMPT


def _prompt() -> str:
    return KB_PROMPT if news_kb_enabled() else SIMULATED_PROMPT


def _simulated() -> bool:
    return not news_kb_enabled()


news_subagent = make_subagent_tool(
    "news_subagent",
    _prompt,
    tools=news_toolset,      # resolved per run: [search_news] on the KB, or [] (SIMULATED)
    simulated=_simulated,
    description="""Recent-news digest from the News sub-agent: dated, sourced news and
    annual-report risk/legal passages published on or before as_of (Bedrock Knowledge Base).
    Cite its nested evidence_ids. If the returned record's assumptions say SIMULATED (no
    knowledge base configured), label the content as fictional and never mix it with real evidence.

    Args:
        subject: e.g. "news backdrop for NVDA and the chip sector".
        tickers: optional assets of particular interest.
    """,
)
