"""Deploy the Intake (box 3) as a Lambda function, with boto3 only (no SAM, no Docker needed).

    python scripts/deploy_intake_lambda.py --profile workshop            # create or update the function
    python scripts/deploy_intake_lambda.py --profile workshop --test     # + invoke it with Robert's demo
    python scripts/deploy_intake_lambda.py --profile workshop --test-only

What the script does:
1. builds build/intake_lambda.zip: backend/app + prompt + assets_universe.csv + pydantic and anthropic
   (Linux x86_64 wheels for Python 3.12; boto3/botocore already ship with the Lambda runtime);
2. creates the IAM role "alphaforge-intake-lambda" (CloudWatch logs + bedrock:InvokeModel), unless --role-arn;
3. creates or updates the function "alphaforge-intake" (Python 3.12, AF_INTAKE_MODE=bedrock).

No public URL is created: call it with boto3 (lambda.invoke), the console, or the future API (box 2).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
FUNCTION = "alphaforge-intake"
ROLE = "alphaforge-intake-lambda"
RUNTIME = "python3.12"
DEPS = ["pydantic>=2.7", "anthropic>=2.0"]

TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
BEDROCK_POLICY = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    # "us." inference profile + the foundation model in every US region it routes to.
    "Resource": ["arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-haiku-5-5",
                 "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5"],
}]}

TEST_EVENT = {"actor_id": "robert",
              "text": "I'm cautious, horizon of 3 to 5 years, advanced experience, $400,000, no crypto. "
                      "I'm thinking of putting 20% in Nvidia, everyone is talking about it."}


def build_zip() -> Path:
    pkg = BUILD / "intake_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    print("• Installing dependencies (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    shutil.copytree(ROOT / "backend" / "app", pkg / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".gitkeep"))
    (pkg / "data").mkdir()
    shutil.copy(ROOT / "data" / "curated" / "assets_universe.csv", pkg / "data" / "assets_universe.csv")

    zip_path = BUILD / "intake_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


def ensure_role(iam) -> str:
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        print(f"• Existing role: {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
                              Description="AlphaForge Intake Lambda (box 3)")["Role"]["Arn"]
        print(f"• Role created: {arn}")
        time.sleep(10)  # IAM propagation before Lambda can assume it
    iam.attach_role_policy(RoleName=ROLE,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE, PolicyName="bedrock-invoke-haiku", PolicyDocument=json.dumps(BEDROCK_POLICY))
    return arn


def deploy(lam, zip_path: Path, role_arn: str, region: str) -> None:
    code = zip_path.read_bytes()
    env = {"Variables": {"AF_INTAKE_MODE": "bedrock", "AF_UNIVERSE_CSV": "/var/task/data/assets_universe.csv"}}
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, Environment=env, Timeout=60, MemorySize=512)
        print(f"• Function updated: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):  # a brand-new role can take a few seconds before it can be assumed
            try:
                lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                                    Handler="app.api.intake_handler.handler", Code={"ZipFile": code},
                                    Timeout=60, MemorySize=512, Environment=env,
                                    Description="AlphaForge Intake (box 3): free text -> Profile + Request JSON")
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(5)
        print(f"• Function created: {FUNCTION}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    print(f"• Ready in {region}.")


def test(lam) -> None:
    out = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps(TEST_EVENT).encode())
    payload = json.loads(out["Payload"].read())
    if out.get("FunctionError"):
        print("✗ Lambda error:", json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(f"• Test (HTTP {payload['statusCode']}):")
    print(json.dumps(json.loads(payload["body"]), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="AWS profile (e.g. workshop); otherwise the AWS_* environment variables")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--role-arn", help="existing IAM role, if the account does not allow creating one")
    ap.add_argument("--test", action="store_true", help="invoke the function after deploying")
    ap.add_argument("--test-only", action="store_true", help="only invoke the already deployed function")
    args = ap.parse_args()

    try:  # Windows: system certificates for botocore
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    if not args.profile:
        from deploy_context_lambda import load_dotenv  # AWS_* from the git-ignored .env, never overrides

        load_dotenv()
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    print(f"• Account: {session.client('sts').get_caller_identity()['Account']}  region: {args.region}")
    lam = session.client("lambda")
    if not args.test_only:
        zip_path = build_zip()
        role_arn = args.role_arn or ensure_role(session.client("iam"))
        deploy(lam, zip_path, role_arn, args.region)
    if args.test or args.test_only:
        test(lam)


if __name__ == "__main__":
    main()
