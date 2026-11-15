"""Fundamentals sub-agent — reported financials and filing facts (architecture node ⑤).

Tools (point-in-time: nothing filed or published after the run's as_of):
- `company_fundamentals`: revenue, profit, margins, cash flow, balance sheet, leverage, growth, from the SEC XBRL
  filings (10-K / 10-Q, and 40-F / 6-K for the Canadian companies, us-gaap or IFRS). Every ratio is computed by the tool.
- `news_search` (origin sec_8k): the dated 8-K filings (results, acquisitions, executive changes, impairments…).
- `esg_screen`: documented controversies from 10-K and NBIM extracts, with their review status.
Not covered, and said so: companies that do not file with the SEC (ATD, CSU), and any valuation multiple.
"""
from __future__ import annotations

from app.agents.subagents.base import event_validator, make_subagent_tool
from app.tools.analysis_tools import esg_screen
from app.tools.fundamentals_tools import company_fundamentals
from app.tools.news_tools import news_search

SYSTEM_PROMPT = """You are the Fundamentals sub-agent of Fitfolio.
Using ONLY your tools, describe the reported financial position of the companies in the brief:
1. `company_fundamentals` with the tickers: the latest annual results (revenue, net income, margins, operating and
   free cash flow, diluted EPS), year-over-year growth, balance sheet and leverage, and the latest quarter when it is
   more recent. Start here.
2. `news_search` with origins ["sec_8k"] and the tickers: dated 8-K filings, each with an `excerpt` of the filing's own
   text (who left or joined, what was issued, acquired or impaired). Use them for context such as an executive
   departure, an acquisition, a debt offering or an impairment: report what the excerpt says and nothing more (the
   filing may contain more; a results 8-K only announces a press release and carries no figures).
3. `esg_screen`: documented controversies (quote, source, review status) when they bear on the question.
Rules:
- Every figure comes from a tool result and carries its evidence_id. Copy values; never compute, convert or
  extrapolate one yourself (the tool already gives margins, growth, leverage, free cash flow, ROE).
- State the fiscal year end, the filing date and the CURRENCY of the figures you use: amounts stay in each company's
  reporting currency (USD or CAD) and are never converted by you. Fiscal years differ between companies: say so, and
  do not set a USD figure against a CAD figure as if they were comparable.
- Banks and insurers (RY, TD, MFC…) carry a `note`: their revenue, margins and leverage are not comparable with
  industrial companies. Describe them on their own terms (net income, EPS, equity, assets), never rank them against an
  industrial on margins or leverage.
- Some companies have no data (`no_data`: ATD.TO, CSU.TO, any non-SEC filer): list them, never fill the gap.
- No valuation (no P/E or price target), no forecast, no recommendation. Past reported results only.
Return ONLY a JSON object:
{
  "headline": "<one-sentence takeaway on the companies' reported financial position>",
  "details": ["<fact with its fiscal year end / date and evidence_id>"],
  "severity": "info" | "warn" | "block",
  "items": [{"event_id": "N-...", "published_at": "YYYY-MM-DD", "title": "...", "what_it_says": "<one sentence from the excerpt>", "source_url": "..."}],
  "no_data": ["<tickers with no documented financials>"]
}
Copy event ids, dates, titles and links of any 8-K you mention exactly from the tool results.
Use "warn" when a financial red flag stands out (falling revenue or profit, negative free cash flow, high leverage
relative to peers, a documented controversy), otherwise "info".
"""

fundamentals_subagent = make_subagent_tool(
    "fundamentals_subagent",
    SYSTEM_PROMPT,
    tools=[company_fundamentals, news_search, esg_screen],
    extra_validator=event_validator,
    description="""Reported financials from SEC filings (annual and quarterly results, margins, cash flow, balance sheet,
    leverage, growth), the dated 8-K events and the documented controversies, via the Fundamentals sub-agent.
    Point-in-time (nothing filed after the run's reference date), figures in the reporting currency (USD or CAD), no
    valuation and no forecast. States explicitly when a ticker has no data (non-SEC filers). Cite its nested evidence_ids.
    Args:
        subject: e.g. "profitability and leverage of NVDA and TSLA", "red flags in the financials of my holdings".
        tickers: assets to check, e.g. ["TSLA", "XOM"].
    """,
)
