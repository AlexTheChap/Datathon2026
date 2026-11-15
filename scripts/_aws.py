"""Helpers shared by the deploy scripts: .env, IAM roles, S3 buckets, Lambda packages and invocations.

boto3 clients are passed in by the caller; nothing touches AWS (or the network) on import.
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.core.dotenv import load_dotenv  # noqa: E402,F401  (re-exported: AWS_* from the git-ignored .env, never overrides)

BASIC_EXECUTION = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def trust_policy(service: str, source_account: str | None = None) -> dict:
    """Assume-role policy for an AWS service principal, optionally limited to calls from our own account."""
    statement = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if source_account:
        statement["Condition"] = {"StringEquals": {"aws:SourceAccount": source_account}}
    return {"Version": "2012-10-17", "Statement": [statement]}


LAMBDA_TRUST = trust_policy("lambda.amazonaws.com")


def ensure_role(iam, name: str, trust: dict, description: str, tags: dict[str, str]) -> tuple[str, bool]:
    """(role ARN, created now?). The caller attaches the policies, and waits for IAM propagation when created."""
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"• Existing role: {arn}")
        return arn, False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description,
                              Tags=tag_list(tags))["Role"]["Arn"]
        print(f"• Role created: {arn}")
        return arn, True


def ensure_bucket(s3, bucket: str, region: str, tags: dict[str, str] | None = None, versioning: bool = False) -> None:
    """Private, encrypted bucket (created when missing); the settings are re-applied on every run."""
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
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    if versioning:
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    if tags:
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": tag_list(tags)})


def pip_install_linux(target: Path, deps: list[str], what: str = "dependencies") -> None:
    """Installs `deps` into `target` as Linux x86_64 wheels for the Lambda runtime (Python 3.12)."""
    print(f"• Installing {what} (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(target),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *deps], check=True)


def zip_dir(pkg: Path, zip_path: Path) -> Path:
    """Zips the package folder (no __pycache__ / .pyc) and prints its size."""
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


def invoke(lam, name: str, payload: dict) -> dict:
    """Synchronous Lambda invocation; raises with the function's error body when it failed."""
    r = lam.invoke(FunctionName=name, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    body = json.loads(r["Payload"].read() or b"null")
    if r.get("FunctionError"):
        raise RuntimeError(f"{name} failed: {body}")
    return body
