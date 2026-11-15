"""How the evidence panel names things for a reader (GET /v1/runs/{id}/evidence/catalog).

The tools record their sources as file paths and audit notes ("prices_daily.csv (Yahoo Finance via yfinance, ...)",
"backend/app/core/rules.yaml"): right for the ledger, meaningless to the person reading the answer. This module turns
them into publisher names, gives each tool a plain title, and writes periods as dates people read.

Nothing here changes the ledger or what the Explainer sees: it is presentation only.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Plain title of each tool's card.
TITLES = {
    "metrics": "How much each holding moves", "risk_contribution": "Where the ups and downs come from",
    "correlation": "How the holdings move together", "concentration": "How spread out the money is",
    "optimize": "The allocation search", "projection": "Range of possible values",
    "vol_forecast": "Where the swings are heading", "macro_snapshot": "Interest rates and inflation",
    "fx_usdcad": "Canadian dollar exchange rate", "rate_sensitivity": "How the holdings react to interest rates",
    "esg_screen": "ESG screen", "company_fundamentals": "Company financials",
    "news_search": "News and filings", "search_news": "News and filings",
    "market_screen": "Market screen", "market_related": "Related companies",
    "risk_subagent": "Risk analysis", "macro_subagent": "Rates and economy analysis", "esg_subagent": "ESG analysis",
    "fundamentals_subagent": "Company financials analysis", "news_subagent": "News analysis",
    "allocation_subagent": "Allocation analysis",
    "stress": "The 2022 market drop, replayed", "max_position": "Largest position your loss limit allows",
}

GOVERNANCE_TOOLS = {"governance_check"}

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def day_text(iso: str) -> str:
    """"2026-11-13" → "13 Nov 2026"."""
    m = _DATE.search(iso or "")
    if not m:
        return iso
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    return f"{d} {_MONTHS[mo - 1]} {y}"


def month_text(iso: str) -> str:
    m = _DATE.search(iso or "")
    return f"{_MONTHS[int(m[2]) - 1]} {m[1]}" if m else iso


def period_text(period: Optional[str]) -> Optional[str]:
    """"2021-11-15/2026-11-12" → "Nov 2021 to Nov 2026"; a single date → "13 Nov 2026"."""
    if not period:
        return None
    parts = [p for p in str(period).split("/") if p]
    if len(parts) == 2 and _DATE.search(parts[0]) and _DATE.search(parts[1]):
        return f"{month_text(parts[0])} to {month_text(parts[1])}"
    return day_text(parts[0]) if parts and _DATE.search(parts[0]) else str(period)


# Raw source text (as the tools write it) → the publisher a reader recognises. First match wins; None = not shown
# (an internal step, not a source: derived tables are named after the data they come from).
_YAHOO = "Yahoo Finance daily prices"
_RULES: list[tuple[re.Pattern, Optional[str]]] = [(re.compile(p, re.I), label) for p, label in [
    (r"sub-agent analysis", None),                        # replaced by the sources of the evidence it used
    (r"^data/curated \(prices, holdings", None),          # vague; the specific files are listed elsewhere
    (r"rules\.yaml", "AlphaForge's limits for each risk profile (team assumptions)"),
    (r"projection_assumptions\.yaml", "FP Canada and IQPF Projection Assumption Guidelines 2026"),
    (r"vol_forecast\.csv", "AlphaForge volatility model (GARCH), fitted on daily prices"),
    (r"rate_sensitivity\.csv", "AlphaForge rate sensitivity, measured on daily prices and bond yields"),
    (r"fx_usdcad", "Bank of Canada USD/CAD exchange rate"),
    (r"macro_rates|macro_daily_asof", "Bank of Canada and US Federal Reserve (FRED) rates and inflation"),
    (r"Bank of Canada / Federal Reserve rate decisions", "Bank of Canada and Federal Reserve rate decisions"),
    (r"esg_index_screen", "Vanguard ESGV fund holdings (FTSE US All Cap Choice index), SEC filings"),
    (r"controvers", "Company annual reports (10-K), reviewed controversy list"),
    (r"etf_holdings\.csv \(SPY", "S&P 500 member list (SPY holdings, State Street)"),
    (r"etf_holdings", "Fund holdings (State Street, iShares Canada, SEC N-PORT filings)"),
    (r"fundamentals\.csv|SEC EDGAR XBRL", "SEC EDGAR company financial filings (10-K, 10-Q)"),
    (r"SEC EDGAR", "SEC EDGAR filings (8-K, 10-K, 40-F)"),
    (r"assets_universe", None),
    (r"prices_daily|returns_daily|correlation_full|asset_risk_metrics|Yahoo Finance", _YAHOO),
    (r"market graph", None),                               # its underlying files are named in the same list
]]
_EXTRACTED = re.compile(r"extracted (\d{4}-\d{2}-\d{2})")


def source_names(raw: Iterable[str]) -> list[str]:
    """Readable, de-duplicated source names, in first-seen order. Unknown text is kept as is (never hidden)."""
    out: list[str] = []
    for text in map(str, raw):
        label: Optional[str] = text
        for pattern, name in _RULES:
            if pattern.search(text):
                label = name
                break
        if not label:
            continue
        if label == _YAHOO:  # one Yahoo line, dated when any of the texts gives the download date
            m = _EXTRACTED.search(text)
            dated = f"{_YAHOO} (downloaded {day_text(m[1])})" if m else None
            i = next((n for n, o in enumerate(out) if o.startswith(_YAHOO)), None)
            if i is None:
                out.append(dated or _YAHOO)
            elif dated:
                out[i] = dated
        elif label not in out:
            out.append(label)
    return out


def esg_screen_sources(raw: Iterable[str]) -> list[str]:
    """The market graph names its files inside one string: split it so each one maps to its publisher."""
    pieces: list[str] = []
    for text in raw:
        found = re.findall(r"curated/[a-z_]+\.csv", str(text))
        pieces.extend(found or [str(text)])
    return source_names(pieces)


def mix_text(weights: Optional[dict]) -> Optional[str]:
    """"ADM 60% · BTC-USD 40%": names the mix a card is about when the answer compares several."""
    if not weights:
        return None
    items = sorted(((k, v) for k, v in weights.items() if v and v > 0), key=lambda kv: -kv[1])
    if not items:
        return None
    total = sum(v for _, v in items) or 1.0
    shown = [f"{k} {round(100 * v / total)}%" for k, v in items[:5]]
    more = f" and {len(items) - 5} more" if len(items) > 5 else ""
    return " · ".join(shown) + more

