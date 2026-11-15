"""Every ticker of the universe (curated + S&P 500 extension) maps to exactly one projection category with a rate."""
from pathlib import Path

import pytest
from app.quant.assumptions import category_of, load_assumptions
from app.tools import curated_store as cs

ROOT = Path(__file__).resolve().parents[2]
CFG = load_assumptions()
UNIVERSE = cs.read_extended(ROOT / "data" / "curated", "assets_universe")


@pytest.mark.parametrize("row", UNIVERSE.to_dict("records"), ids=lambda r: r["ticker"])
def test_every_universe_ticker_maps_to_one_category(row):
    cat = category_of(row["ticker"], row["asset_class"], row["market"], CFG)
    in_rates, in_team = cat.name in CFG["rates"], cat.name in CFG.get("team_assumptions", {})
    assert in_rates != in_team                       # exactly one source of the rate
    assert cat.team_assumption == in_team
    assert 0 < cat.rate < 0.2


def test_mapping_markets_exist_in_the_universe():
    markets = set(UNIVERSE["market"])
    keys = CFG["mapping"]["by_asset_class_and_market"]
    assert {k.removeprefix("equity_") for k in keys} <= markets
    assert set(CFG["mapping"]["by_ticker"]) <= set(UNIVERSE["ticker"])


def test_team_assumptions_equal_inflation_and_labelled():
    assert CFG["team_assumptions"] == {"gold": CFG["inflation"], "crypto": CFG["inflation"]}
    assert category_of("BTC-USD", "crypto", "Crypto (24/7)", CFG).team_assumption


def test_unknown_market_is_unmapped():
    from app.quant.assumptions import UnmappedTickerError

    with pytest.raises(UnmappedTickerError):
        category_of("XYZ", "equity", "LSE", CFG)
