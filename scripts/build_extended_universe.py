"""Extended universe: every S&P 500 constituent, with the same price history as the 31 curated assets.

    .venv/Scripts/python scripts/build_extended_universe.py            # Windows (.venv/bin/python elsewhere)
    .venv/Scripts/python scripts/build_extended_universe.py --system-certs   # behind a TLS-inspecting proxy

The 31 curated assets come from the official starter dataset (scripts/build_curated.py) and are left
untouched. This script ADDS the S&P 500 constituents that are not among them, in separate tables, so the
risk tools (metrics, correlation, risk_contribution, concentration, governance, optimizer, projection)
accept them like any curated asset:

    assets_universe_ext.csv        ticker, name, asset_class (equity), market (US), currency (USD), GICS sector
    returns_daily_cad_ext.csv      daily simple returns in CAD, same calendar and dates as returns_daily_cad.csv
    extended_universe_manifest.json  source, retrieval date, window, dropped tickers, quality warnings

Constituents = the SPY holdings already in curated/etf_holdings.csv (State Street daily file, see
app/market/universe.py), so the list is the one of the holdings date, not a historical membership.

Processing, identical to build_curated.py for the curated assets:
- adj_close (total return, dividends reinvested) from Yahoo Finance, on the curated calendar
  (calendar.csv: union of TSX and US trading days); last price carried forward on a US holiday,
  never before the first quote;
- converted to CAD with fx_usdcad_filled.csv (Bank of Canada);
- a ticker with fewer than MIN_OBS daily returns in the window (recent listing) is dropped and listed
  in the manifest; one-day moves above 40% are flagged, never removed.

Limits (repeated in the tools' assumptions): Yahoo Finance is unofficial and unlicensed (prototype only);
current constituents only, so the history carries a strong survivorship bias; no fundamentals, ESG detail,
10-K excerpts or rate sensitivity for these tickers.

After a rebuild, re-run the GARCH job so vol_forecast.csv covers the extension too:
    .venv/Scripts/python scripts/run_garch_sagemaker.py --local --run-backtest
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.market.refresh import SOURCE, download, quality_warnings
from app.market.universe import market_universe

MIN_OBS = 252          # one trading year of returns, the minimum for a volatility or a drawdown worth citing
CHUNK = 100            # tickers per Yahoo request (rate limits)
UNIVERSE_COLUMNS = ["asset_id", "ticker", "name", "asset_class", "market", "currency", "sector"]


def constituents(curated: Path) -> pd.DataFrame:
    """S&P 500 constituents that are NOT already curated, with their GICS sector."""
    uni = market_universe(curated)
    return uni[uni["in_sp500"] & ~uni["in_core_universe"]][["ticker", "name", "sector"]].reset_index(drop=True)


def fetch(tickers: list[str], start: str) -> pd.DataFrame:
    parts = [download(tickers[i:i + CHUNK], start=start) for i in range(0, len(tickers), CHUNK)]
    return pd.concat(parts, ignore_index=True)


def build(prices: pd.DataFrame, calendar: pd.DatetimeIndex, fx: pd.Series,
          min_obs: int = MIN_OBS) -> tuple[pd.DataFrame, dict[str, str]]:
    """Long Yahoo bars -> wide daily CAD returns on the curated calendar. Returns (returns, dropped)."""
    local = prices.pivot_table(index="date", columns="ticker", values="adj_close", aggfunc="last")
    local = local.reindex(calendar).ffill()          # carried forward after the first quote only
    cad = local.mul(fx.reindex(calendar).ffill().bfill(), axis=0)
    rets = cad.pct_change(fill_method=None).iloc[1:]
    n_obs = rets.notna().sum()
    dropped = {t: f"only {int(n)} daily returns in the window (< {min_obs}): recent listing"
               for t, n in n_obs.items() if n < min_obs}
    return rets.drop(columns=list(dropped)).sort_index(axis=1), dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--curated", default=str(ROOT / "data" / "curated"))
    ap.add_argument("--system-certs", action="store_true",
                    help="use the OS certificate store (Windows behind a TLS-inspecting proxy)")
    a = ap.parse_args()
    curated = Path(a.curated)
    if a.system_certs:
        import truststore

        truststore.inject_into_ssl()

    cal = pd.DatetimeIndex(pd.read_csv(curated / "calendar.csv", parse_dates=["date"])["date"])
    fx = pd.read_csv(curated / "fx_usdcad_filled.csv", parse_dates=["date"]).set_index("date")["usdcad"]
    members = constituents(curated)
    tickers = members["ticker"].tolist()
    print(f"Downloading {len(tickers)} S&P 500 constituents from {cal.min():%Y-%m-%d}…")
    prices = fetch(tickers, start=f"{cal.min():%Y-%m-%d}")
    prices = prices[prices["date"] <= cal.max()]
    missing = sorted(set(tickers) - set(prices["ticker"]))

    rets, dropped = build(prices, cal, fx)
    dropped.update({t: "no price returned by Yahoo Finance" for t in missing})
    kept = members[members["ticker"].isin(rets.columns)].sort_values("ticker").reset_index(drop=True)
    uni = kept.assign(asset_id=[f"E{i + 1:03d}" for i in range(len(kept))], asset_class="equity",
                      market="US", currency="USD")[UNIVERSE_COLUMNS]

    holdings_date = str(pd.read_csv(curated / "etf_holdings.csv", usecols=["fund", "holdings_date"])
                        .query("fund == 'SPY'")["holdings_date"].iloc[0])
    manifest = {
        "source": SOURCE,
        "retrieved": date.today().isoformat(),
        "constituents": f"SPY holdings of {holdings_date} (curated/etf_holdings.csv, State Street)",
        "window": f"{rets.index.min():%Y-%m-%d}/{rets.index.max():%Y-%m-%d}",
        "processing": "adj_close (total return) on calendar.csv, carried forward on US holidays after the first "
                      "quote, converted to CAD with fx_usdcad_filled.csv (Bank of Canada); simple daily returns.",
        "tickers_requested": len(tickers), "tickers_kept": len(uni),
        "dropped": dict(sorted(dropped.items())),
        "quality_warnings": quality_warnings(prices[prices["ticker"].isin(uni["ticker"])]),
        "limitations": [
            "Yahoo Finance is an unofficial, unlicensed source: fine for a datathon prototype, not for production.",
            "Current constituents only: companies removed from the index since 2021 are absent (survivorship bias).",
            "No fundamentals, ESG detail, 10-K excerpts or rate sensitivity for these tickers.",
        ],
    }
    uni.to_csv(curated / "assets_universe_ext.csv", index=False)
    rets.round(8).to_csv(curated / "returns_daily_cad_ext.csv", index_label="date")
    (curated / "extended_universe_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"OK: {len(uni)} tickers kept, {len(dropped)} dropped, {len(manifest['quality_warnings'])} warnings "
          f"-> {curated}")


if __name__ == "__main__":
    main()
