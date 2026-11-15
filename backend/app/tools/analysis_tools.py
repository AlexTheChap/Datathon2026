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
from app.tools.risk_tools import _eid

_LABELS = {
    "V39079": "BoC policy rate (%)", "DFF": "US fed funds (%)",
    "BD.CDN.2YR.DQ.YLD": "CA 2y yield (%)", "BD.CDN.10YR.DQ.YLD": "CA 10y yield (%)",
    "DGS2": "US 2y yield (%)", "DGS10": "US 10y yield (%)",
    "STATIC_TOTALCPICHANGE": "CA CPI y/y (%)", "CPIAUCSL": "US CPI index",
    "UNRATE": "US unemployment (%)",
}


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
        as_of: Optional "YYYY-MM-DD" reference date.
    """
    df = pd.read_csv(_curated() / "macro_daily_asof.csv", parse_dates=["date"])
    if as_of:
        df = df[df["date"] <= pd.Timestamp(as_of)]
        if df.empty:
            raise ValueError(f"No macro data on or before {as_of}.")
    row = df.sort_values("date").iloc[-1]
    out = {"date": str(row["date"].date())}
    skipped = []
    for col in df.columns[1:]:
        v = row[col]
        if pd.isna(v):
            skipped.append(col)
        else:
            out[_LABELS.get(col, col)] = float(v)
    say = (f"As of {out['date']}: CA policy rate {out.get('BoC policy rate (%)', 'n/a')}%, "
           f"CA 10y {out.get('CA 10y yield (%)', 'n/a')}%, US 10y {out.get('US 10y yield (%)', 'n/a')}%, "
           f"CA CPI {out.get('CA CPI y/y (%)', 'n/a')}%.")
    return _envelope("macro_snapshot", {"as_of": as_of}, out, say,
                     ["Monthly series (CPI, unemployment) are dated to the reference "
                      "month and only appear on/after their release date.",
                      "No data after the as_of date is used."],
                     ["macro_rates.csv (Bank of Canada Valet API; FRED) via "
                      "curated/macro_daily_asof.csv (release-aware alignment)"])


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
def esg_screen(tickers: list[str]) -> dict:
    """ESG-index membership and documented controversies for the given tickers.
    Membership: is the stock inside the reference ESG index (preference signal,
    never a ban). Controversies: extracted from filings/NBIM with severity and
    review status — only status='verified' lines may drive BLOCKs.

    Args:
        tickers: Assets from the universe (e.g. ["XOM", "CNQ.TO"]).
    """
    tset = set(tickers)

    screen = pd.read_csv(_curated() / "esg_index_screen.csv")
    screen = screen[(screen["in_universe"].astype(str).str.lower() == "true")
                    & screen["ticker"].isin(tset)]
    membership = {
        r.ticker: {"esg_index": r.esg_fund, "in_esg_index": str(r.in_esg_fund).lower() == "true",
                   "status": r.status, "wording": r.wording or None}
        for r in screen.itertuples()
    }

    cont = pd.read_csv(_curated() / "controversies.csv", dtype=str).fillna("")
    cont = cont[cont["ticker"].isin(tset)]
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
    return _envelope("esg_screen", {"tickers": tickers}, out, say,
                     ["Index membership does not explain WHY a stock is excluded "
                      "(reason not published); it is a discussion signal, not a verdict.",
                      "Controversy rows with status != 'verified' are unreviewed "
                      "extracts: absence of evidence is not evidence of absence."],
                     ["curated/esg_index_screen.csv (State Street/iShares holdings vs "
                      "Vanguard ESGV FTSE screen)",
                      "curated/controversies.csv (10-K extracts via SEC EDGAR; NBIM "
                      "Council on Ethics recommendations)"])
