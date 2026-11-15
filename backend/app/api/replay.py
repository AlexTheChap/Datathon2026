"""Historical replay for the dashboard: "what your money would do, day by day, over a chosen period".

GET /v1/runs/{id}/replay. Deterministic, no LLM: it replays the portfolios found in the run's evidence
with RiskTools.portfolio_returns (daily rebalancing) over the requested start/end dates:
  - "proposal": the weights proposed by the allocation sub-agent, when the run has one;
  - "idea":     portfolios previously tested by stress-style tools, when such records exist.
It is a historical scenario, not a forecast: the response says so, and carries the weights it replayed.
"""
from __future__ import annotations

from typing import Any, Optional


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


def replay(records: list[dict], capital: Optional[float], rt, start: str, end: str) -> dict:
    """Daily portfolio value over [start, end] for each portfolio found in the evidence."""
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
    return {"start": start, "end": end, "capital": cap, "series": series,
            "method": "Historical replay with daily rebalancing.",
            "note": "A past period replayed on today's portfolio: a scenario, not a forecast."}
