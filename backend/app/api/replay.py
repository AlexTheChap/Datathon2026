"""Crisis replay for the dashboard: "what your money would do, day by day, if a year like 2022 happened again".

GET /v1/runs/{id}/replay. Deterministic, no LLM: it replays the SAME portfolios the run's tools tested, with the
same code as the `stress` tool (RiskTools.portfolio_returns over the stress window, daily rebalancing):
  - "idea":     the portfolio of the stress test that included the user's tested asset at its highest weight
                (e.g. 20% NVDA), else the run's worst stress test;
  - "proposal": the weights proposed by the allocation sub-agent, when the run has one.
It is a historical scenario, not a forecast: the response says so, and carries the weights it replayed.
"""
from __future__ import annotations

from typing import Any, Optional

from app.tools.risk_tools import STRESS_WINDOWS


def _body(record: dict) -> Any:
    p = record.get("payload")
    return p[0] if isinstance(p, list) and len(p) == 1 else p


def _tool(record: dict) -> str:
    return str(record.get("tool", "")).split("___")[-1]


def portfolios_from_evidence(records: list[dict]) -> dict[str, dict[str, float]]:
    """The idea and the proposal, as weights, read from the run's evidence ledger."""
    stresses = []
    for r in records:
        if r.get("error") or _tool(r) != "stress":
            continue
        b = _body(r)
        if not isinstance(b, dict):
            continue
        w = (b.get("inputs") or {}).get("weights") or {}
        dd = ((b.get("output") or {}).get("portfolio") or {}).get("max_drawdown")
        if w and dd is not None:
            stresses.append((w, dd))
    out: dict[str, dict[str, float]] = {}
    tested = None
    for r in records:  # the asset whose maximum weight the run searched (max_position)
        if _tool(r) == "max_position" and isinstance(_body(r), dict):
            tested = ((_body(r).get("output") or {}).get("asset")) or tested
    if stresses:
        with_asset = [s for s in stresses if tested and tested in s[0]]
        idea = max(with_asset, key=lambda s: s[0][tested]) if with_asset else min(stresses, key=lambda s: s[1])
        out["idea"] = {k: float(v) for k, v in idea[0].items()}
    for r in records:
        if _tool(r) == "allocation_subagent" and isinstance(_body(r), dict):
            w = (_body(r).get("output") or {}).get("weights")
            if isinstance(w, dict) and w:
                out["proposal"] = {k: float(v) for k, v in w.items()}
    return out


def replay(records: list[dict], capital: Optional[float], rt, window: str = "2022") -> dict:
    """Daily portfolio value over the crisis window for each portfolio found in the evidence."""
    start, end, label = STRESS_WINDOWS[window]
    cap = float(capital or 100_000)
    series = {}
    for name, w in portfolios_from_evidence(records).items():
        try:
            r = rt.portfolio_returns(w, start=start, end=end).iloc[1:]
        except (KeyError, ValueError):
            continue
        value = cap * (1 + r).cumprod()
        dd = value / value.cummax() - 1
        series[name] = {
            "weights": w,
            "dates": [start] + [d.strftime("%Y-%m-%d") for d in value.index],   # day 0 = the capital
            "value": [round(cap, 2)] + [round(float(v), 2) for v in value],
            "end_value": round(float(value.iloc[-1]), 2),
            "max_drawdown": round(float(dd.min()), 6),
            "trough_date": dd.idxmin().strftime("%Y-%m-%d"),
            "trough_value": round(float(value.loc[dd.idxmin()]), 2),
        }
    return {"window": window, "label": label, "capital": cap, "series": series,
            "method": "Historical replay with daily rebalancing (same code as the stress tool).",
            "note": "A past crisis replayed on today's portfolio: a scenario, not a forecast."}
