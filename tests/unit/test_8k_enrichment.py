"""8-K filings are read, not just titled: item extraction, merge that keeps what was read, the right SEC company per
ticker, and the search over the excerpts. Synthetic filings: no network, no AWS, no LLM."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest
from app.tools import news_tools
from app.tools.context import current_as_of

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enrich = load("enrich_8k")
sec_cik = load("sec_cik")
ingest_news = load("ingest_news")
ingest_fundamentals = load("ingest_fundamentals")


# ---------- the right SEC company ----------

SEC = {"CNR": "0001710366", "CNI": "0000016868", "XOM": "0002093318", "RY": "0001000275", "AAPL": "0000320193"}


def test_canadian_national_is_not_core_natural_resources():
    """The SEC ticker 'CNR' is Core Natural Resources (ex-CONSOL Energy), not Canadian National Railway."""
    assert sec_cik.cik_for("CNR.TO", SEC) == "0000016868"
    assert sec_cik.cik_for("CNR.TO", SEC) != SEC["CNR"]


def test_exxon_uses_the_company_that_files_not_the_new_holding_company():
    assert sec_cik.cik_for("XOM", SEC) == "0000034088"


def test_other_tickers_follow_the_sec_file_and_non_filers_have_none():
    assert sec_cik.cik_for("RY.TO", SEC) == "0001000275" and sec_cik.cik_for("AAPL", SEC) == "0000320193"
    assert sec_cik.cik_for("ATD.TO", SEC) is None and sec_cik.cik_for("CSU.TO", SEC) is None


# ---------- extraction ----------

def test_item_text_without_the_heading_and_with_the_item_list_ignored():
    text = ("UNITED STATES FORM 8-K CURRENT REPORT Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
            "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. On January 12, 2022, Jane Roe, "
            "Executive Vice President of Acme Inc., notified the Company of her intention to retire effective May 31, 2022. "
            "Item 9.01 Financial Statements and Exhibits. (d) Exhibits 104 Cover Page SIGNATURES Pursuant to the requirements "
            "of the Securities Exchange Act")
    out = enrich.extract_excerpt(text, ["5.02", "9.01"])
    assert out.startswith("On January 12, 2022, Jane Roe") and "Item 5.02" not in out and "Exhibits" not in out
    assert "Departure of Directors" not in out


def test_table_of_contents_and_cross_references_do_not_hide_the_real_body():
    text = ("ITEM 2.02. RESULTS OF OPERATIONS AND FINANCIAL CONDITION. 3 ITEM 9.01. FINANCIAL STATEMENTS AND EXHIBITS. 3 "
            "SIGNATURES 4 ITEM 2.02. RESULTS OF OPERATIONS AND FINANCIAL CONDITION. On February 3, 2022, Acme announced its "
            "fourth quarter results. A copy of the press release is Exhibit 99.1. ITEM 9.01. FINANCIAL STATEMENTS. (d) Exhibits "
            "SIGNATURES Pursuant to")
    assert enrich.extract_excerpt(text, ["2.02"]).startswith("On February 3, 2022, Acme announced")


def test_adjacent_headings_take_the_text_of_the_next_heading():
    text = ("Item 2.02 Results of Operations and Financial Condition Item 7.01 Regulation FD Disclosure The Registrant furnishes "
            "its News Release dated February 1, 2022, announcing fourth quarter 2021 results. Item 9.01 Financial Statements and "
            "Exhibits SIGNATURES")
    assert "News Release dated February 1, 2022" in enrich.extract_excerpt(text, ["2.02"])


def test_the_older_heading_wording_is_removed_too():
    text = ("Item 5.02 Departure of Directors or Principal Officers; Election of Directors; Appointment of Principal Officers. "
            "(d) On and effective as of September 25, 2022, Joseph Gebbia was appointed to serve as a member of the Board. "
            "Item 9.01 Financial Statements and Exhibits SIGNATURES")
    out = enrich.extract_excerpt(text, ["5.02"])
    assert out.startswith("(d) On and effective as of September 25, 2022") and "Principal Officers" not in out


def test_a_cover_page_without_facts_gives_nothing_to_quote():
    text = "Item 2.02 Results from Operations and Financial Condition Item 9.01 Financial Statements and Exhibits SIGNATURES"
    assert enrich.extract_excerpt(text, ["2.02"]) == ""          # the caller writes COVER_NOTE: no invented content
    assert "no facts" in enrich.COVER_NOTE


def test_several_items_are_joined_and_the_excerpt_is_bounded_at_a_sentence_end():
    long = " ".join(f"Sentence number {i} says something about the transaction." for i in range(60))
    text = f"Item 1.01 Entry into a Material Definitive Agreement {long} Item 8.01 Other Events {long} SIGNATURES"
    out = enrich.extract_excerpt(text, ["1.01", "8.01"])
    assert len(out) <= enrich.MAX_CHARS and out.endswith(".") and out.startswith("Sentence number 0")


def test_abbreviations_do_not_end_the_excerpt():
    for text in ("compensation for Mr. Duato and", "at Johnson & Johnson Inc. The Board", "its U.S. Subsidiaries", "Exhibit No. 99.1",
                 "Dr. Roe joined"):
        assert not enrich.SENTENCE_END.search(text), text
    assert enrich.SENTENCE_END.search("She will retire. The Board thanked her")
    cut = enrich._cut("The Board approved changes for Mr. Duato at Johnson & Johnson Inc. The CEO retires in May. "
                      "A successor will be named later, as described in the proxy statement filed today.", 100)
    assert cut.endswith(("The CEO retires in May.", "…"))


def test_item_codes_come_from_the_event_summary():
    assert enrich.codes_of("X: Form 8-K (a), filed with the SEC on 2022-01-01. Items: 1.01, 5.02, 8.01.") == ["1.01", "5.02", "8.01"]
    assert enrich.codes_of("no items here") == []


# ---------- the index keeps what was read ----------

def event(eid, excerpt="", ticker="NVDA"):
    return {"event_id": eid, "published_at": "2025-01-02", "title": f"{ticker}: Form 8-K (executive departure or appointment)",
            "summary": f"{ticker}: Form 8-K, filed with the SEC on 2025-01-02. Items: 5.02.", "source": "SEC EDGAR (8-K)",
            "source_url": "https://sec/x", "tickers": ticker, "theme": "company", "language": "en", "origin": "sec_8k",
            "n_sources": 1, "date_method": "SEC filing date", "retrieved_on": "2026-11-15", "excerpt": excerpt}


def test_refreshing_the_index_does_not_erase_the_excerpts_already_read(tmp_path):
    out = tmp_path / "news"
    ingest_news.write_outputs(pd.DataFrame([event("N-0000000001", "Jane Roe retires.")], columns=ingest_news.COLUMNS), out)
    ingest_news.write_outputs(pd.DataFrame([event("N-0000000001"), event("N-0000000002")], columns=ingest_news.COLUMNS), out)
    idx = pd.read_csv(out / "news_index.csv", dtype=str).fillna("").set_index("event_id")
    assert idx.loc["N-0000000001", "excerpt"] == "Jane Roe retires." and idx.loc["N-0000000002", "excerpt"] == ""
    assert len(idx) == 2 and "excerpt" in idx.columns


def test_knowledge_base_documents_carry_the_excerpt(tmp_path):
    row = event("N-0000000003", "Jane Roe retires.")
    row["summary"] += " Excerpt: Jane Roe retires."
    ingest_news.write_outputs(pd.DataFrame([row], columns=ingest_news.COLUMNS), tmp_path)
    assert "Excerpt: Jane Roe retires." in (tmp_path / "kb" / "N-0000000003.txt").read_text()


def test_partial_fundamentals_run_replaces_only_its_own_tickers():
    old = pd.DataFrame({"ticker": ["AAPL", "CNR.TO", "XOM"], "value": [1, 2, 3]})
    new = pd.DataFrame({"ticker": ["CNR.TO"], "value": [20]})
    merged = ingest_fundamentals.merge_partial(old, new, ["CNR.TO"])
    assert sorted(zip(merged["ticker"], merged["value"], strict=True)) == [("AAPL", 1), ("CNR.TO", 20), ("XOM", 3)]


# ---------- the search reads the excerpts ----------

@pytest.fixture
def corpus(monkeypatch):
    rows = [("N-0000000001", "2025-03-01", "AAPL: Form 8-K (executive departure or appointment)",
             "The Board appointed Mr. Alex Gorsky to Apple's Board and the Nominating Committee.", "SEC EDGAR (8-K)", "u1", "AAPL",
             "company", "sec_8k", 1),
            ("N-0000000002", "2025-04-01", "GOOGL: Form 8-K (other material event)",
             "Alphabet closed an offering of $25 billion of senior notes.", "SEC EDGAR (8-K)", "u2", "GOOGL", "company", "sec_8k", 1)]
    df = pd.DataFrame(rows, columns=["event_id", "published_at", "title", "excerpt", "source", "source_url", "tickers", "theme",
                                     "origin", "n_sources"])
    monkeypatch.setattr(news_tools, "_index", lambda: df)
    token = current_as_of.set("2026-11-12")
    yield
    current_as_of.reset(token)


def test_keywords_match_the_text_of_the_filing_not_only_the_title(corpus):
    ev = news_tools.search_news(query="senior notes offering")
    assert [e["event_id"] for e in ev["output"]["events"]] == ["N-0000000002"]
    assert "senior notes" in ev["output"]["events"][0]["excerpt"]
    assert [e["event_id"] for e in news_tools.search_news(query="Gorsky board")["output"]["events"]] == ["N-0000000001"]


def test_an_index_built_before_the_excerpts_still_loads(tmp_path):
    path = tmp_path / "news_index.csv"
    pd.DataFrame({"event_id": ["N-1"], "published_at": ["2025-01-01"], "title": ["t"], "source": ["s"], "source_url": ["u"],
                  "tickers": ["X"], "theme": ["company"], "origin": ["sec_8k"], "n_sources": ["1"]}).to_csv(path, index=False)
    assert list(news_tools._load(str(path), 1.0)["excerpt"]) == [""]


def test_the_real_index_has_no_company_mix_up():
    path = ROOT / "data/external/news/news_index.csv"
    if not path.exists():
        pytest.skip("news_index.csv not built")
    d = pd.read_csv(path, dtype=str).fillna("")
    k = d[(d.origin == "sec_8k") & (d.tickers == "CNR.TO")]
    assert k.empty                                         # Canadian National files 40-F / 6-K, never an 8-K
    assert not d["source_url"].str.contains("ceix|/1710366/").any()   # Core Natural Resources / CONSOL filings
    read = d[(d.origin == "sec_8k") & (d.excerpt != "")]
    assert len(read) == int((d.origin == "sec_8k").sum()) and read.excerpt.str.len().max() <= enrich.MAX_CHARS + 2
