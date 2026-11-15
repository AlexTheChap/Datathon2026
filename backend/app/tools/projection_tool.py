"""projection tool: wraps app.quant.projection.project in an evidence envelope (one evidence record per call).

A RANGE of possible outcomes under the FP Canada / Institute of Financial Planning 2026 Projection Assumption
Guidelines, not a forecast. The evidence carries the assumptions block and, when GARCH volatility was used, the
SageMaker job name and the forecast's as_of, so every number traces back to the job (vol_forecast.csv,
garch_manifest.json).
"""
from __future__ import annotations

from pathlib import Path

from app.quant import projection as _p
from app.tools.risk_tools import _eid

TOOL = "projection"
DEFAULT_PATHS = 10_000


def horizon_from_profile(horizon: dict | list | None) -> tuple[int | None, str]:
    """Profile horizon -> years: the upper bound of a range ([3, 5] -> 5), the lower bound when open-ended
    (15+ -> 15). Returns (years, plain note)."""
    if isinstance(horizon, dict):
        lo, hi = horizon.get("min"), horizon.get("max")
    elif isinstance(horizon, (list, tuple)) and horizon:
        lo, hi = (list(horizon) + [None])[:2]
    else:
        return None, "no horizon in the profile"
    if hi:
        return int(hi), (f"profile horizon {lo}–{hi} years: the upper bound ({hi}) is used" if lo and lo != hi
                         else f"profile horizon {hi} years")
    if lo:
        return int(lo), f"profile horizon {lo}+ years: {lo} years is used"
    return None, "no horizon in the profile"


def _money(x: float) -> str:
    return f"${x:,.0f}"


def run_projection(weights: dict[str, float], start_value: float, horizon_years: int, as_of: str,
                   monthly_contribution: float = 0.0, annual_fee: float = 0.0, n_paths: int = DEFAULT_PATHS,
                   seed: int = 42, vol_source: str = "auto", risk_band: str | None = None,
                   horizon_note: str | None = None, inputs: _p.MarketInputs | None = None,
                   curated_dir: str | Path | None = None) -> dict:
    """Evidence envelope around project(). Raises ValueError / KeyError on bad inputs (callers map them)."""
    n_paths = min(int(n_paths), _p.MAX_PATHS)
    res = _p.project(weights, start_value, horizon_years, as_of, vol_source=vol_source, n_paths=n_paths, seed=seed,
                     monthly_contribution=monthly_contribution, annual_fee=annual_fee, risk_band=risk_band,
                     inputs=inputs, curated_dir=curated_dir)
    total = sum(v for v in weights.values() if v and v > 0)
    inp = {"weights": {k: round(v / total, 6) for k, v in sorted(weights.items()) if v and v > 0},
           "start_value": float(start_value), "horizon_years": int(horizon_years), "as_of": res["as_of"],
           "monthly_contribution": float(monthly_contribution), "annual_fee": float(annual_fee),
           "n_paths": n_paths, "seed": int(seed), "vol_source": vol_source}
    f, h = res["final"], res["horizon_years"]
    say = (f"In {h} years, under the 2026 planning guidelines, the simulated portfolio ended between "
           f"{_money(f['nominal']['p10'])} and {_money(f['nominal']['p90'])} in 8 of 10 scenarios (median "
           f"{_money(f['nominal']['p50'])}); in {f['prob_below_start_nominal'] * 100:.0f}% of scenarios it was worth "
           f"less than the starting {_money(res['start_value'])}. After inflation: {_money(f['real']['p10'])} to "
           f"{_money(f['real']['p90'])}. A simulation, not a forecast.")
    vs = res["assumptions"]["vol_source"]
    garch_job = ({"job_name": vs.get("job_name"), "as_of": vs.get("forecast_as_of")} if vs["used"] == "garch" else None)
    assumptions = [            # warnings first: the Explainer sees the first four
        *res["warnings"],
        (f"Expected returns: {res['assumptions']['source']} ({res['assumptions']['source_url']}); never "
         "estimated from past prices."),
        "Rates per holding: " + ", ".join(f"{x['ticker']} {x['category']} {x['rate'] * 100:.1f}%"
                                          + (" (team assumption)" if x["team_assumption"] else "")
                                          for x in res["assumptions"]["per_ticker"]) + ".",
        f"Inflation {res['assumptions']['inflation'] * 100:.1f}%, fees {annual_fee * 100:.2f}% a year.",
        f"Volatility: {vs['used']} ({vs['reason']}).",
        "Monthly steps, rebalanced monthly to the target weights; the median path compounds at each rate.",
        "Individual stocks get their region's equity rate (asset-class rates) and their own volatility.",
    ]
    if horizon_note:
        assumptions.append(f"Horizon: {horizon_note}.")
    out = {k: res[k] for k in ("horizon_years", "start_value", "monthly_contribution", "total_contributed",
                               "headline", "final", "percentiles_by_year", "drawdown", "assumptions", "warnings",
                               "disclaimer")}
    if horizon_note:
        out["horizon_note"] = horizon_note
    return {"evidence_id": _eid(TOOL, inp), "tool": TOOL, "inputs": inp, "output": out, "say": say,
            "currency": "CAD", "as_of": res["as_of"],
            "method": f"Monte Carlo, {n_paths} paths (seed {seed}), monthly multivariate lognormal returns under "
                      "the planning-guideline rates; percentiles only.",
            "assumptions": assumptions,
            "data_sources": ["backend/app/core/projection_assumptions.yaml", "data/curated/correlation_full.csv",
                             "data/curated/asset_risk_metrics.csv", "data/curated/vol_forecast.csv (when used)"],
            **({"garch_job": garch_job} if garch_job else {})}
