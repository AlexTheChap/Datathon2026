"""rate_exposure: how a portfolio's holdings have reacted to interest rates, next to the current rates.
Deterministic (no LLM, no forecast). It joins two curated reads in one evidence record:
- the current macro snapshot (policy rates, 2y / 10y yields, inflation) as of the run's date;
- the empirical rate sensitivity ("duration") of each holding that has one (curated/rate_sensitivity.csv).
Only the bond funds of the universe have a measured sensitivity; a stock or equity fund is reported as "not
measured", never as zero. The "1-point" figure is an illustration of that historical sensitivity, not a prediction.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.tools import macro_tools
from app.tools.risk_tools import _eid

TOOL = "rate_exposure"
RATE_KEYS = ["BoC policy rate (%)", "CA 2y yield (%)", "CA 10y yield (%)", "US fed funds (%)", "US 10y yield (%)",
             "CA CPI y/y (%)"]


def run_rate_exposure(weights: dict[str, float], as_of: str, curated: Path) -> dict:
    """Raises KeyError for a ticker outside the universe's data, ValueError for empty or negative weights."""
    w = {t: float(v) for t, v in weights.items() if v}
    if not w or any(v < 0 for v in w.values()):
        raise ValueError("Give at least one holding with a positive weight.")
    from app.tools import curated_store as cs

    known = set(cs.read_extended(curated, "assets_universe")["ticker"])   # + S&P 500 extension
    unknown = sorted(set(w) - known)
    if unknown:
        raise KeyError(f"Not in the data universe: {', '.join(unknown)}.")
    total = sum(w.values())
    w = {t: v / total for t, v in w.items()}
    sens = pd.read_csv(curated / "rate_sensitivity.csv").set_index("ticker")
    macro = macro_tools.macro_snapshot(curated, as_of)
    snap = macro["output"]
    rates = {k: snap[k] for k in RATE_KEYS if snap.get(k) is not None}
    holdings = []
    for t, wt in sorted(w.items(), key=lambda kv: -kv[1]):
        if t in sens.index:
            r = sens.loc[t]
            d = float(r["empirical_duration"])
            holdings.append({"ticker": t, "weight": round(wt, 6), "measured": True, "duration": round(d, 2),
                             "official_duration": None if pd.isna(r["official_duration"]) else float(r["official_duration"]),
                             "r2": round(float(r["r2"]), 2), "yield_series": r["yield_series"],
                             "impact_pct_per_point": round(-d, 2)})
        else:
            holdings.append({"ticker": t, "weight": round(wt, 6), "measured": False, "duration": None,
                             "official_duration": None, "r2": None, "yield_series": None, "impact_pct_per_point": None})
    measured = [x for x in holdings if x["measured"]]
    share = sum(x["weight"] for x in measured)
    portfolio = {"measured_share": round(share, 6),
                 "weighted_duration": round(sum(x["weight"] * x["duration"] for x in measured), 3),
                 # Effect on the whole portfolio's value of a 1-point rise in yields, from the measured part only.
                 "impact_pct_per_point": round(-sum(x["weight"] * x["duration"] for x in measured), 2)}
    inputs = {"weights": {t: round(v, 6) for t, v in w.items()}, "as_of": as_of}
    say = ((macro.get("say") or f"As of {macro['as_of']}.") + " "
           + (f"{share:.0%} of the portfolio has a measured rate sensitivity; a 1-point rise in yields would have "
              f"moved it by about {portfolio['impact_pct_per_point']:.1f}% of the portfolio's value, by that history."
              if measured else "None of the holdings has a measured rate sensitivity."))
    return {
        "evidence_id": _eid(TOOL, inputs), "tool": TOOL, "inputs": inputs, "as_of": macro["as_of"], "say": say,
        "output": {"rates": rates, "rates_date": snap.get("date"), "holdings": holdings, "portfolio": portfolio},
        "macro_evidence_id": macro.get("evidence_id"),
        "method": "Current macro series (point-in-time) and the regression of each fund's daily returns on yield "
                  "changes over 2021-2026 (curated/rate_sensitivity.csv). Impact = -duration x 1 point.",
        "assumptions": ["Sensitivity is the 2021-2026 average: it changes over time and with non-parallel curve shifts.",
                        "Only bond funds have a measured sensitivity; stocks and equity funds are not measured, not zero.",
                        "The 1-point figure illustrates past sensitivity. It is not a forecast of rates or returns."],
        "data_sources": ["curated/rate_sensitivity.csv", "curated/macro_daily_asof.csv"],
    }
