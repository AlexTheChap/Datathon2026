"""
Builds the curated dataset from the AlphaForge starter dataset.

Usage:
    python scripts/build_curated.py --starter data/raw --out data/curated

Outputs (CSV, in --out):
    calendar.csv              common calendar + per-market holiday flags
    fx_usdcad_filled.csv      USD/CAD carried forward on days without a rate (Canadian holidays)
    prices_cad.csv            adj_close converted to CAD, wide format (one column per ticker)
    prices_local.csv          adj_close in listing currency, wide format
    filled_flags.csv          True when the price was carried forward (no quote that day)
    returns_daily_cad.csv     daily simple returns in CAD (total return)
    returns_daily_local.csv   daily simple returns in listing currency
    macro_pit.csv             macro in long format + available_from column (point-in-time)
    macro_daily_asof.csv      macro in wide format, each value placed at its availability date
    asset_risk_metrics.csv    per-asset risk metrics (full period and 2022)
    correlation_full.csv      correlations of daily CAD returns, full period
    correlation_2022.csv      2022-only correlations
    rate_sensitivity.csv      empirical rate sensitivity vs official duration
    quality_report.json       quality checks + processing decisions

All processing decisions are described in docs/README_extended.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.market.metrics import asset_metrics  # noqa: E402  (shared with the market refresh)


# HYPOTHETICAL release lags (to be replaced by the real dates: ALFRED / StatCan).
# Used to avoid look-ahead bias in simulations.
RELEASE_LAG_DAYS = {
    "CPIAUCSL": 15,                 # US CPI: published around mid-next-month
    "UNRATE": 7,                    # US unemployment: first Friday of next month
    "STATIC_TOTALCPICHANGE": 21,    # Canadian CPI: ~3rd week of next month
}
DAILY_SERIES_LAG_BDAYS = 1          # daily series: usable the next business day


def load_starter(starter: Path):
    px = pd.read_csv(starter / "prices_daily.csv", parse_dates=["date"])
    fx = pd.read_csv(starter / "fx_usdcad_daily.csv", parse_dates=["date"])
    macro = pd.read_csv(starter / "macro_rates.csv", parse_dates=["date"])
    uni = pd.read_csv(starter / "assets_universe.csv")
    actions = pd.read_csv(starter / "corporate_actions.csv", parse_dates=["ex_date"])
    return px, fx, macro, uni, actions


def build_calendar(px: pd.DataFrame, uni: pd.DataFrame) -> pd.DataFrame:
    """Calendar = days when at least one exchange (TSX or US) quoted at least one security.
    Cryptos (7 days/week) are sampled on this calendar: the weekend return is
    included in Monday's."""
    market = uni.set_index("ticker")["market"]
    px = px.assign(market=px["ticker"].map(market))
    eq = px[~px["market"].str.contains("Crypto", na=False)]
    tsx_days = set(eq.loc[eq["market"] == "TSX", "date"])
    us_days = set(eq.loc[eq["market"] != "TSX", "date"])
    days = sorted(tsx_days | us_days)
    cal = pd.DataFrame({"date": days})
    cal["tsx_open"] = cal["date"].isin(tsx_days)
    cal["us_open"] = cal["date"].isin(us_days)
    return cal


def build_prices(px, fx, uni, cal):
    dates = pd.DatetimeIndex(cal["date"])
    local = px.pivot(index="date", columns="ticker", values="adj_close")
    raw_on_cal = local.reindex(dates)
    filled_flags = raw_on_cal.isna()
    # Carry forward the last known price (market-specific holiday). No filling
    # before the first quote.
    local_f = raw_on_cal.ffill()
    filled_flags = filled_flags & local_f.notna()

    fx_s = fx.set_index("date")["usdcad"].reindex(dates)
    fx_filled_flag = fx_s.isna()
    fx_s = fx_s.ffill().bfill()
    fx_out = pd.DataFrame({"date": dates, "usdcad": fx_s.values,
                           "filled": fx_filled_flag.values})

    ccy = uni.set_index("ticker")["currency"]
    usd_cols = [c for c in local_f.columns if ccy.get(c) == "USD"]
    cad = local_f.copy()
    cad[usd_cols] = cad[usd_cols].mul(fx_s, axis=0)
    return local_f, cad, filled_flags, fx_out


def build_macro_pit(macro: pd.DataFrame) -> pd.DataFrame:
    m = macro.copy()
    avail, method = [], []
    for _, r in m.iterrows():
        sid = r["series_id"]
        if sid in RELEASE_LAG_DAYS:
            # reference date = 1st of the month -> month end + lag
            month_end = r["date"] + pd.offsets.MonthEnd(0)
            avail.append(month_end + pd.Timedelta(days=RELEASE_LAG_DAYS[sid]))
            method.append(f"assumed_lag_{RELEASE_LAG_DAYS[sid]}d_after_month_end")
        else:
            avail.append(r["date"] + pd.offsets.BDay(DAILY_SERIES_LAG_BDAYS))
            method.append(f"next_business_day_(+{DAILY_SERIES_LAG_BDAYS})")
    m["available_from"] = avail
    m["available_from_method"] = method
    m["note"] = np.where(m["source"].str.contains("FRED"),
                         "Revised FRED value (not the first release)", "")
    return m


def macro_daily_asof(m: pd.DataFrame, cal: pd.DataFrame) -> pd.DataFrame:
    """For each calendar date: the latest value AVAILABLE on that date."""
    dates = pd.DatetimeIndex(cal["date"])
    out = pd.DataFrame(index=dates)
    for sid, g in m.groupby("series_id"):
        s = g.sort_values("available_from").drop_duplicates("available_from", keep="last")
        s = s.set_index("available_from")["value"]
        out[sid] = s.reindex(s.index.union(dates)).ffill().reindex(dates)
    out.index.name = "date"
    return out


# ---------- risk metrics ----------

def rate_sensitivity(rets_local: pd.DataFrame, macro_wide: pd.DataFrame,
                     durations: pd.DataFrame | None) -> pd.DataFrame:
    """Regression of returns (local currency) on the change in the same country's
    10-year yield. -beta*100 ≈ empirical duration (return for +1 point of yield)."""
    pairs = {"XBB.TO": "BD.CDN.10YR.DQ.YLD", "ZST.TO": "BD.CDN.2YR.DQ.YLD",
             "AGG": "DGS10", "TLT": "DGS10"}
    rows = []
    for t, sid in pairs.items():
        if t not in rets_local or sid not in macro_wide:
            continue
        dy = macro_wide[sid].diff() / 100.0          # percentage points -> fraction
        j = pd.concat([rets_local[t], dy], axis=1).dropna()
        j = j[j.iloc[:, 1] != 0]
        x, y = j.iloc[:, 1], j.iloc[:, 0]
        beta = np.cov(y, x)[0, 1] / np.var(x, ddof=1)
        r2 = np.corrcoef(y, x)[0, 1] ** 2
        off = None
        if durations is not None and t in set(durations["ticker"]):
            off = float(durations.set_index("ticker").loc[t, "duration_years"])
        rows.append({"ticker": t, "yield_series": sid, "n_obs": len(j),
                     "empirical_duration": -beta, "r2": r2,
                     "official_duration": off,
                     "note": "Current official duration (2026-11) vs average 2021-2026 sensitivity: "
                             "gap expected (duration varies over time, non-parallel curve shifts)."})
    return pd.DataFrame(rows)


def quality_report(px, cal, filled, fx_out, macro_pit, cnq_check) -> dict:
    dup = int(px.duplicated(["date", "ticker"]).sum())
    zero_vol = px[(px["volume"] == 0) & (~px["ticker"].str.contains("-USD"))][["date", "ticker"]]
    return {
        "generated_from": "data/raw (2021-11-12 → 2026-11-12)",
        "calendar_days": int(len(cal)),
        "duplicates_date_ticker": dup,
        "zero_volume_rows_non_crypto": zero_vol.astype(str).to_dict("records"),
        "filled_price_days_by_ticker": {k: int(v) for k, v in filled.sum().items()},
        "fx_filled_days": int(fx_out["filled"].sum()),
        "macro_series": sorted(macro_pit["series_id"].unique().tolist()),
        "decisions": {
            "calendar": "Union of TSX and US trading days (weekdays). Crypto sampled "
                        "on this calendar: the weekend return is included on Monday.",
            "price_fill": "Last known price carried forward when a market is closed (country-specific holiday). "
                          "Return = 0 that day. Flag in filled_flags.csv.",
            "fx": "USD/CAD from the Bank of Canada, carried forward on Canadian holidays.",
            "returns": "Simple returns on adj_close (total return, dividends reinvested), in CAD.",
            "cnq_2022_08_22": cnq_check,
            "macro_pit": "available_from = estimated availability date. Monthly: hypothetical lag "
                         f"{RELEASE_LAG_DAYS}. Daily: next business day. To be replaced by the "
                         "real release dates (ALFRED, StatCan calendar).",
            "survivorship": "Universe chosen in 2026: historical returns are biased upward.",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--starter", default="data/raw")
    ap.add_argument("--external", default="data/external")
    ap.add_argument("--out", default="data/curated")
    a = ap.parse_args()
    starter, out = Path(a.starter), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    px, fx, macro, uni, actions = load_starter(starter)
    assert not px.duplicated(["date", "ticker"]).any(), "duplicate date/ticker rows"

    cal = build_calendar(px, uni)
    local, cad, filled, fx_out = build_prices(px, fx, uni, cal)
    r_cad = cad.pct_change(fill_method=None).iloc[1:]
    r_loc = local.pct_change(fill_method=None).iloc[1:]

    m_pit = build_macro_pit(macro)
    m_wide_obs = macro.pivot_table(index="date", columns="series_id", values="value")
    m_asof = macro_daily_asof(m_pit, cal)

    # CNQ: adj_close vs close return on 2022-08-22
    c = px[px.ticker == "CNQ.TO"].set_index("date")
    d0, d1 = pd.Timestamp("2022-08-19"), pd.Timestamp("2022-08-22")
    cnq = {
        "close_return": float(c.loc[d1, "close"] / c.loc[d0, "close"] - 1),
        "adj_close_return": float(c.loc[d1, "adj_close"] / c.loc[d0, "adj_close"] - 1),
        "decision": "adj_close kept: the gap (~2%) is consistent with a special "
                    "distribution treated as a reinvested dividend (total return). The "
                    "1.021 factor in corporate_actions.csv is NOT reapplied. To be confirmed with "
                    "CNQ investor relations.",
    }

    bench = {"spy": "SPY", "xiu": "XIU.TO"}
    met_full = asset_metrics(r_cad, bench, "2021-11-12_2026-11-12")
    met_2022 = asset_metrics(r_cad.loc["2022-01-01":"2022-12-31"], bench, "2022")
    metrics = pd.concat([met_full, met_2022], ignore_index=True)
    metrics.insert(2, "currency", "CAD")
    meta = uni.set_index("ticker")[["name", "asset_class", "sector", "currency"]] \
              .rename(columns={"currency": "listing_currency"})
    metrics = metrics.merge(meta, left_on="ticker", right_index=True, how="left")

    durations = None
    dp = Path(a.external) / "etf_bond_duration.csv"
    if dp.exists():
        durations = pd.read_csv(dp)
    rs = rate_sensitivity(r_loc, m_wide_obs.reindex(r_loc.index.union(m_wide_obs.index)).ffill()
                          .reindex(r_loc.index), durations)

    # ----- writing -----
    uni.to_csv(out / "assets_universe.csv", index=False)
    cal.to_csv(out / "calendar.csv", index=False)
    fx_out.to_csv(out / "fx_usdcad_filled.csv", index=False)
    cad.round(6).to_csv(out / "prices_cad.csv", index_label="date")
    local.round(6).to_csv(out / "prices_local.csv", index_label="date")
    filled.to_csv(out / "filled_flags.csv", index_label="date")
    r_cad.round(8).to_csv(out / "returns_daily_cad.csv", index_label="date")
    r_loc.round(8).to_csv(out / "returns_daily_local.csv", index_label="date")
    m_pit.to_csv(out / "macro_pit.csv", index=False)
    m_asof.to_csv(out / "macro_daily_asof.csv", index_label="date")
    metrics.round(6).to_csv(out / "asset_risk_metrics.csv", index=False)
    r_cad.corr().round(4).to_csv(out / "correlation_full.csv", index_label="ticker")
    r_cad.loc["2022"].corr().round(4).to_csv(out / "correlation_2022.csv", index_label="ticker")
    rs.round(4).to_csv(out / "rate_sensitivity.csv", index=False)
    with open(out / "quality_report.json", "w", encoding="utf-8") as f:
        json.dump(quality_report(px, cal, filled, fx_out, m_pit, cnq), f,
                  ensure_ascii=False, indent=2, default=str)
    print(f"OK: {len(list(out.iterdir()))} files written to {out}")


if __name__ == "__main__":
    main()
