"""Deploy ④ Context resolution (plan resolver + prompt loader) as a Lambda, with boto3 only.

    python scripts/deploy_context_lambda.py                 # create or update everything
    python scripts/deploy_context_lambda.py --test          # + invoke it and compare with the local result
    python scripts/deploy_context_lambda.py --test-only
    python scripts/deploy_context_lambda.py --delete        # tear everything down

Credentials: --profile, otherwise the AWS_* variables, otherwise the repo's .env file (git-ignored).

What the script does (links L4, L5 of docs/architecture.mmd):
1. S3 bucket "alphaforge-prompts-<account>-<region>": private, versioned, encrypted; uploads
   backend/app/prompts/explainer.*.md under prompts/ (the Git → S3 publication of L5);
2. IAM role "alphaforge-context-lambda": CloudWatch logs + read-only access to prompts/* (no Bedrock:
   this box never calls an LLM);
3. Lambda "alphaforge-context" (Python 3.12, AF_PROMPT_SOURCE=s3): {profile, request} →
   {execution_plan, prompt_ref}. Package = app/context + app/schemas + app/core + pydantic + pyyaml
   (boto3 ships with the Lambda runtime).

No public URL is created: the future dispatcher (L6) invokes it with lambda.invoke.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
BUILD = ROOT / "build"
FUNCTION = "alphaforge-context"
ROLE = "alphaforge-context-lambda"
RUNTIME = "python3.12"
PREFIX = "prompts/"
DEPS = ["pydantic>=2.7", "pyyaml>=6"]
TAGS = {"project": "alphaforge", "component": "context-resolution"}

TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def read_policy(bucket: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/{PREFIX}*"},
        # ListBucket lets S3 answer NoSuchKey (→ PromptNotFound) instead of AccessDenied for a missing template.
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{PREFIX}*"]}}},
    ]}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (no extra dependency). Never overrides variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


# ---------- Build ----------

def build_zip() -> Path:
    pkg = BUILD / "context_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    print("• Installing dependencies (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".gitkeep")
    for sub in ("context", "schemas", "core", "prompts"):  # only what box ④ imports or reads
        shutil.copytree(APP / sub, pkg / "app" / sub, ignore=ignore)

    zip_path = BUILD / "context_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


# ---------- S3 ----------

def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"• Existing bucket: s3://{bucket}")
    except s3.exceptions.ClientError:
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=bucket, **kwargs)
        s3.get_waiter("bucket_exists").wait(Bucket=bucket)
        print(f"• Bucket created: s3://{bucket}")
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})


def publish_prompts(s3, bucket: str) -> None:
    files = sorted((APP / "prompts").glob("explainer.*.md"))
    for f in files:
        s3.put_object(Bucket=bucket, Key=PREFIX + f.name, Body=f.read_bytes(),
                      ContentType="text/markdown; charset=utf-8")
    print(f"• Published {len(files)} templates to s3://{bucket}/{PREFIX}: {', '.join(f.name for f in files)}")


# ---------- IAM ----------

def ensure_role(iam, bucket: str) -> str:
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        print(f"• Existing role: {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
                              Description="AlphaForge Context resolution Lambda (box 4)",
                              Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        print(f"• Role created: {arn}")
        time.sleep(10)  # IAM propagation before Lambda can assume it
    iam.attach_role_policy(RoleName=ROLE,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE, PolicyName="read-prompts", PolicyDocument=json.dumps(read_policy(bucket)))
    return arn


# ---------- Lambda ----------

def deploy(lam, zip_path: Path, role_arn: str, bucket: str) -> None:
    code = zip_path.read_bytes()
    env = {"Variables": {"AF_PROMPT_SOURCE": "s3", "AF_PROMPT_BUCKET": bucket, "AF_PROMPT_PREFIX": PREFIX}}
    common = dict(Timeout=15, MemorySize=256, Environment=env)
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, Role=role_arn, **common)
        print(f"• Function updated: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):  # a brand-new role can take a few seconds before it can be assumed
            try:
                lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                                    Handler="app.context.handler.handler", Code={"ZipFile": code},
                                    Architectures=["x86_64"], Tags=TAGS,
                                    Description="AlphaForge Context resolution (box 4): Profile + Request -> "
                                                "ExecutionPlan + PromptRef", **common)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(5)
        print(f"• Function created: {FUNCTION}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)


# ---------- Smoke test ----------

def test(lam) -> None:
    """Invokes the deployed function with the repo examples and compares with the local result."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.context import LocalPromptSource, resolve_context
    from app.schemas import Profile, Request

    ex = APP / "schemas" / "examples"
    profile = json.loads((ex / "profile.example.json").read_text(encoding="utf-8"))
    request = json.loads((ex / "request.example.json").read_text(encoding="utf-8"))

    out = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps({"profile": profile, "request": request}).encode())
    remote = json.loads(out["Payload"].read())
    if out.get("FunctionError"):
        print("✗ Lambda error:", json.dumps(remote, indent=2))
        sys.exit(1)
    local = resolve_context(Profile.model_validate(profile), Request.model_validate(request), LocalPromptSource())
    print(json.dumps(remote, indent=2))
    if remote != local:
        print("✗ The deployed result differs from the local one (stale package or prompts?)")
        sys.exit(1)
    print("✓ Deployed result identical to the local result (plan + prompt_ref, sha256 included).")

    bad = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps(
        {"profile": profile, "request": {**request, "task": "buy_now"}}).encode())
    err = json.loads(bad["Payload"].read())
    if not bad.get("FunctionError") or err.get("errorType") != "ValidationError":
        print("✗ An invalid Request was not rejected:", err)
        sys.exit(1)
    print("✓ Invalid Request rejected (ValidationError).")


# ---------- Teardown ----------

def delete_all(session, bucket: str) -> None:
    lam, iam, s3 = session.client("lambda"), session.client("iam"), session.client("s3")
    try:
        lam.delete_function(FunctionName=FUNCTION)
        print(f"• Function deleted: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        pass
    try:
        for p in iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=ROLE, PolicyArn=p["PolicyArn"])
        for name in iam.list_role_policies(RoleName=ROLE)["PolicyNames"]:
            iam.delete_role_policy(RoleName=ROLE, PolicyName=name)
        iam.delete_role(RoleName=ROLE)
        print(f"• Role deleted: {ROLE}")
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=bucket)
        print(f"• Bucket deleted: s3://{bucket}")
    except s3.exceptions.NoSuchBucket:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="AWS profile; otherwise AWS_* variables, otherwise .env")
    ap.add_argument("--region", default=None, help="default: AWS_REGION, otherwise us-east-1")
    ap.add_argument("--test", action="store_true", help="invoke the function after deploying")
    ap.add_argument("--test-only", action="store_true", help="only invoke the already deployed function")
    ap.add_argument("--delete", action="store_true", help="delete the function, role and bucket")
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
    bucket = f"alphaforge-prompts-{account}-{region}"
    print(f"• Account: {account}  region: {region}")

    if args.delete:
        delete_all(session, bucket)
        return
    lam = session.client("lambda")
    if not args.test_only:
        zip_path = build_zip()
        s3 = session.client("s3")
        ensure_bucket(s3, bucket, region)
        publish_prompts(s3, bucket)
        role_arn = ensure_role(session.client("iam"), bucket)
        deploy(lam, zip_path, role_arn, bucket)
        print(f"• Ready: {FUNCTION} in {region}, prompts in s3://{bucket}/{PREFIX}")
    if args.test or args.test_only:
        test(lam)


if __name__ == "__main__":
    main()
