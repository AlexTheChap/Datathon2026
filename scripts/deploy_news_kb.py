"""Deploy box ⑥ "Bedrock Knowledge Base — news & docs · S3 Vectors" (link L13), with boto3 only.

    python scripts/deploy_news_kb.py              # build corpus, create/update everything, ingest
    python scripts/deploy_news_kb.py --test       # + point-in-time retrieval checks
    python scripts/deploy_news_kb.py --test-only
    python scripts/deploy_news_kb.py --delete     # tear everything down

Credentials: --profile, otherwise the AWS_* variables, otherwise the repo's .env file (git-ignored).

Inputs (already in the repo / produced by the team's scripts):
  news : data/external/news/news_index.csv      (scripts/ingest_news.py: SEC 8-K, rate decisions)
  docs : data/external/kb_filings/*.txt (+ .metadata.json)   (scripts/fetch_10k.py: 10-K / 40-F sections)

What it creates (names prefixed "alphaforge-"):
1. build/kb/{news,docs}/: one .txt per document + .txt.metadata.json (doc_type, published_date YYYYMMDD,
   ticker, source_url…) — the metadata Bedrock uses for the "published <= as_of" filter.
2. S3 bucket alphaforge-kb-<account>-<region> (private, encrypted): prefixes news/ and docs/.
3. S3 Vectors: vector bucket alphaforge-kb-vectors-<account> + index "news-docs"
   (1024 dims float32, cosine; the chunk text is stored as non-filterable metadata).
4. IAM role alphaforge-kb: read the S3 prefixes, embed with Titan v2, write/query the vector index only.
5. Knowledge Base alphaforge-news-docs (Titan Text Embeddings v2, storage S3_VECTORS) with two data sources:
   news (no chunking: one headline = one vector) and docs (fixed-size chunks of 400 tokens, 15% overlap).
6. Ingestion jobs; the KB id is written to build/news_kb.json and to .env as AF_KB_ID.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import BUILD, ROOT, ensure_bucket, ensure_role, load_dotenv, trust_policy  # noqa: E402

CORPUS = BUILD / "kb"
STATE = BUILD / "news_kb.json"

KB_NAME = "alphaforge-news-docs"
ROLE = "alphaforge-kb"
INDEX = "news-docs"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
DIMS = 1024
TAGS = {"project": "alphaforge", "component": "news-kb"}
NEWS_CSV = ROOT / "data" / "external" / "news" / "news_index.csv"
FILINGS = ROOT / "data" / "external" / "kb_filings"
MIN_FILING_CHARS = 500          # shorter extracts are failed heading matches (see fetch_10k.py warnings)
EDGAR_TO_UNIVERSE = {"ENB": "ENB.TO", "CNQ": "CNQ.TO", "CNI": "CNR.TO", "SHOP": "SHOP.TO",
                     "RY": "RY.TO", "TD": "TD.TO", "MFC": "MFC.TO"}


# ---------- 1. Corpus ----------

def build_corpus() -> dict[str, int]:
    shutil.rmtree(CORPUS, ignore_errors=True)
    news_dir, docs_dir = CORPUS / "news", CORPUS / "docs"
    news_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    df = pd.read_csv(NEWS_CSV, dtype=str, keep_default_na=False)
    for r in df.itertuples():
        text = f"{r.title}\n\n{r.summary}\n\nPublished: {r.published_at}. Source: {r.source} - {r.source_url}\n"
        (news_dir / f"{r.event_id}.txt").write_text(text, encoding="utf-8")
        meta = {"metadataAttributes": {
            "doc_type": "news", "published_date": int(r.published_at.replace("-", "")),
            "ticker": r.tickers.split(";")[0], "theme": r.theme, "origin": r.origin, "source": r.source,
            "source_url": r.source_url}}
        (news_dir / f"{r.event_id}.txt.metadata.json").write_text(json.dumps(meta, ensure_ascii=False),
                                                                  encoding="utf-8")
    n_docs, skipped = 0, []
    for txt in sorted(FILINGS.glob("*.txt")) if FILINGS.exists() else []:
        meta_path = txt.with_name(txt.name + ".metadata.json")
        body = txt.read_text(encoding="utf-8").strip()
        if len(body) < MIN_FILING_CHARS or not meta_path.exists():
            skipped.append(txt.stem)
            continue
        m = json.loads(meta_path.read_text(encoding="utf-8"))["metadataAttributes"]
        ticker = EDGAR_TO_UNIVERSE.get(m["ticker"], m["ticker"])
        section = m["doc_type"].split("_", 1)[1] if "_" in m["doc_type"] else m["doc_type"]
        header = (f"{ticker} - Form {m['form']} for fiscal period ending {m['fiscal_period_end']}, "
                  f"section {section}, filed {str(m['published_date'])[:4]}-{str(m['published_date'])[4:6]}-"
                  f"{str(m['published_date'])[6:]}. Source: {m['source_url']}\n\n")
        (docs_dir / txt.name).write_text(header + body + "\n", encoding="utf-8")
        meta = {"metadataAttributes": {
            "doc_type": "filing", "published_date": int(m["published_date"]), "ticker": ticker,
            "form": m["form"], "section": section, "fiscal_period_end": m["fiscal_period_end"],
            "source": f"SEC EDGAR ({m['form']})", "source_url": m["source_url"]}}
        (docs_dir / f"{txt.name}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        n_docs += 1
    print(f"• Corpus: {len(df)} news, {n_docs} filing sections in {CORPUS}"
          + (f" (skipped {len(skipped)} empty/short extracts: {', '.join(skipped)})" if skipped else ""))
    return {"news": len(df), "docs": n_docs}


# ---------- 2. S3 ----------

def sync_corpus(s3, bucket: str) -> None:
    """Upload build/kb/ to the bucket and delete objects that are no longer in the corpus."""
    local = {p.relative_to(CORPUS).as_posix(): p for p in CORPUS.rglob("*") if p.is_file()}
    remote = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        remote |= {o["Key"] for o in page.get("Contents", [])}
    for key, path in local.items():
        s3.upload_file(str(path), bucket, key)
    stale = sorted(remote - set(local))
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
    print(f"• Uploaded {len(local)} files to s3://{bucket}/ (removed {len(stale)} stale)")


# ---------- 3. S3 Vectors ----------

def ensure_vector_index(sv, vbucket: str) -> str:
    try:
        sv.get_vector_bucket(vectorBucketName=vbucket)
    except sv.exceptions.NotFoundException:
        sv.create_vector_bucket(vectorBucketName=vbucket)
        print(f"• Vector bucket created: {vbucket}")
    try:
        idx = sv.get_index(vectorBucketName=vbucket, indexName=INDEX)["index"]
    except sv.exceptions.NotFoundException:
        sv.create_index(vectorBucketName=vbucket, indexName=INDEX, dataType="float32", dimension=DIMS,
                        distanceMetric="cosine",
                        # chunk text + Bedrock bookkeeping are stored but not filterable (2 KB filterable limit)
                        metadataConfiguration={"nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT",
                                                                             "AMAZON_BEDROCK_METADATA"]})
        idx = sv.get_index(vectorBucketName=vbucket, indexName=INDEX)["index"]
        print(f"• Vector index created: {vbucket}/{INDEX} ({DIMS} dims, cosine)")
    return idx["indexArn"]


# ---------- 4. IAM ----------

def ensure_kb_role(iam, account: str, region: str, bucket: str, index_arn: str) -> tuple[str, bool]:
    arn, created = ensure_role(iam, ROLE, trust_policy("bedrock.amazonaws.com", account),
                               "AlphaForge Bedrock Knowledge Base (news & docs, S3 Vectors)", TAGS)
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "bedrock:InvokeModel",
         "Resource": f"arn:aws:bedrock:{region}::foundation-model/{EMBED_MODEL}"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"},
        {"Effect": "Allow", "Action": ["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors",
                                       "s3vectors:QueryVectors", "s3vectors:ListVectors", "s3vectors:GetIndex"],
         "Resource": index_arn},
    ]}
    iam.put_role_policy(RoleName=ROLE, PolicyName="kb-access", PolicyDocument=json.dumps(policy))
    return arn, created


# ---------- 5. Knowledge Base ----------

def find_kb(ag) -> dict | None:
    for page in ag.get_paginator("list_knowledge_bases").paginate():
        for k in page["knowledgeBaseSummaries"]:
            if k["name"] == KB_NAME:
                return ag.get_knowledge_base(knowledgeBaseId=k["knowledgeBaseId"])["knowledgeBase"]
    return None


def ensure_kb(ag, role_arn: str, index_arn: str, region: str) -> dict:
    kb = find_kb(ag)
    if kb is None:
        for attempt in range(10):  # new IAM role propagation
            try:
                kb = ag.create_knowledge_base(
                    name=KB_NAME, roleArn=role_arn,
                    description="AlphaForge news (8-K filings, rate decisions) and annual-report "
                                "risk/legal sections; filter published_date <= as_of",
                    knowledgeBaseConfiguration={"type": "VECTOR", "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": f"arn:aws:bedrock:{region}::foundation-model/{EMBED_MODEL}",
                        "embeddingModelConfiguration": {"bedrockEmbeddingModelConfiguration": {
                            "dimensions": DIMS, "embeddingDataType": "FLOAT32"}}}},
                    storageConfiguration={"type": "S3_VECTORS", "s3VectorsConfiguration": {"indexArn": index_arn}},
                    tags=TAGS)["knowledgeBase"]
                break
            except ag.exceptions.ValidationException as e:
                if attempt == 9 or not any(w in str(e).lower() for w in ("role", "assume", "permission")):
                    raise
                time.sleep(6)
        print(f"• Knowledge Base created: {kb['knowledgeBaseId']}")
    else:
        print(f"• Existing Knowledge Base: {kb['knowledgeBaseId']}")
    kid = kb["knowledgeBaseId"]
    for _ in range(60):
        kb = ag.get_knowledge_base(knowledgeBaseId=kid)["knowledgeBase"]
        if kb["status"] == "ACTIVE":
            return kb
        if "FAIL" in kb["status"]:
            raise RuntimeError(f"KB {kb['status']}: {kb.get('failureReasons')}")
        time.sleep(3)
    raise TimeoutError("KB not ACTIVE")


DATA_SOURCES = {
    "news": {"prefix": "news/", "chunking": {"chunkingStrategy": "NONE"},
             "description": "Dated news events: one headline + summary per document"},
    "docs": {"prefix": "docs/", "chunking": {"chunkingStrategy": "FIXED_SIZE", "fixedSizeChunkingConfiguration": {
        "maxTokens": 400, "overlapPercentage": 15}},
             "description": "Annual-report sections (10-K / 40-F): risk factors, legal proceedings, contingencies"},
}


def ensure_data_sources(ag, kb_id: str, bucket: str) -> dict[str, str]:
    existing = {d["name"]: d["dataSourceId"]
                for d in ag.list_data_sources(knowledgeBaseId=kb_id, maxResults=100)["dataSourceSummaries"]}
    ids = {}
    for name, cfg in DATA_SOURCES.items():
        ds_cfg = {"type": "S3", "s3Configuration": {"bucketArn": f"arn:aws:s3:::{bucket}",
                                                    "inclusionPrefixes": [cfg["prefix"]]}}
        vec = {"chunkingConfiguration": cfg["chunking"]}
        if name in existing:
            ids[name] = existing[name]  # chunking cannot change after creation; config kept as is
        else:
            ids[name] = ag.create_data_source(knowledgeBaseId=kb_id, name=name, description=cfg["description"],
                                              dataSourceConfiguration=ds_cfg,
                                              vectorIngestionConfiguration=vec)["dataSource"]["dataSourceId"]
            print(f"• Data source created: {name} ({cfg['prefix']})")
    return ids


def ingest(ag, kb_id: str, ds_ids: dict[str, str]) -> None:
    """One data source at a time: Bedrock allows a single running ingestion job per knowledge base."""
    print("• Ingestion (embedding with Titan v2)...")
    for name, ds_id in ds_ids.items():
        for _ in range(60):
            try:
                jid = ag.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]["ingestionJobId"]
                break
            except ag.exceptions.ConflictException:  # a previous job (e.g. an interrupted run) is still running
                time.sleep(10)
        while True:
            time.sleep(10)
            job = ag.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=jid)["ingestionJob"]
            if job["status"] in ("COMPLETE", "FAILED", "STOPPED"):
                break
        st = job.get("statistics", {})
        print(f"  {name}: {job['status']}  scanned={st.get('numberOfDocumentsScanned')} "
              f"indexed(new/modified)={st.get('numberOfNewDocumentsIndexed')}/"
              f"{st.get('numberOfModifiedDocumentsIndexed')} deleted={st.get('numberOfDocumentsDeleted')} "
              f"failed={st.get('numberOfDocumentsFailed')}")
        for reason in job.get("failureReasons", [])[:5]:
            print(f"    ! {reason[:300]}")
        if job["status"] != "COMPLETE":
            raise RuntimeError(f"ingestion {name} {job['status']}")


def save_state(kb_id: str, bucket: str, vbucket: str, ds_ids: dict, region: str) -> None:
    BUILD.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"kb_id": kb_id, "bucket": bucket, "vector_bucket": vbucket, "index": INDEX,
                                 "data_sources": ds_ids, "region": region}, indent=2))
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith("AF_KB_ID=")] + [f"AF_KB_ID={kb_id}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["AF_KB_ID"] = kb_id
    print(f"• KB id: {kb_id}  (saved in .env as AF_KB_ID)")


# ---------- Test ----------

def test(session, kb_id: str) -> bool:
    from app.tools.news_kb import news_search

    rt = session.client("bedrock-agent-runtime")
    ok = True

    def show(label, ev, check):
        nonlocal ok
        good = "error" not in ev and check(ev)
        ok &= good
        print(f"\n{'✓' if good else '✗'} {label}\n  say: {ev.get('say') or ev.get('error')}")
        for h in ev.get("output", {}).get("hits", [])[:4]:
            print(f"   {h['rank']}. [{h['published_date']}] {h['doc_type']:<6} {h['ticker'] or '-':<11} "
                  f"{h['score']:.3f}  {h['title'][:90]}")

    def all_before(d):
        return lambda ev: ev["output"]["n_hits"] > 0 and all(h["published_date"] <= d for h in ev["output"]["hits"])

    show("Rate hikes, as_of 2022-06-30: nothing published after that date",
         news_search("central bank raises interest rates", as_of="2022-06-30", k=6, kb_id=kb_id, client=rt),
         all_before("2022-06-30"))
    show("NVDA filings (risk factors on export controls), as_of 2026-11-12",
         news_search("export controls restrictions on sales to China", as_of="2026-11-12", tickers=["NVDA"],
                     doc_type="filing", k=4, kb_id=kb_id, client=rt),
         lambda ev: ev["output"]["n_hits"] > 0 and all(h["ticker"] == "NVDA" and h["doc_type"] == "filing"
                                                       for h in ev["output"]["hits"]))
    show("Same filing query as_of 2022-12-31: annual reports filed in 2025-26 must NOT leak into the past",
         news_search("export controls restrictions on sales to China", as_of="2022-12-31", tickers=["NVDA"],
                     doc_type="filing", k=4, kb_id=kb_id, client=rt),
         lambda ev: ev["output"]["n_hits"] == 0)
    show("Enbridge (news + filings) up to 2026-11-12, ticker filter",
         news_search("Enbridge pipeline", as_of="2026-11-12", tickers=["ENB.TO"], k=4, kb_id=kb_id, client=rt),
         lambda ev: ev["output"]["n_hits"] > 0 and all(h["ticker"] == "ENB.TO" for h in ev["output"]["hits"]))
    show("Recent market news (week of 2026-11-14)",
         news_search("AI stocks market rally", as_of="2026-11-14", doc_type="news", k=4, kb_id=kb_id, client=rt),
         all_before("2026-11-14"))
    print("\n• All retrieval checks passed" if ok else "\n✗ Some retrieval checks failed")
    return ok


# ---------- Delete ----------

def delete_all(session, bucket: str, vbucket: str) -> None:
    ag, sv, s3, iam = (session.client(n) for n in ("bedrock-agent", "s3vectors", "s3", "iam"))
    kb = find_kb(ag)
    if kb:
        kid = kb["knowledgeBaseId"]
        for d in ag.list_data_sources(knowledgeBaseId=kid)["dataSourceSummaries"]:
            ag.delete_data_source(knowledgeBaseId=kid, dataSourceId=d["dataSourceId"])
        ag.delete_knowledge_base(knowledgeBaseId=kid)
        for _ in range(40):
            if find_kb(ag) is None:
                break
            time.sleep(3)
        print(f"• Knowledge Base deleted: {kid}")
    try:
        sv.delete_index(vectorBucketName=vbucket, indexName=INDEX)
        sv.delete_vector_bucket(vectorBucketName=vbucket)
        print(f"• Vector bucket deleted: {vbucket}")
    except sv.exceptions.NotFoundException:
        pass
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=bucket)
        print(f"• Bucket deleted: {bucket}")
    except s3.exceptions.NoSuchBucket:
        pass
    try:
        for p in iam.list_role_policies(RoleName=ROLE)["PolicyNames"]:
            iam.delete_role_policy(RoleName=ROLE, PolicyName=p)
        iam.delete_role(RoleName=ROLE)
        print(f"• Role deleted: {ROLE}")
    except iam.exceptions.NoSuchEntityException:
        pass
    STATE.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--region", default=None)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--test-only", action="store_true")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    try:  # Windows: system certificates for botocore
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    if not args.profile:
        load_dotenv()
    import boto3

    region = args.region or os.environ.get("AWS_REGION") or "us-east-1"
    session = boto3.Session(profile_name=args.profile, region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket, vbucket = f"alphaforge-kb-{account}-{region}", f"alphaforge-kb-vectors-{account}"
    print(f"• Account: {account}  region: {region}")

    if args.delete:
        delete_all(session, bucket, vbucket)
        return
    if not args.test_only:
        build_corpus()
        s3 = session.client("s3")
        ensure_bucket(s3, bucket, region, TAGS)
        sync_corpus(s3, bucket)
        index_arn = ensure_vector_index(session.client("s3vectors"), vbucket)
        role_arn, created = ensure_kb_role(session.client("iam"), account, region, bucket, index_arn)
        if created:
            time.sleep(10)
        ag = session.client("bedrock-agent")
        kb = ensure_kb(ag, role_arn, index_arn, region)
        ds_ids = ensure_data_sources(ag, kb["knowledgeBaseId"], bucket)
        ingest(ag, kb["knowledgeBaseId"], ds_ids)
        save_state(kb["knowledgeBaseId"], bucket, vbucket, ds_ids, region)
    if args.test or args.test_only:
        kb_id = os.environ.get("AF_KB_ID") or json.loads(STATE.read_text())["kb_id"]
        sys.exit(0 if test(session, kb_id) else 1)


if __name__ == "__main__":
    main()
