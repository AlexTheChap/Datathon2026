"""Market universe of the graph: the 31 curated assets + every S&P 500 constituent.

S&P 500 list = the SPY holdings already in data/curated/etf_holdings.csv (State Street daily file,
scripts/etf_holdings.py): ticker, name, GICS sector and index weight, no extra download. Tickers are
already in Yahoo form (BRK-B); non-ticker lines (cash, CUSIP-like codes) are dropped.

listing_country is the country of the LISTING exchange (US for the S&P 500, CA for the TSX), not the legal
domicile (e.g. Linde is listed in the US, incorporated in Ireland): documented in the tools' assumptions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.tools import curated_store as cs

TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?(\.TO)?$|^[A-Z]{2,5}-USD$")
COLUMNS = ["ticker", "name", "asset_class", "sector", "currency", "market", "listing_country",
           "in_core_universe", "in_sp500", "sp500_weight"]


def _country(market: str, ticker: str) -> str:
    if ticker.endswith(".TO") or market == "TSX":
        return "CA"
    if ticker.endswith("-USD") or market.lower().startswith("crypto"):
        return "Global (crypto)"
    return "US"


def market_universe(curated_dir: str | Path) -> pd.DataFrame:
    core = cs.read(curated_dir, "assets_universe")
    core = pd.DataFrame({
        "ticker": core["ticker"], "name": core["name"], "asset_class": core["asset_class"],
        "sector": core["sector"], "currency": core["currency"], "market": core["market"],
        "listing_country": [_country(m, t) for m, t in zip(core["market"], core["ticker"])],
        "in_core_universe": True,
    })
    sp = pd.DataFrame(columns=["ticker", "name", "sector", "sp500_weight"])
    if cs.exists(curated_dir, "etf_holdings"):
        h = cs.read(curated_dir, "etf_holdings")
        h = h[(h["fund"] == "SPY") & h["ticker"].astype(str).str.match(TICKER_RE)]
        sp = (h.assign(sp500_weight=pd.to_numeric(h["weight"], errors="coerce"))
              .sort_values("sp500_weight", ascending=False).drop_duplicates("ticker")
              [["ticker", "name", "sector", "sp500_weight"]])
        sp["name"] = sp["name"].astype(str).str.title()

    uni = core.merge(sp, on="ticker", how="outer", suffixes=("", "_sp"))
    new = uni["in_core_universe"].isna()
    uni.loc[new, "name"] = uni.loc[new, "name_sp"]
    uni.loc[new, "sector"] = uni.loc[new, "sector_sp"]
    uni.loc[new, ["asset_class", "currency", "market", "listing_country"]] = ["equity", "USD", "US", "US"]
    uni["in_core_universe"] = uni["in_core_universe"].eq(True)  # NaN (S&P only) -> False
    uni["in_sp500"] = uni["sp500_weight"].notna()
    uni["sector"] = uni["sector"].replace({"": None, "nan": None}).fillna("Unclassified")
    return uni[COLUMNS].sort_values("ticker").reset_index(drop=True)
