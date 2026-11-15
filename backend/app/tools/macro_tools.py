"""Point-in-time macro & FX tools (box ⑥ "prices · FX · macro · point-in-time").

Pure functions (no Strands, no AWS): used by the AgentCore Gateway Lambda (family "macro") and by
analysis_tools.macro_snapshot (the in-process @tool). Data via curated_store: Parquet on S3 or repo CSV.

Point-in-time rule: only rows dated on or before `as_of` are used. macro_daily_asof is release-aware
(a monthly CPI value appears only from its publication date), so no figure unknown at as_of can leak in.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.tools import curated_store as cs
from app.tools.risk_tools import _eid

LABELS = {
    "V39079": "BoC policy rate (%)", "DFF": "US fed funds (%)",
    "BD.CDN.2YR.DQ.YLD": "CA 2y yield (%)", "BD.CDN.10YR.DQ.YLD": "CA 10y yield (%)",
    "DGS2": "US 2y yield (%)", "DGS10": "US 10y yield (%)",
    "STATIC_TOTALCPICHANGE": "CA CPI y/y (%)", "CPIAUCSL": "US CPI index",
    "UNRATE": "US unemployment (%)",
}


def _envelope(tool: str, inputs: dict, output: dict, say: str, assumptions: list[str], sources: list[str],
              **extra) -> dict:
    return {
        "evidence_id": _eid(tool, inputs), "tool": tool, "inputs": inputs,
        "output": output, "say": say, "currency": "CAD",
        "method": "Deterministic pandas read of data/curated (point-in-time as-of filter).",
        "assumptions": assumptions, "data_sources": sources, **extra,
    }


def macro_snapshot(curated_dir: str | Path, as_of: str | None = None) -> dict:
    """Macro series at `as_of` (or latest): policy rates, 2y/10y yields, CPI, unemployment — CA and US."""
    df = cs.read(curated_dir, "macro_daily_asof")
    if as_of:
        df = df[df["date"] <= pd.Timestamp(as_of)]
        if df.empty:
            raise ValueError(f"No macro data on or before {as_of}.")
    row = df.sort_values("date").iloc[-1]
    out = {"date": str(row["date"].date())}
    for col in df.columns[1:]:
        v = row[col]
        if not pd.isna(v):
            out[LABELS.get(col, col)] = float(v)
    say = (f"As of {out['date']}: CA policy rate {out.get('BoC policy rate (%)', 'n/a')}%, "
           f"CA 10y {out.get('CA 10y yield (%)', 'n/a')}%, US 10y {out.get('US 10y yield (%)', 'n/a')}%, "
           f"CA CPI {out.get('CA CPI y/y (%)', 'n/a')}%.")
    return _envelope("macro_snapshot", {"as_of": as_of}, out, say,
                     ["Monthly figures (inflation, unemployment) are only used once they were published.",
                      "Only data available on the analysis date is used."],
                     ["macro_rates.csv (Bank of Canada Valet API; FRED) via "
                      "curated/macro_daily_asof.csv (release-aware alignment)"],
                     as_of=out["date"])


def fx_usdcad(curated_dir: str | Path, as_of: str | None = None, start: str | None = None) -> dict:
    """USD/CAD (Bank of Canada) on `as_of` (or latest), plus its change since `start` when given."""
    df = cs.read(curated_dir, "fx_usdcad_filled").sort_values("date")
    if as_of:
        df = df[df["date"] <= pd.Timestamp(as_of)]
    if df.empty:
        raise ValueError(f"No USD/CAD rate on or before {as_of}.")
    last = df.iloc[-1]
    out = {"date": str(last["date"].date()), "usdcad": float(last["usdcad"]),
           "filled": bool(str(last.get("filled", False)).lower() == "true")}
    say = f"USD/CAD on {out['date']}: {out['usdcad']:.4f} (1 USD = {out['usdcad']:.4f} CAD)."
    if start:
        first = df[df["date"] >= pd.Timestamp(start)]
        if first.empty:
            raise ValueError(f"No USD/CAD rate between {start} and {as_of}.")
        f0 = first.iloc[0]
        chg = out["usdcad"] / float(f0["usdcad"]) - 1
        out.update({"start_date": str(f0["date"].date()), "start_usdcad": float(f0["usdcad"]),
                    "change": round(chg, 6)})
        say += (f" Since {out['start_date']}: {chg * 100:+.1f}% "
                f"({'USD stronger' if chg > 0 else 'CAD stronger'}).")
    return _envelope("fx_usdcad", {"as_of": as_of, "start": start}, out, say,
                     ["Bank of Canada daily rate; on a holiday, the previous business day's rate is used.",
                      "Only data available on the analysis date is used."],
                     ["fx_usdcad_daily.csv (Bank of Canada, series FXUSDCAD) via curated/fx_usdcad_filled.csv"],
                     as_of=out["date"], period=f"{df['date'].min().date()}/{out['date']}")
