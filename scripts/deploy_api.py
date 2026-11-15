"""Deploy box ② (the FastAPI API, with the box ⑤ agents in the same image) to AWS Lambda, with boto3 only.

    python scripts/deploy_api.py            # build the image in AWS CodeBuild, create / update the Lambda + its URL
    python scripts/deploy_api.py --test     # + GET /health and GET /v1/me (must be 401 without a token)
    python scripts/deploy_api.py --delete   # tear down the Lambda, its URL, roles, CodeBuild project, ECR repo

Then: python scripts/deploy_frontend.py --api-url <the URL printed here>   (CloudFront routes /v1/* to it)

Why a container image: the API runs the agents in-process (strands, Bedrock, cvxpy/scipy, pandas): ~680 MB of
packages, far above the 250 MB of a zip Lambda. No Docker needed on this machine: CodeBuild builds the image
from a source zip on S3 and pushes it to ECR.

What it creates (names "alphaforge-api"):
1. S3 bucket "alphaforge-build-<account>-<region>" (private, encrypted): the build context zip.
2. ECR repository "alphaforge-api" (keeps the 5 newest images) and CodeBuild project "alphaforge-api"
   (+ role "alphaforge-api-codebuild": logs, read the zip, push to that repository only).
3. Lambda "alphaforge-api" (image, 15 min, 3 GB) + role "alphaforge-api-lambda": logs; DynamoDB on the
   "alphaforge" table; Bedrock models (agents, intake, explainer) and the Knowledge Base; the AgentCore Gateway
   of the quant tools; invoke ITSELF (AF_DISPATCH=lambda: an analysis runs in a second, asynchronous invocation
   so no request waits minutes; the page polls GET /v1/runs/{id}).
4. A Function URL (auth NONE): the API verifies every caller's Cognito ID token itself (AF_AUTH_MODE=cognito,
   app/api/auth.py); only /health and the static config answer without one.
Data: the curated CSVs and market snapshot of data/ are baked into the image (read-only).
State: build/api.json (function, url).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import BUILD, LAMBDA_TRUST, ROOT, ensure_bucket, ensure_role, load_dotenv, trust_policy  # noqa: E402

NAME = "alphaforge-api"
LAMBDA_ROLE, BUILD_ROLE = f"{NAME}-lambda", f"{NAME}-codebuild"
TAGS = {"project": "alphaforge", "component": "api"}
STATE = BUILD / "api.json"
DATA_LAYER = BUILD / "data_layer.json"
DATA_DIRS = ["data/curated", "data/external", "data/market"]   # read by the agents and tools (no data/raw, data/runs)
CODEBUILD_TRUST = trust_policy("codebuild.amazonaws.com")

DOCKERFILE = """FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt
COPY backend ${LAMBDA_TASK_ROOT}/backend
COPY data ${LAMBDA_TASK_ROOT}/data
ENV PYTHONPATH=${LAMBDA_TASK_ROOT}/backend
CMD ["app.api.app.handler"]
"""

BUILDSPEC = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $REPO_HOST
  build:
    commands:
      - docker build --provenance=false -t $REPO_URI:latest .
  post_build:
    commands:
      - docker push $REPO_URI:latest
"""


def requirements() -> str:
    """pyproject dependencies + the api and bedrock extras (truststore is Windows-only: dropped)."""
    p = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = p["dependencies"] + p["optional-dependencies"]["api"] + p["optional-dependencies"]["bedrock"]
    return "\n".join(d for d in deps if not d.startswith("truststore")) + "\n"


def source_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Dockerfile", DOCKERFILE)
        z.writestr("buildspec.yml", BUILDSPEC)
        z.writestr("requirements.txt", requirements())
        roots = [ROOT / "backend" / "app"] + [ROOT / d for d in DATA_DIRS]
        for base in roots:
            for f in sorted(base.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc", ".pyo"):
                    z.write(f, f.relative_to(ROOT).as_posix())
    return buf.getvalue()


# ---------- build: S3 -> CodeBuild -> ECR ----------

def ensure_repo(ecr) -> str:
    try:
        repo = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]
    except ecr.exceptions.RepositoryNotFoundException:
        repo = ecr.create_repository(repositoryName=NAME, imageScanningConfiguration={"scanOnPush": True},
                                     tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["repository"]
        ecr.put_lifecycle_policy(repositoryName=NAME, lifecyclePolicyText=json.dumps({"rules": [{
            "rulePriority": 1, "description": "keep the 5 newest images",
            "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 5},
            "action": {"type": "expire"}}]}))
        print(f"• ECR repository created: {repo['repositoryUri']}")
    return repo["repositoryUri"]


def build_image(session, account: str, region: str) -> str:
    s3, ecr, cb, iam = (session.client(s) for s in ("s3", "ecr", "codebuild", "iam"))
    bucket = f"alphaforge-build-{account}-{region}"
    ensure_bucket(s3, bucket, region, TAGS)
    data = source_zip()
    s3.put_object(Bucket=bucket, Key=f"{NAME}/source.zip", Body=data)
    print(f"• Build context uploaded: s3://{bucket}/{NAME}/source.zip ({len(data) / 1e6:.1f} MB)")
    uri = ensure_repo(ecr)

    role_arn, new = ensure_role(iam, BUILD_ROLE, CODEBUILD_TRUST, "Fitfolio API image build (CodeBuild)", TAGS)
    iam.put_role_policy(RoleName=BUILD_ROLE, PolicyName="build", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{NAME}*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
             "Resource": f"arn:aws:s3:::{bucket}/{NAME}/*"},
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
                                           "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                                           "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
             "Resource": f"arn:aws:ecr:{region}:{account}:repository/{NAME}"}]}))
    project = dict(
        name=NAME, source={"type": "S3", "location": f"{bucket}/{NAME}/source.zip"}, artifacts={"type": "NO_ARTIFACTS"},
        environment={"type": "LINUX_CONTAINER", "image": "aws/codebuild/amazonlinux2-x86_64-standard:5.0",
                     "computeType": "BUILD_GENERAL1_MEDIUM", "privilegedMode": True,
                     "environmentVariables": [{"name": "REPO_URI", "value": uri},
                                              {"name": "REPO_HOST", "value": uri.split("/")[0]}]},
        serviceRole=role_arn, timeoutInMinutes=30)
    if cb.batch_get_projects(names=[NAME])["projects"]:
        cb.update_project(**project)
    else:
        for attempt in range(8):  # a brand-new role takes a few seconds before CodeBuild can assume it
            try:
                cb.create_project(**project, tags=[{"key": k, "value": v} for k, v in TAGS.items()])
                break
            except cb.exceptions.InvalidInputException as e:
                if "role" not in str(e).lower() or attempt == 7:
                    raise
                time.sleep(5)
        print(f"• CodeBuild project created: {NAME}")

    build_id = cb.start_build(projectName=NAME)["build"]["id"]
    print(f"• Image build started ({build_id}); 5 to 10 minutes...", flush=True)
    phase = None
    while True:
        b = cb.batch_get_builds(ids=[build_id])["builds"][0]
        if b["currentPhase"] != phase:
            phase = b["currentPhase"]
            print(f"    {phase}", flush=True)
        if b["buildStatus"] != "IN_PROGRESS":
            break
        time.sleep(15)
    if b["buildStatus"] != "SUCCEEDED":
        logs = b.get("logs", {}).get("deepLink", "")
        raise SystemExit(f"Image build {b['buildStatus']}. Logs: {logs}")
    digest = ecr.describe_images(repositoryName=NAME, imageIds=[{"imageTag": "latest"}])["imageDetails"][0]["imageDigest"]
    print(f"• Image pushed: {uri}@{digest[:19]}…")
    return f"{uri}@{digest}"


# ---------- Lambda + Function URL ----------

def lambda_policy(account: str, region: str, table: str) -> dict:
    fn_arn = f"arn:aws:lambda:{region}:{account}:function:{NAME}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{NAME}:*"},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                       "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
                                       "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem"],
         "Resource": [f"arn:aws:dynamodb:{region}:{account}:table/{table}",
                      f"arn:aws:dynamodb:{region}:{account}:table/{table}/index/*"]},
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                                       "bedrock:Converse", "bedrock:ConverseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                      f"arn:aws:bedrock:*:{account}:inference-profile/*"]},
        {"Effect": "Allow", "Action": ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
         "Resource": f"arn:aws:bedrock:{region}:{account}:knowledge-base/*"},
        {"Effect": "Allow", "Action": ["bedrock:ApplyGuardrail"],
         "Resource": f"arn:aws:bedrock:{region}:{account}:guardrail/*"},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/*"},
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_arn},   # AF_DISPATCH=lambda
    ]}


def lambda_env(table: str) -> dict:
    env = {"AF_STORE": "dynamodb", "AF_TABLE": table, "AF_DISPATCH": "lambda", "AF_AUTH_MODE": "cognito",
           "AF_INTAKE_MODE": "bedrock", "AF_SERVE_FRONTEND": "0"}
    for k in ("AF_GATEWAY_URL", "AF_KB_ID", "AF_GUARDRAIL_ID", "AF_GUARDRAIL_VERSION", "AF_MEMORY_ID",
              "AF_MODEL_ORCHESTRATOR", "AF_MODEL_SUBAGENT", "AF_MODEL_EXPLAINER", "AF_INTAKE_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def deploy_lambda(lam, image: str, role_arn: str, env: dict) -> str:
    cfg = dict(Timeout=900, MemorySize=3008, Environment={"Variables": env})
    try:
        lam.get_function(FunctionName=NAME)
        lam.update_function_code(FunctionName=NAME, ImageUri=image)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, **cfg)
        print(f"• Lambda updated: {NAME}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a brand-new role takes a few seconds before Lambda can assume it
            try:
                lam.create_function(FunctionName=NAME, PackageType="Image", Code={"ImageUri": image}, Role=role_arn,
                                    Description="Fitfolio API (box 2) + agents (box 5)", Tags=TAGS, **cfg)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 9:
                    raise
                time.sleep(6)
        print(f"• Lambda created: {NAME}")
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME)
    lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
    # An analysis is never retried by Lambda's async machinery (it would bill the agents twice).
    lam.put_function_event_invoke_config(FunctionName=NAME, MaximumRetryAttempts=0)
    return ensure_url(lam)


def ensure_url(lam) -> str:
    try:
        url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE")["FunctionUrl"]
        print("• Function URL created")
    for sid, action, extra in (("public-url", "lambda:InvokeFunctionUrl", {"FunctionUrlAuthType": "NONE"}),
                               ("public-url-invoke", "lambda:InvokeFunction", {"InvokedViaFunctionUrl": True})):
        try:
            lam.add_permission(FunctionName=NAME, StatementId=sid, Action=action, Principal="*", **extra)
        except lam.exceptions.ResourceConflictException:
            pass
    return url.rstrip("/")


# ---------- test / delete ----------

def test(url: str) -> bool:
    ok = True
    for path, want in (("/health", 200), ("/v1/me", 401)):
        try:
            with urllib.request.urlopen(url + path, timeout=60) as r:
                code, body = r.status, r.read()[:200]
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()[:200]
        good = code == want
        ok &= good
        print(f"  {'OK  ' if good else 'FAIL'} GET {path} -> {code} (expected {want}) {body.decode(errors='replace')}")
    return ok


def delete_all(session) -> None:
    lam, iam, cb, ecr = (session.client(s) for s in ("lambda", "iam", "codebuild", "ecr"))
    for call in (lambda: lam.delete_function_url_config(FunctionName=NAME), lambda: lam.delete_function(FunctionName=NAME),
                 lambda: cb.delete_project(name=NAME), lambda: ecr.delete_repository(repositoryName=NAME, force=True)):
        try:
            call()
        except Exception as e:
            print(f"  (skipped: {type(e).__name__})")
    for role in (LAMBDA_ROLE, BUILD_ROLE):
        try:
            for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
            iam.delete_role(RoleName=role)
        except iam.exceptions.NoSuchEntityException:
            pass
    STATE.unlink(missing_ok=True)
    print("• Deleted (the build bucket is kept)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="AWS profile; otherwise AWS_* variables, otherwise .env")
    ap.add_argument("--region", default=None, help="default: AWS_REGION, otherwise us-east-1")
    ap.add_argument("--test", action="store_true", help="after deploying: /health and an unauthenticated /v1/me")
    ap.add_argument("--test-only", action="store_true")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    try:  # Windows: system certificates for botocore (before boto3 is imported)
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
    print(f"• Account: {account}  region: {region}")
    if args.delete:
        delete_all(session)
        return
    if args.test_only:
        url = json.loads(STATE.read_text())["url"]
        raise SystemExit(0 if test(url) else 1)

    table = json.loads(DATA_LAYER.read_text())["table"] if DATA_LAYER.exists() else "alphaforge"
    image = build_image(session, account, region)
    iam = session.client("iam")
    role_arn, _ = ensure_role(iam, LAMBDA_ROLE, LAMBDA_TRUST, "Fitfolio API Lambda (box 2 + box 5 agents)", TAGS)
    iam.put_role_policy(RoleName=LAMBDA_ROLE, PolicyName="api",
                        PolicyDocument=json.dumps(lambda_policy(account, region, table)))
    url = deploy_lambda(session.client("lambda"), image, role_arn, lambda_env(table))
    BUILD.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"function": NAME, "url": url, "image": image, "region": region}, indent=2))
    print(f"• API URL: {url}")
    print(f"  next: python scripts/deploy_frontend.py --api-url {url}")
    if args.test:
        time.sleep(5)
        raise SystemExit(0 if test(url) else 1)


if __name__ == "__main__":
    main()
