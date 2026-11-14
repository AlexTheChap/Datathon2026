"""
Fills data/curated/controversies.csv from the 10-K excerpts produced by fetch_10k.py, using Claude
on Amazon Bedrock. The LLM PROPOSES rows; the code CHECKS them; a human VALIDATES them.

    pip install "anthropic[bedrock]" pandas
    # AWS credentials: profile, environment variables or SageMaker role (Bedrock access required)
    python scripts/fetch_10k.py --tickers XOM ENB TSLA --as-of 2026-11-12 --email ... --out data/external/kb_filings
    python scripts/extract_controversies.py --kb data/external/kb_filings --tickers XOM ENB TSLA \
        --ticker-map ENB=ENB.TO --region us-east-1

Deterministic checks on each proposed row (otherwise the row is rejected and printed):
  - quote appears VERBATIM in the source text (whitespace/quotes normalized);
  - a non-empty amount must appear in the quote;
  - category / severity / section are in the allowed lists;
  - publication_date = FILING date of the 10-K (point-in-time), never a date chosen by the LLM.
All rows written have status = "to_verify" and an empty reviewer. governance_check only blocks
on "verified" rows: a human reads the quote, then fills in
status=verified, reviewer, review_date.

Test without AWS: --from-json response.json validates a response already obtained (format {"controversies": [...]}).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

MODEL = "anthropic.claude-haiku-4-5"
FALLBACK_MODEL = "anthropic.claude-opus-4-8"
COLUMNS = ["ticker", "publication_date", "category", "description", "quote", "severity", "status",
           "amount_currency", "amount", "source_doc", "section", "source_url", "reviewer", "review_date"]
CATEGORIES = ["environment", "social", "governance", "product_safety", "regulatory", "other"]
SEVERITIES = ["minor", "moderate", "major"]
SECTIONS = ["contingencies", "mdna_legal", "item3", "item1a"]

SCHEMA = {
    "type": "object",
    "properties": {
        "controversies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "description": {"type": "string"},
                    "quote": {"type": "string"},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "severity_justification": {"type": "string"},
                    "amount_currency": {"type": "string", "enum": ["USD", "CAD", ""]},
                    "amount": {"type": ["number", "null"]},
                    "section": {"type": "string", "enum": SECTIONS},
                },
                "required": ["category", "description", "quote", "severity", "severity_justification",
                             "amount_currency", "amount", "section"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["controversies"],
    "additionalProperties": False,
}

SYSTEM = """You extract ESG controversies from SEC annual report excerpts (10-K or 40-F) for an investment-suitability tool.
A controversy is a specific, named legal proceeding, government investigation, enforcement action,
verdict, settlement or allegation involving the company's conduct (environmental, social, governance,
product safety, regulatory). Generic risk-factor language ("we may be subject to litigation") is NOT a
controversy. One entry per distinct matter; merge follow-up developments of the same matter.

Rules:
- quote: copy ONE contiguous passage from the source, character for character (no ellipses, no
  paraphrase, no added words), 1-3 sentences, that best supports the entry. It is checked by exact match;
  entries whose quote is not found verbatim are discarded.
- amount: only an amount that appears inside quote (in full units, e.g. "$ 345 million" -> 345000000);
  otherwise null and amount_currency "".
- description: one or two neutral sentences in English: who, what, current status. No speculation.
- severity:
  * major: death or serious injury linked to the company's products/operations; a government agency
    alleging systemic misconduct (discrimination, fraud, environmental violations); a verdict, fine or
    settlement of USD 100 million or more; or allegations of misleading investors about core products.
  * moderate: other active lawsuits or investigations with a specific named matter, or amounts between
    USD 10 and 100 million.
  * minor: resolved/dismissed matters with no lasting finding, or smaller amounts.
  severity_justification: one short English sentence citing which criterion applies.
- Return an empty list if the excerpts contain no specific controversy."""


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[–—−]", "-", s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:%)])", r"\1", s)        # "$ 345 million ," becomes "$345 million,"
    s = re.sub(r"([$(])\s+", r"\1", s)
    return s.strip().casefold()


SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


def amounts_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion)?", text, flags=re.I):
        v = float(m.group(1).replace(",", ""))
        out.append(v * SCALE.get((m.group(2) or "").lower(), 1))
    return out


def validate(rows: list[dict], sources: dict[str, str]) -> tuple[list[dict], list[tuple[dict, str]]]:
    ok, bad = [], []
    normed = {k: norm(v) for k, v in sources.items()}
    for r in rows:
        q = norm(r.get("quote", ""))
        if r.get("category") not in CATEGORIES or r.get("severity") not in SEVERITIES:
            bad.append((r, "category/severity not in allowed list")); continue
        if len(q) < 40:
            bad.append((r, "quote too short")); continue
        found = [s for s, t in normed.items() if q in t]
        if not found:
            bad.append((r, "quote not found verbatim in the source")); continue
        r["section"] = r["section"] if r.get("section") in found else found[0]
        m = r.get("amount")
        if m not in (None, "", 0):
            if not any(abs(a - float(m)) <= 0.005 * float(m) for a in amounts_in(r["quote"])):
                bad.append((r, f"amount {m} not in the quote")); continue
        else:
            r["amount"], r["amount_currency"] = None, ""
        ok.append(r)
    return ok, bad


def load_sources(kb: Path, ticker: str, include_item1a: bool):
    """Latest fiscal year available for this ticker: {section: text}, metadata."""
    files = sorted(kb.glob(f"{ticker}_*_contingencies.txt"), key=lambda p: p.name.split("_")[-2])  # 10K or 40F
    if not files:
        return None, None
    stem = files[-1].name.replace("_contingencies.txt", "")
    secs = ["contingencies", "mdna_legal", "item3"] + (["item1a"] if include_item1a else [])
    sources = {s: (kb / f"{stem}_{s}.txt").read_text(encoding="utf-8") for s in secs if (kb / f"{stem}_{s}.txt").exists()}
    meta = json.loads((kb / f"{stem}_contingencies.txt.metadata.json").read_text(encoding="utf-8"))["metadataAttributes"]
    return {k: v for k, v in sources.items() if v.strip()}, meta


def call_claude(client, ticker: str, sources: dict[str, str]) -> list[dict]:
    doc = "\n\n".join(f'<excerpt section="{s}">\n{t}\n</excerpt>' for s, t in sources.items())
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Company ticker: {ticker}\n\n{doc}\n\nExtract the controversies."}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        cat = resp.stop_details.category if resp.stop_details else None
        raise RuntimeError(f"model refusal (category: {cat})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("response truncated (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["controversies"]


def make_client(region: str):
    from anthropic import AnthropicBedrockMantle
    try:  # automatic fallback to another model if Claude refuses (not available server-side on Bedrock)
        from anthropic import BetaRefusalFallbackMiddleware
        return AnthropicBedrockMantle(aws_region=region,
                                      middleware=[BetaRefusalFallbackMiddleware([{"model": FALLBACK_MODEL}])])
    except (ImportError, TypeError):
        print("(SDK without BetaRefusalFallbackMiddleware: no fallback on refusal)")
        return AnthropicBedrockMantle(aws_region=region)


def to_rows(items: list[dict], ticker_out: str, meta: dict) -> list[dict]:
    pub = str(meta["published_date"])
    return [{
        "ticker": ticker_out, "publication_date": f"{pub[:4]}-{pub[4:6]}-{pub[6:]}",
        "category": it["category"], "description": it["description"], "quote": it["quote"],
        "severity": it["severity"], "status": "to_verify", "amount_currency": it.get("amount_currency") or "",
        "amount": it.get("amount"), "source_doc": f"{meta.get('form', '10-K')} {meta['fiscal_period_end'][:4]} ({meta['accession']})",
        "section": it["section"], "source_url": meta["source_url"], "reviewer": "", "review_date": "",
    } for it in items]


def merge(out: Path, new: pd.DataFrame) -> pd.DataFrame:
    """Replaces the NOT-yet-reviewed rows of the same ticker and document; keeps everything a human has validated."""
    if out.exists():
        old = pd.read_csv(out, dtype=str).fillna("")
        key = set(zip(new["ticker"], new["source_doc"]))
        stale = old.apply(lambda r: (r["ticker"], r["source_doc"]) in key and r["status"] == "to_verify", axis=1)
        reviewed = old[~stale]
        new = new[~new["quote"].isin(reviewed["quote"])]
        return pd.concat([reviewed, new], ignore_index=True)[COLUMNS]
    return new[COLUMNS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default="data/external/kb_filings", help="output folder of fetch_10k.py")
    ap.add_argument("--tickers", nargs="+", required=True, help="SEC tickers (the ones used with fetch_10k.py)")
    ap.add_argument("--ticker-map", nargs="*", default=[], help="SEC=universe, e.g. ENB=ENB.TO CNQ=CNQ.TO")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--out", default="data/curated/controversies.csv")
    ap.add_argument("--include-item1a", action="store_true", help="also include risk factors (long, often generic)")
    ap.add_argument("--from-json", help="validate a JSON response already obtained (single ticker), without calling Bedrock")
    a = ap.parse_args()
    tmap = dict(m.split("=", 1) for m in a.ticker_map)
    kb, out = Path(a.kb), Path(a.out)
    client = None if a.from_json else make_client(a.region)

    all_rows = []
    for t in a.tickers:
        sources, meta = load_sources(kb, t, a.include_item1a)
        if not sources:
            print(f"[{t}] no excerpts in {kb}: run fetch_10k.py first"); continue
        try:
            items = (json.loads(Path(a.from_json).read_text(encoding="utf-8"))["controversies"] if a.from_json
                     else call_claude(client, t, sources))
        except Exception as e:  # a failed ticker does not stop the others
            print(f"[{t}] FAILED : {e}"); continue
        ok, bad = validate(items, sources)
        for r, why in bad:
            print(f"[{t}] REJECTED ({why}) : {r.get('quote', '')[:100]!r}")
        rows = to_rows(ok, tmap.get(t, t), meta)
        for r in rows:
            print(f"[{t}] {r['severity']:<8} {r['category']:<14} {r['description'][:90]}")
        print(f"[{t}] {len(rows)} row(s) kept, {len(bad)} rejected  ({meta.get('form', '10-K')} filed {rows[0]['publication_date'] if rows else meta['published_date']})")
        all_rows += rows

    if not all_rows:
        print("Nothing to write."); sys.exit(0 if a.from_json else 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = merge(out, pd.DataFrame(all_rows))
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"\n{out} : {len(df)} rows. TO DO: read each quote, then set status=verified, reviewer, review_date.")


if __name__ == "__main__":
    main()
