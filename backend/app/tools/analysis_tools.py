"""Domain analysis tools (node ⑥) beyond price-based risk: macro, rates, ESG.

Same contract as risk_tools.py: deterministic pandas reads over data/curated/,
each call returns a standard evidence envelope (evidence_id, inputs, output,
assumptions, data_sources). The LLM cites these; it never computes.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from strands import tool

from app.core.config import get_settings
from app.tools.context import effective_as_of
from app.tools.risk_tools import _eid

def _curated() -> Path:
    s = get_settings()
    return s.abs_path(s.curated_dir)


def _envelope(tool: str, inputs: dict, output: dict, say: str,
              assumptions: list[str], sources: list[str]) -> dict:
    return {
        "evidence_id": _eid(tool, inputs), "tool": tool, "inputs": inputs,
        "output": output, "say": say, "currency": "CAD",
        "method": "Deterministic pandas read of data/curated (point-in-time as-of filter).",
        "assumptions": assumptions, "data_sources": sources,
    }


@tool
def macro_snapshot(as_of: str | None = None) -> dict:
    """Macro series at `as_of` (or latest): policy rates, 2y/10y yields, CPI,
    unemployment — Canada and US. Release-aware table; no data after as_of.

    Args:
        as_of: Optional earlier "YYYY-MM-DD" date (the run's reference date is the upper bound).
    """
    # Same logic as the Gateway's macro___macro_snapshot (one implementation: app.tools.macro_tools),
    # with the date bounded by the run's reference date: never later than as_of.
    from app.tools import macro_tools

    return macro_tools.macro_snapshot(_curated(), effective_as_of(as_of))


@tool
def rate_sensitivity(tickers: list[str]) -> dict:
    """Empirical rate sensitivity (duration) per asset: regression beta of returns
    on the referenced yield series, vs the official duration.

    Args:
        tickers: Assets from the universe (e.g. ["XBB.TO", "TLT"]).
    """
    df = pd.read_csv(_curated() / "rate_sensitivity.csv")
    df = df[df["ticker"].isin(tickers)]
    if df.empty:
        raise KeyError(f"No rate-sensitivity rows for {tickers}.")
    out = {
        r.ticker: {"yield_series": r.yield_series, "empirical_duration": float(r.empirical_duration),
                   "official_duration": None if pd.isna(r.official_duration) else float(r.official_duration),
                   "r2": float(r.r2), "n_obs": int(r.n_obs)}
        for r in df.itertuples()
    }
    t0 = next(iter(out))
    say = (f"{t0}: empirical duration {out[t0]['empirical_duration']:.1f} "
           f"(official {out[t0]['official_duration']}) on {out[t0]['yield_series']}.")
    return _envelope("rate_sensitivity", {"tickers": tickers}, out, say,
                     ["Empirical duration = sensitivity of total returns to yield changes "
                      "over 2021-2026; varies over time and with non-parallel shifts."],
                     ["curated/rate_sensitivity.csv (regression over returns_daily_cad.csv "
                      "vs Bank of Canada / FRED yields)"])


@tool
def esg_screen(tickers: list[str], as_of: str | None = None) -> dict:
    """ESG-index membership and documented controversies for the given tickers.
    Membership: is the stock inside the reference ESG index (preference signal,
    never a ban). Controversies: extracted from filings/NBIM with severity and
    review status — only status='verified' lines may drive BLOCKs.

    Locally, the synced market-graph cache serves this when it is fresh (identical tables to
    Neptune's); the curated CSVs below are the fallback when the cache is stale or absent.

    Args:
        tickers: Assets from the universe (e.g. ["XOM", "CNQ.TO"]).
        as_of: Optional earlier "YYYY-MM-DD" date; controversies published after the run's reference
            date are never returned (point-in-time).
    """
    as_of = effective_as_of(as_of)
    from app.tools.market_store import local_market_graph

    s = get_settings()
    g = local_market_graph(_curated(), s.abs_path(s.market_dir), require_tables=True)
    if g is not None:
        from app.market.graph import esg_screen_tool

        env = esg_screen_tool(g, {"tickers": tickers, "as_of": as_of})
        if "error" not in env:          # stale or unreachable cache: curated fallback below
            return env
    return _esg_screen_curated(tickers, as_of)


def _esg_screen_curated(tickers: list[str], as_of: str) -> dict:
    tset = set(tickers)

    screen = pd.read_csv(_curated() / "esg_index_screen.csv")
    screen = screen[(screen["in_universe"].astype(str).str.lower() == "true")
                    & screen["ticker"].isin(tset)]
    membership = {
        r.ticker: {"esg_index": r.esg_fund, "in_esg_index": str(r.in_esg_fund).lower() == "true",
                   "status": r.status, "wording": r.wording if isinstance(r.wording, str) and r.wording else None}  # NaN (empty cell) is truthy
        for r in screen.itertuples()
    }

    cont = pd.read_csv(_curated() / "controversies.csv", dtype=str).fillna("")
    cont = cont[cont["ticker"].isin(tset) & (cont["publication_date"] <= as_of)]
    controversies = [
        {"ticker": r.ticker, "severity": r.severity, "status": r.status,
         "category": r.category, "description": r.description[:220],
         "source_doc": r.source_doc}
        for r in cont.itertuples()
    ]
    unverified = sorted({c["ticker"] for c in controversies if c["status"] != "verified"}
                        - {c["ticker"] for c in controversies if c["status"] == "verified"})
    out = {"membership": membership, "controversies": controversies,
           "unverified_only": unverified}
    excluded = sorted(t for t, m in membership.items() if not m["in_esg_index"])
    say = (f"Outside reference ESG index: {', '.join(excluded) if excluded else 'none'}. "
           f"Controversy lines: {len(controversies)}"
           + (f" (unverified-only for: {', '.join(unverified)})" if unverified else "") + ".")
    return _envelope("esg_screen", {"tickers": tickers, "as_of": as_of}, out, say,
                     [f"Controversies are limited to those published on or before {as_of} (point-in-time).",
                      "Index membership does not explain WHY a stock is excluded "
                      "(reason not published); it is a discussion signal, not a verdict.",
                      "Controversy rows with status != 'verified' are unreviewed "
                      "extracts: absence of evidence is not evidence of absence."],
                     ["curated/esg_index_screen.csv (State Street/iShares holdings vs "
                      "Vanguard ESGV FTSE screen)",
                      "curated/controversies.csv (10-K extracts via SEC EDGAR; NBIM "
                      "Council on Ethics recommendations)"])
