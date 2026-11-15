"""Deploy the Intake (box 3) as a Lambda function, with boto3 only (no SAM, no Docker needed).

    python scripts/deploy_intake_lambda.py --profile workshop            # create or update the function
    python scripts/deploy_intake_lambda.py --profile workshop --test     # + invoke it with Robert's demo
    python scripts/deploy_intake_lambda.py --profile workshop --test-only
    python scripts/deploy_intake_lambda.py --test         # credentials from the AWS_* variables or the repo .env

What the script does:
1. builds build/intake_lambda.zip: backend/app + prompt + assets_universe.csv + pydantic and anthropic
   (Linux x86_64 wheels for Python 3.12; boto3/botocore already ship with the Lambda runtime);
2. creates the IAM role "alphaforge-intake-lambda" (CloudWatch logs + bedrock:InvokeModel), unless --role-arn;
3. creates or updates the function "alphaforge-intake" (Python 3.12, AF_INTAKE_MODE=bedrock).

No public URL is created: call it with boto3 (lambda.invoke), the console, or the API (box 2, AF_INTAKE_MODE=lambda:
{"op": "extract"}, see backend/app/api/intake_handler.py). --test checks both operations.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import (  # noqa: E402
    BASIC_EXECUTION, BUILD, LAMBDA_TRUST, ROOT, ensure_role, load_dotenv, pip_install_linux, zip_dir,
)

FUNCTION = "alphaforge-intake"
ROLE = "alphaforge-intake-lambda"
RUNTIME = "python3.12"
DEPS = ["pydantic>=2.7", "anthropic>=2.0"]
TAGS = {"project": "alphaforge", "component": "intake"}
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
    pip_install_linux(pkg, DEPS)
    shutil.copytree(ROOT / "backend" / "app", pkg / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".gitkeep"))
    (pkg / "data").mkdir()
    shutil.copy(ROOT / "data" / "curated" / "assets_universe.csv", pkg / "data" / "assets_universe.csv")
    ext = ROOT / "data" / "curated" / "assets_universe_ext.csv"   # S&P 500 extension, read next to it
    if ext.exists():
        shutil.copy(ext, pkg / "data" / ext.name)

    return zip_dir(pkg, BUILD / "intake_lambda.zip")


def ensure_intake_role(iam) -> str:
    arn, created = ensure_role(iam, ROLE, LAMBDA_TRUST, "Fitfolio Intake Lambda (box 3)", TAGS)
    if created:
        time.sleep(10)  # IAM propagation before Lambda can assume it
    iam.attach_role_policy(RoleName=ROLE, PolicyArn=BASIC_EXECUTION)
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
                                    Description="Fitfolio Intake (box 3): free text -> Profile + Request JSON")
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

    # What the API sends with AF_INTAKE_MODE=lambda: the model step only (intake.backends.LambdaBackend).
    event = {"op": "extract", "text": "I'm a student. Is 20% in Nvidia too risky?",
             "known_profile": {"risk_band": "moderate", "experience": "beginner"}}
    out = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps(event).encode())
    body = json.loads(json.loads(out["Payload"].read())["body"])
    if out.get("FunctionError") or "draft" not in body:
        print("✗ extract operation failed:", body)
        sys.exit(1)
    d = body["draft"]
    print(f"✓ extract operation: task={d['request']['task']} learning={d['profile']['learning']} "
          f"positions={d['request']['positions']}")


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
        load_dotenv()
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    print(f"• Account: {session.client('sts').get_caller_identity()['Account']}  region: {args.region}")
    lam = session.client("lambda")
    if not args.test_only:
        zip_path = build_zip()
        role_arn = args.role_arn or ensure_intake_role(session.client("iam"))
        deploy(lam, zip_path, role_arn, args.region)
    if args.test or args.test_only:
        test(lam)


if __name__ == "__main__":
    main()
