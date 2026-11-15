"""vol_forecast tool: is a holding's risk (volatility) rising, stable or easing? Never a return forecast.

Reads data/curated/vol_forecast.csv (GARCH job, sagemaker/garch_job) and compares each ticker's 21-day GARCH
forecast with its last-63-day historical volatility:
    forecast / hist > risk_trend.rising_ratio  -> "rising"
    forecast / hist < risk_trend.easing_ratio  -> "easing"
    otherwise                                  -> "stable"
(ratios in backend/app/core/projection_assumptions.yaml).

Point-in-time: the forecast file is used only if its as_of is on or before the requested as_of and at most
max_forecast_age_days older. Otherwise the tool returns the historical volatility up to as_of, with no trend
and the reason. Returns an evidence envelope like every tool (evidence_id, say, assumptions).
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from app.quant.assumptions import load_assumptions
from app.quant.garch import HIST_WINDOW, TRADING_DAYS
from app.quant.projection import MarketInputs, cached_inputs, garch_status
from app.tools.risk_tools import _eid

TOOL = "vol_forecast"


def trend_of(forecast: float | None, hist: float | None, cfg: dict | None = None) -> str | None:
    if forecast is None or hist is None or not (hist > 0) or not math.isfinite(forecast):
        return None
    rt = (cfg or load_assumptions())["risk_trend"]
    ratio = forecast / hist
    return "rising" if ratio > rt["rising_ratio"] else "easing" if ratio < rt["easing_ratio"] else "stable"


def _hist(inp: MarketInputs, t: str, as_of: pd.Timestamp) -> float | None:
    r = inp.returns.loc[inp.returns.index <= as_of, t].dropna().tail(HIST_WINDOW)
    return round(float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)), 6) if len(r) >= 2 else None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 6) if math.isfinite(f) else None


def vol_forecast(tickers: list[str], as_of: str, inputs: MarketInputs | None = None,
                 curated_dir: str | Path | None = None) -> dict:
    """Per ticker: forecast_vol, hist_vol_63d, trend (rising | stable | easing), model, job_name."""
    if not tickers:
        raise ValueError("tickers must not be empty")
    cfg = load_assumptions()
    inp = inputs or cached_inputs(curated_dir)
    as_of_ts = pd.Timestamp(as_of)
    tickers = sorted(dict.fromkeys(tickers))
    unknown = [t for t in tickers if t not in inp.returns.columns]
    if unknown:
        raise KeyError(f"unknown tickers: {unknown}")
    usable, why = garch_status(inp, as_of_ts, int(cfg["max_forecast_age_days"]))
    vf = inp.vol_forecast.set_index("ticker") if usable else None
    job = inp.manifest.get("job_name") if usable else None
    rows = {}
    for t in tickers:
        if vf is not None and t in vf.index:
            f, h = _num(vf.at[t, "forecast_vol"]), _num(vf.at[t, "hist_vol_63d_ann"])
            rows[t] = {"forecast_vol": f, "hist_vol_63d": h, "trend": trend_of(f, h, cfg),
                       "ratio": round(f / h, 4) if f and h else None, "model": str(vf.at[t, "model"]),
                       "job_name": str(vf.at[t, "job_name"]) if "job_name" in vf.columns else job,
                       "forecast_as_of": str(pd.Timestamp(vf.at[t, "as_of"]).date())}
        else:
            rows[t] = {"forecast_vol": None, "hist_vol_63d": _hist(inp, t, as_of_ts), "trend": None,
                       "ratio": None, "model": "historical", "job_name": None,
                       "reason": why if vf is None else "no GARCH forecast for this ticker"}
    out = {"tickers": rows, "source": "garch" if vf is not None else "historical", "source_reason": why,
           "thresholds": dict(cfg["risk_trend"])}
    parts = []
    for t, r in rows.items():
        if r["trend"]:
            parts.append(f"{t} {r['trend']} (GARCH forecast {r['forecast_vol'] * 100:.1f}% vs last 3 months "
                         f"{r['hist_vol_63d'] * 100:.1f}%)")
        elif r["hist_vol_63d"] is not None:
            parts.append(f"{t} last 3 months {r['hist_vol_63d'] * 100:.1f}% (no forecast)")
    say = "Annualized volatility, a risk measure, not a return: " + "; ".join(parts) + "."
    inputs_ = {"tickers": tickers, "as_of": str(as_of_ts.date())}
    return {"evidence_id": _eid(TOOL, inputs_), "tool": TOOL, "inputs": inputs_, "output": out, "say": say,
            "currency": "CAD", "as_of": str(as_of_ts.date()),
            "method": "GARCH(1,1)-t 21-day volatility forecast (SageMaker job) vs std of the last 63 daily returns "
                      "× sqrt(252); trend from the ratio thresholds in projection_assumptions.yaml.",
            "assumptions": ["Volatility forecasts describe risk, not the direction or size of returns.",
                            "252 trading days a year for every asset, crypto included (common calendar).",
                            (f"Trend thresholds: rising above ×{cfg['risk_trend']['rising_ratio']}, easing below "
                             f"×{cfg['risk_trend']['easing_ratio']} (team assumptions).")],
            "data_sources": ["data/curated/vol_forecast.csv (GARCH job)", "data/curated/returns_daily_cad.csv"],
            **({"garch_job": {"job_name": job, "as_of": out["tickers"][tickers[0]].get("forecast_as_of")}}
               if vf is not None else {})}


def holding_risk_input(ticker: str, as_of: str, inputs: MarketInputs | None = None) -> dict:
    """INTEGRATION POINT for tools/holding_status.py (not in the repo yet): the volatility a holding's risk_level
    should use, with risk_source "garch_forecast" when a usable forecast exists, else "historical", and the
    risk_trend. Same risk-level thresholds as before (rules.yaml) apply to `vol`."""
    r = vol_forecast([ticker], as_of, inputs=inputs)["output"]["tickers"][ticker]
    if r["forecast_vol"] is not None:
        return {"vol": r["forecast_vol"], "risk_source": "garch_forecast", "risk_trend": r["trend"]}
    return {"vol": r["hist_vol_63d"], "risk_source": "historical", "risk_trend": None}
