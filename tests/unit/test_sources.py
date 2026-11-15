"""Readable names of the evidence panel (app.agents.sources): publishers, not file paths."""
from app.agents.sources import esg_screen_sources, mix_text, period_text, source_names


def test_files_become_publishers_once():
    raw = ["prices_daily.csv (Yahoo Finance via yfinance, extracted 2026-11-13)",
           "fx_usdcad_daily.csv (Bank of Canada, series FXUSDCAD)",
           "returns_daily_cad_ext.csv (S&P 500 constituents, Yahoo Finance via yfinance, scripts/build_extended_universe.py)"]
    assert source_names(raw) == ["Yahoo Finance daily prices (downloaded 13 Nov 2026)", "Bank of Canada USD/CAD exchange rate"]


def test_internal_steps_are_not_sources():
    assert source_names(["sub-agent analysis over data/curated tools", "data/curated (prices, holdings, ESG screens)"]) == []
    assert source_names(["backend/app/core/projection_assumptions.yaml", "data/curated/correlation_full.csv"]) == [
        "FP Canada and IQPF Projection Assumption Guidelines 2026", "Yahoo Finance daily prices"]


def test_unknown_source_is_kept():
    assert source_names(["Some new feed"]) == ["Some new feed"]


def test_esg_graph_names_its_files():
    raw = ["market graph (MEMBER_OF / EXCLUDED_FROM / HAS_CONTROVERSY edges, Neptune openCypher; loaded from "
           "curated/esg_index_screen.csv and curated/controversies.csv)"]
    assert esg_screen_sources(raw) == ["Vanguard ESGV fund holdings (FTSE US All Cap Choice index), SEC filings",
                                       "Company annual reports (10-K), reviewed controversy list"]


def test_period_and_mix():
    assert period_text("2021-11-15/2026-11-12") == "Nov 2021 to Nov 2026"
    assert period_text("2026-11-13") == "13 Nov 2026"
    assert period_text(None) is None
    assert mix_text({"BTC-USD": 0.4, "ADM": 0.6, "X": 0}) == "ADM 60% · BTC-USD 40%"
