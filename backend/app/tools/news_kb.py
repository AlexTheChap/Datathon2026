"""News & docs retrieval from the Bedrock Knowledge Base (box ⑥, link L13: News → KB).

The KB (scripts/deploy_news_kb.py) indexes two corpora in one S3 Vectors index:
  - doc_type "news"   : dated events from data/external/news/news_index.csv
                        (SEC 8-K filings, central-bank rate decisions);
  - doc_type "filing" : risk-factor / legal / contingencies sections of the latest annual reports
                        (10-K, 40-F) from scripts/fetch_10k.py.

Point-in-time rule of the architecture: only documents with published_date <= as_of are returned
(metadata filter applied by Bedrock BEFORE ranking, so nothing from the future can leak in).

`news_search(...)` returns the same EVIDENCE envelope as the quant tools (evidence_id, say, output,
assumptions, data_sources), so the EvidenceHook (L15) records it and the Explainer can cite it.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from app.tools.risk_tools import _eid

DOC_TYPES = ("news", "filing")
SECTION_LABELS = {"item1a": "Risk Factors", "item3": "Legal Proceedings",
                  "contingencies": "Commitments and Contingencies", "mdna_legal": "Legal and Other Updates"}


def _date_int(d: str) -> int:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {d!r}")
    return int(d.replace("-", ""))


def _date_str(v: Any) -> Optional[str]:
    s = str(int(float(v))) if v not in (None, "") else ""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None


def build_filter(as_of: Optional[str] = None, tickers: Optional[list[str]] = None,
                 doc_type: Optional[str] = None) -> Optional[dict]:
    """Bedrock KB metadata filter (RetrievalFilter). None when there is nothing to filter on."""
    conds: list[dict] = []
    if as_of:
        conds.append({"lessThanOrEquals": {"key": "published_date", "value": _date_int(as_of)}})
    if doc_type:
        if doc_type not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {DOC_TYPES}")
        conds.append({"equals": {"key": "doc_type", "value": doc_type}})
    if tickers:
        conds.append({"in": {"key": "ticker", "value": [str(t) for t in tickers]}})
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"andAll": conds}


def _hit(rank: int, r: dict) -> dict:
    meta = r.get("metadata") or {}
    text = (r.get("content") or {}).get("text", "")
    doc_type = meta.get("doc_type")
    if doc_type == "filing":
        title = (f"{meta.get('ticker')} {meta.get('form')} (filed {_date_str(meta.get('published_date'))}) - "
                 f"{SECTION_LABELS.get(meta.get('section'), meta.get('section'))}")
    else:
        title = text.split("\n", 1)[0].strip()
    return {
        "rank": rank, "score": round(float(r.get("score") or 0), 4), "doc_type": doc_type, "title": title,
        "published_date": _date_str(meta.get("published_date")), "ticker": meta.get("ticker"),
        "source": meta.get("source") or meta.get("form"), "source_url": meta.get("source_url"),
        "text": text[:700] + ("…" if len(text) > 700 else ""),
        "location": ((r.get("location") or {}).get("s3Location") or {}).get("uri"),
    }


def news_search(query: str, as_of: Optional[str] = None, tickers: Optional[list[str]] = None,
                doc_type: Optional[str] = None, k: int = 6, kb_id: Optional[str] = None, client=None) -> dict:
    """Semantic search in the news & filings KB, published on or before `as_of`.

    Args:
        query: what to look for, e.g. "NVDA export restrictions to China".
        as_of: point-in-time date YYYY-MM-DD (nothing published after it is returned).
        tickers: optional universe tickers, e.g. ["NVDA"] (macro/press items use the pseudo-tickers
                 "MACRO", "MARKET", "GEOPOLITICS", "CRYPTO").
        doc_type: "news" or "filing" (annual-report risk/legal sections); both when omitted.
        k: number of passages (1-20).
    """
    try:
        flt = build_filter(as_of, tickers, doc_type)
    except ValueError as e:
        return {"error": str(e), "note": "Fix the arguments and call the tool again."}
    kb_id = kb_id or os.environ.get("AF_KB_ID")
    if not kb_id:
        return {"error": "AF_KB_ID is not set (run scripts/deploy_news_kb.py)."}
    if client is None:
        import boto3

        client = boto3.client("bedrock-agent-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    vcfg: dict[str, Any] = {"numberOfResults": max(1, min(int(k), 20))}
    if flt:
        vcfg["filter"] = flt
    resp = client.retrieve(knowledgeBaseId=kb_id, retrievalQuery={"text": query},
                           retrievalConfiguration={"vectorSearchConfiguration": vcfg})
    hits = [_hit(i + 1, r) for i, r in enumerate(resp.get("retrievalResults", []))]

    inputs = {"query": query, "as_of": as_of, "tickers": tickers, "doc_type": doc_type, "k": k}
    when = f"published on or before {as_of}" if as_of else "with no date limit"
    if hits:
        top = hits[0]
        say = (f"{len(hits)} passages {when}; most relevant: \"{top['title'][:120]}\" "
               f"({top['published_date']}, {top['doc_type']}).")
    else:
        say = f"No document {when} matches this query."
    dates = [h["published_date"] for h in hits if h["published_date"]]
    return {
        "evidence_id": _eid("news_search", inputs), "tool": "news_search", "inputs": inputs,
        "output": {"hits": hits, "n_hits": len(hits)}, "say": say,
        "as_of": as_of, "period": f"{min(dates)}/{max(dates)}" if dates else None,
        "method": "Bedrock Knowledge Base semantic retrieval (Titan Text Embeddings v2, S3 Vectors, cosine); "
                  "metadata filter published_date <= as_of applied before ranking.",
        "assumptions": [
            "Point-in-time: documents published after as_of are excluded by a metadata filter.",
            "8-K items are filing titles with an excerpt of the reported item; no press coverage is included.",
            "Annual-report passages come from the latest 10-K/40-F filed on or before 2026-11-12; for an earlier "
            "as_of they are filtered out (no older reports are indexed).",
            "Similarity ranking is not a judgement of importance or of market impact.",
        ],
        "data_sources": ["SEC EDGAR (8-K, 10-K, 40-F; public domain)", 
                         "Bank of Canada / Federal Reserve rate decisions (derived from data/curated/macro_pit.csv)"],
    }


def as_strands_tool(kb_id: Optional[str] = None):
    """The same function as a Strands @tool (for the News sub-agent)."""
    from strands import tool

    @tool
    def search_news(query: str, as_of: Optional[str] = None, tickers: Optional[list[str]] = None,
                    doc_type: Optional[str] = None, k: int = 6) -> dict:
        """Search dated news (SEC 8-K filings, rate decisions) and annual-report risk/legal
        sections, published on or before `as_of`. Returns an evidence record: cite its evidence_id.

        Args:
            query: what to look for, e.g. "NVDA export restrictions to China".
            as_of: point-in-time date YYYY-MM-DD (nothing published after it is returned); never later than
                the run's reference date.
            tickers: optional tickers, e.g. ["NVDA"]; macro/press items use "MACRO", "MARKET", "GEOPOLITICS", "CRYPTO".
            doc_type: "news" or "filing"; both when omitted.
            k: number of passages (1-20).
        """
        from app.tools.context import effective_as_of     # the run's date bounds what the model asks for
        return news_search(query, as_of=effective_as_of(as_of), tickers=tickers, doc_type=doc_type, k=k, kb_id=kb_id)

    return search_news
