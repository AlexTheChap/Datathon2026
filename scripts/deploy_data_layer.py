"""Deploy box ⑥ data layer: "S3 curated Parquet (prices · FX · macro · point-in-time)" and
"DynamoDB (profiles · runs · evidence ledger)", with boto3 only.

    python scripts/deploy_data_layer.py              # create/update both
    python scripts/deploy_data_layer.py --test       # + read back from S3, DynamoDB round trip
    python scripts/deploy_data_layer.py --test-only
    python scripts/deploy_data_layer.py --delete

Credentials: --profile, otherwise the AWS_* variables, otherwise the repo's .env file (git-ignored).

1. build/curated_parquet/: every table of app.tools.curated_store.TABLES converted CSV -> Parquet
   (fastparquet; same parsing as the tools), uploaded to s3://alphaforge-curated-<account>-<region>/curated/
   (private, versioned, encrypted). The quant Gateway Lambda reads them (L14): run
   scripts/deploy_quant_gateway.py afterwards, it picks the bucket up from build/data_layer.json.
2. DynamoDB table "alphaforge" (on demand, PK/SK strings): the single table already used by the API for
   profiles (ACTOR#…/PROFILE#current) and runs (RUN#…/META), and by DynamoDBLedger for the evidence
   ledger (RUN#…/EVID#…). Use it with AF_STORE=dynamodb (and AF_TABLE if renamed).

Nothing is written to .env: AF_STORE=dynamodb is a deployment choice (Runtime / API environment), and the
local tests must keep their in-memory / JSONL stand-ins.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
PARQUET = BUILD / "curated_parquet"
STATE = BUILD / "data_layer.json"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.tools import curated_store as cs  # noqa: E402
from deploy_context_lambda import load_dotenv  # noqa: E402

TABLE = "alphaforge"
PREFIX = "curated/"
TAGS = {"project": "alphaforge", "component": "data-layer"}


def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except Exception:
        pass
    kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
    s3.create_bucket(Bucket=bucket, **kwargs)
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})
    print(f"• Bucket created: s3://{bucket}")


def publish_parquet(s3, bucket: str) -> dict[str, int]:
    rows = cs.csv_to_parquet(ROOT / "data" / "curated", PARQUET)
    for name in rows:
        s3.upload_file(str(PARQUET / f"{name}.parquet"), bucket, f"{PREFIX}{name}.parquet")
    size = sum((PARQUET / f"{n}.parquet").stat().st_size for n in rows) / 1e6
    print(f"• Parquet published to s3://{bucket}/{PREFIX} ({size:.1f} MB): "
          + ", ".join(f"{n} ({r} rows)" for n, r in rows.items()))
    return rows


def ensure_table(ddb) -> dict:
    try:
        t = ddb.describe_table(TableName=TABLE)["Table"]
        print(f"• Existing DynamoDB table: {TABLE} ({t['TableStatus']})")
    except ddb.exceptions.ResourceNotFoundException:
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                                    {"AttributeName": "SK", "KeyType": "RANGE"}],
                         Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
        print(f"• DynamoDB table created: {TABLE} (on demand, PK/SK)")
    ddb.get_waiter("table_exists").wait(TableName=TABLE)
    return ddb.describe_table(TableName=TABLE)["Table"]


def test(session, bucket: str) -> bool:
    import pandas as pd

    from app.store.ledger import DynamoDBLedger
    from app.tools.macro_tools import fx_usdcad, macro_snapshot
    from app.tools.risk_tools import RiskTools

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        names = cs.sync_from_s3(f"s3://{bucket}/{PREFIX}", tmp, client=session.client("s3"))
        print(f"• Downloaded from S3: {sorted(names)}")
        local = ROOT / "data" / "curated"
        for name in cs.TABLES:
            a, b = cs.read(tmp, name), pd.read_csv(local / f"{name}.csv", **cs.TABLES[name])
            same = a.shape == b.shape and a.equals(b) if name != "etf_holdings" else a.astype(str).equals(b.astype(str))
            ok &= same
            print(f"  {'✓' if same else '✗'} {name:<18} {a.shape}  identical to the repo CSV")
        rt_s3, rt_csv = RiskTools(tmp), RiskTools(local)
        w = {"XIU.TO": 0.24, "XBB.TO": 0.24, "ZST.TO": 0.08, "RY.TO": 0.08, "FTS.TO": 0.08, "JNJ": 0.08, "NVDA": 0.2}
        for label, f in [("risk contribution", lambda rt: rt.risk_contribution(w)),
                         ("metrics as_of 2025-06-30", lambda rt: rt.metrics(["NVDA"], as_of="2025-06-30"))]:
            x, y = f(rt_s3), f(rt_csv)
            same = x["evidence_id"] == y["evidence_id"] and x["output"] == y["output"]
            ok &= same
            print(f"  {'✓' if same else '✗'} RiskTools {label}: S3 Parquet == CSV  ({x['say'][:80]})")
        m = macro_snapshot(tmp, "2025-06-30")
        fx = fx_usdcad(tmp, "2025-12-30", start="2025-01-02")
        print(f"  ✓ macro point-in-time: {m['say']}")
        print(f"  ✓ FX point-in-time:    {fx['say']}")

    table = session.resource("dynamodb").Table(TABLE)
    led, run_id = DynamoDBLedger(table), f"selftest-{int(time.time())}"
    led.append(run_id, {"tool": "selftest", "evidence_id": "ev-selftest", "say": "round trip"})
    led.append(run_id, {"tool": "selftest", "evidence_id": "ev-selftest2"})
    back = led.records(run_id)
    same = [r["seq"] for r in back] == ["E-001", "E-002"] and led.evidence_ids(run_id) == ["ev-selftest", "ev-selftest2"]
    ok &= same
    print(f"  {'✓' if same else '✗'} DynamoDB ledger round trip ({len(back)} records, seq E-001/E-002)")
    for item in table.query(KeyConditionExpression="PK = :pk",
                            ExpressionAttributeValues={":pk": f"RUN#{run_id}"})["Items"]:
        table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    print("• Data layer OK" if ok else "✗ Data layer checks failed")
    return ok


def delete_all(session, bucket: str) -> None:
    s3, ddb = session.client("s3"), session.client("dynamodb")
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
        ddb.delete_table(TableName=TABLE)
        print(f"• Table deleted: {TABLE}")
    except ddb.exceptions.ResourceNotFoundException:
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
    bucket = f"alphaforge-curated-{account}-{region}"
    print(f"• Account: {account}  region: {region}")

    if args.delete:
        delete_all(session, bucket)
        return
    if not args.test_only:
        s3 = session.client("s3")
        ensure_bucket(s3, bucket, region)
        rows = publish_parquet(s3, bucket)
        t = ensure_table(session.client("dynamodb"))
        BUILD.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({"curated_s3": f"s3://{bucket}/{PREFIX}", "bucket": bucket,
                                     "table": TABLE, "table_arn": t["TableArn"], "tables": rows,
                                     "region": region}, indent=2))
        print(f"• State: {STATE}  (AF_CURATED_S3=s3://{bucket}/{PREFIX} for the quant Lambda)")
    if args.test or args.test_only:
        sys.exit(0 if test(session, bucket) else 1)


if __name__ == "__main__":
    main()
