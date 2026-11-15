"""News & docs KB retrieval (box ⑥, L13): point-in-time filter and evidence envelope, without AWS."""
import pytest

from app.tools.news_kb import as_strands_tool, build_filter, news_search


def test_filter_point_in_time_only():
    assert build_filter(as_of="2022-06-30") == {"lessThanOrEquals": {"key": "published_date", "value": 20220630}}


def test_filter_combines_with_and():
    f = build_filter(as_of="2026-11-12", tickers=["NVDA"], doc_type="filing")
    assert f == {"andAll": [
        {"lessThanOrEquals": {"key": "published_date", "value": 20261112}},
        {"equals": {"key": "doc_type", "value": "filing"}},
        {"in": {"key": "ticker", "value": ["NVDA"]}},
    ]}


def test_no_filter():
    assert build_filter() is None


@pytest.mark.parametrize("kwargs", [{"as_of": "12/11/2026"}, {"doc_type": "tweet"}])
def test_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        build_filter(**kwargs)


class FakeRuntime:
    def __init__(self, results):
        self.results, self.calls = results, []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


RESULTS = [
    {"content": {"text": "Bank of Canada raises its policy rate by 25 basis points to 0.50%\n\nDetails"},
     "score": 0.61, "location": {"s3Location": {"uri": "s3://b/news/N-1.txt"}},
     "metadata": {"doc_type": "news", "published_date": 20220302.0, "ticker": "MACRO",
                  "source": "Bank of Canada", "source_url": "https://bankofcanada.ca"}},
    {"content": {"text": "NVDA - Form 10-K ... export controls ..."}, "score": 0.55,
     "metadata": {"doc_type": "filing", "published_date": 20260225, "ticker": "NVDA", "form": "10-K",
                  "section": "item1a", "source_url": "https://sec.gov/x"}},
]


def test_envelope_and_request_sent_to_bedrock():
    rt = FakeRuntime(RESULTS)
    ev = news_search("rate hikes", as_of="2026-11-12", k=50, kb_id="KB123", client=rt)
    call = rt.calls[0]
    assert call["knowledgeBaseId"] == "KB123"
    vcfg = call["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vcfg["numberOfResults"] == 20  # capped
    assert vcfg["filter"]["lessThanOrEquals"]["value"] == 20261112
    assert ev["evidence_id"].startswith("ev-") and ev["tool"] == "news_search"
    h1, h2 = ev["output"]["hits"]
    assert h1["published_date"] == "2022-03-02" and h1["title"].startswith("Bank of Canada raises")
    assert h2["title"] == "NVDA 10-K (filed 2026-02-25) - Risk Factors"
    assert ev["period"] == "2022-03-02/2026-02-25"
    assert "most relevant" in ev["say"]


def test_same_inputs_same_evidence_id():
    a = news_search("x", as_of="2022-01-01", kb_id="K", client=FakeRuntime([]))
    b = news_search("x", as_of="2022-01-01", kb_id="K", client=FakeRuntime([]))
    assert a["evidence_id"] == b["evidence_id"] and a["output"]["n_hits"] == 0
    assert a["say"].startswith("No document published on or before 2022-01-01")


def test_errors_are_returned_not_raised(monkeypatch):
    monkeypatch.delenv("AF_KB_ID", raising=False)
    assert "AF_KB_ID" in news_search("x")["error"]
    assert "as_of" in news_search("x", as_of="yesterday", kb_id="K", client=FakeRuntime([]))["error"]


def test_strands_tool_exposes_only_agent_arguments():
    spec = as_strands_tool().tool_spec
    props = set(spec["inputSchema"]["json"]["properties"])
    assert props == {"query", "as_of", "tickers", "doc_type", "k"}
