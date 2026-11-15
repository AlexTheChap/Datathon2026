"""News search tool: point-in-time filter, filters, ranking, evidence. A small synthetic corpus, no network."""
from __future__ import annotations

import pandas as pd
import pytest
from app.tools import news_tools
from app.tools.context import current_as_of

ROWS = [
    # event_id, published_at, title, source, source_url, tickers, theme, origin, n_sources
    ("N-0000000001", "2022-03-02", "Bank of Canada raises its policy rate by 25 basis points", "Bank of Canada",
     "https://boc", "MACRO", "rates", "macro_derived", 1),
    ("N-0000000002", "2022-05-10", "NVDA: Form 8-K (financial results)", "SEC EDGAR (8-K)", "https://sec/1",
     "NVDA", "company", "sec_8k", 1),
    ("N-0000000003", "2022-09-15", "NVDA: Form 8-K (acquisition or disposition of assets)", "SEC EDGAR (8-K)",
     "https://sec/2", "NVDA", "company", "sec_8k", 1),
    ("N-0000000004", "2026-11-10", "Federal Reserve signals further interest rate hikes", "Federal Reserve",
     "https://fed", "MACRO", "rates", "macro_derived", 1),
    ("N-0000000005", "2026-11-11", "AAPL: Form 8-K (departure of directors or officers)", "SEC EDGAR (8-K)",
     "https://sec/3", "AAPL", "company", "sec_8k", 1),
]
COLUMNS = ["event_id", "published_at", "title", "source", "source_url", "tickers", "theme", "origin", "n_sources"]


@pytest.fixture(autouse=True)
def corpus(monkeypatch):
    monkeypatch.setattr(news_tools, "_index", lambda: pd.DataFrame(ROWS, columns=COLUMNS))
    token = current_as_of.set("2026-11-12")
    yield
    current_as_of.reset(token)


def ids(ev):
    return [e["event_id"] for e in ev["output"]["events"]]


def test_point_in_time_nothing_after_the_run_date():
    token = current_as_of.set("2022-06-30")
    try:
        ev = news_tools.search_news()
    finally:
        current_as_of.reset(token)
    assert set(ids(ev)) == {"N-0000000001", "N-0000000002"}
    assert ev["as_of"] == "2022-06-30" and ev["output"]["n_events_available_at_as_of"] == 2


def test_the_caller_cannot_ask_for_a_later_date():
    token = current_as_of.set("2022-06-30")
    try:
        ev = news_tools.search_news(as_of="2030-01-01")
    finally:
        current_as_of.reset(token)
    assert all(e["published_at"] <= "2022-06-30" for e in ev["output"]["events"])
    assert news_tools.search_news(as_of="2022-04-01")["as_of"] == "2022-04-01"
    assert ids(news_tools.search_news(as_of="2022-04-01")) == ["N-0000000001"]


def test_filters_by_ticker_origin_theme_and_window():
    assert ids(news_tools.search_news(tickers=["NVDA"])) == ["N-0000000003", "N-0000000002"]    # newest first
    assert ids(news_tools.search_news(origins=["macro_derived"])) == ["N-0000000004", "N-0000000001"]
    assert ids(news_tools.search_news(themes=["company"], days_back=7)) == ["N-0000000005"]
    assert set(ids(news_tools.search_news(days_back=7))) == {"N-0000000004", "N-0000000005"}
    assert ids(news_tools.search_news(tickers=["TSLA"])) == []


def test_keyword_ranking_and_limit():
    ev = news_tools.search_news(query="interest rate hikes")
    assert ids(ev)[0] == "N-0000000004"                         # the headline that matches most terms
    assert "N-0000000005" not in ids(ev)                        # no term in common: not a match
    assert len(news_tools.search_news(limit=2)["output"]["events"]) == 2
    assert len(news_tools.search_news(limit=500)["output"]["events"]) == len(ROWS)    # capped at MAX_LIMIT


def test_evidence_envelope_and_determinism():
    a, b = news_tools.search_news(query="NVDA"), news_tools.search_news(query="NVDA")
    assert a["evidence_id"].startswith("ev-") and a["evidence_id"] == b["evidence_id"]
    assert a["tool"] == "news_search" and a["output"]["events"] == b["output"]["events"]
    assert any("point-in-time" in x for x in a["assumptions"])
    assert any("incomplete" in x for x in a["assumptions"])
    assert news_tools.search_news(query="bank")["evidence_id"] != a["evidence_id"]
    assert "No news event" in news_tools.search_news(tickers=["TSLA"])["say"]


def test_unknown_origin_is_an_error_dict_for_the_model():
    with pytest.raises(ValueError):
        news_tools.search_news(origins=["twitter"])
    with pytest.raises(ValueError):  # press headlines were dropped from the corpus
        news_tools.search_news(origins=["gdelt"])
    assert "error" in news_tools.news_search(origins=["twitter"])


@pytest.mark.skipif(not (news_tools.get_settings().abs_path(news_tools.get_settings().news_dir) / "news_index.csv").exists(),
                    reason="news_index.csv not built (scripts/ingest_news.py)")
def test_real_corpus_is_readable_and_point_in_time(monkeypatch):
    monkeypatch.undo()                                          # use the real index
    token = current_as_of.set("2022-12-31")
    try:
        ev = news_tools.search_news(origins=["sec_8k"], limit=20)
    finally:
        current_as_of.reset(token)
    assert ev["output"]["events"] and all(e["published_at"] <= "2022-12-31" for e in ev["output"]["events"])
    assert all(e["origin"] == "sec_8k" for e in ev["output"]["events"])
