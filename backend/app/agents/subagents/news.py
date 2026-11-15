"""News sub-agent (architecture node ⑤, link L13 → news & docs).

Three modes, chosen at each run by app.tools.toolsets.news_mode():
- "kb" (AF_KB_ID set): search_news on the Bedrock Knowledge Base "news & docs" (S3 Vectors, Titan v2 embeddings):
  dated SEC 8-K events, press headlines, central-bank rate decisions and annual-report risk/legal sections,
  filtered published_date <= as_of (the run's date: the model cannot ask for a later one).
- "local" (no KB, data/external/news/news_index.csv present): news_search on the same dated corpus in-process,
  published_at <= as_of. Event ids (N-…) in the finding are checked by code against what the search returned.
- "simulated" (neither: offline runs without the corpus): no tools, clearly fictional headlines, flagged
  SIMULATED end to end — the orchestrator must label them as such.
"""
from __future__ import annotations

from app.agents.subagents.base import event_validator, make_subagent_tool
from app.tools.toolsets import news_mode, news_toolset

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

LOCAL_PROMPT = """You are the News sub-agent of AlphaForge.
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

_PROMPTS = {"kb": KB_PROMPT, "local": LOCAL_PROMPT, "simulated": SIMULATED_PROMPT}


def _prompt() -> str:
    return _PROMPTS[news_mode()]


def _simulated() -> bool:
    return news_mode() == "simulated"


news_subagent = make_subagent_tool(
    "news_subagent",
    _prompt,
    tools=news_toolset,      # resolved per run: KB search_news, local news_search, or [] (SIMULATED)
    simulated=_simulated,
    extra_validator=event_validator,
    description="""Recent-news digest from the News sub-agent: dated, sourced news (SEC 8-K, rate decisions,
    press headlines) and annual-report risk/legal passages published on or before as_of. The corpus is
    incomplete: the agent says when it finds nothing. Cite its nested evidence_ids. If the returned record's
    assumptions say SIMULATED (no news corpus available), label the content as fictional and never mix it with
    real evidence.
    Args:
        subject: e.g. "news backdrop for NVDA and the chip sector".
        tickers: optional assets of particular interest.
    """,
)
