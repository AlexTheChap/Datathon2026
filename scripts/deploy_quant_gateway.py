"""Deploy box ⑥ quant tools behind an AgentCore Gateway (MCP), with boto3 only.

    python scripts/deploy_quant_gateway.py              # create or update everything
    python scripts/deploy_quant_gateway.py --test       # + call every tool through the Gateway
    python scripts/deploy_quant_gateway.py --test-only
    python scripts/deploy_quant_gateway.py --delete     # tear everything down

Credentials: --profile, otherwise the AWS_* variables, otherwise the repo's .env file (git-ignored).

What it creates (all names prefixed "alphaforge-quant"):
1. build/quant_lambda.zip: app/tools (risk_tools, quant_gateway, curated_store, macro_tools)
   + numpy/pandas/fastparquet (Linux x86_64 wheels, Python 3.12). Data: the S3 curated Parquet published by
   scripts/deploy_data_layer.py (env AF_CURATED_S3, read-only IAM), or the CSVs bundled in the zip without it.
2. IAM role "alphaforge-quant-lambda" (CloudWatch logs only) and Lambda "alphaforge-quant-tools"
   (handler app.tools.quant_gateway.handler).
3. IAM role "alphaforge-quant-gateway" (trusted by bedrock-agentcore, may invoke ONLY that Lambda).
4. AgentCore Gateway "alphaforge-quant-gateway": protocol MCP, authorizer AWS_IAM (SigV4).
5. Three Gateway targets on the same Lambda: metrics · risk · macro
   (tool schemas from app.tools.quant_gateway.TARGETS; removed targets are pruned on redeploy).
The Gateway URL is written to build/quant_gateway.json and to .env as AF_GATEWAY_URL.
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
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.tools.quant_gateway import TARGETS  # noqa: E402
from deploy_context_lambda import load_dotenv  # noqa: E402

FUNCTION = "alphaforge-quant-tools"
LAMBDA_ROLE = "alphaforge-quant-lambda"
GATEWAY = "alphaforge-quant-gateway"
GATEWAY_ROLE = "alphaforge-quant-gateway"
RUNTIME = "python3.12"
DEPS = ["numpy", "pandas", "python-dateutil<3", "fastparquet"]  # dateutil<3: same major as the runtime botocore
CSVS = ["returns_daily_cad.csv", "prices_cad.csv", "assets_universe.csv", "etf_holdings.csv",
        "fx_usdcad_filled.csv", "macro_daily_asof.csv"]   # bundled only when the S3 data layer is absent
DATA_LAYER = BUILD / "data_layer.json"   # written by scripts/deploy_data_layer.py (S3 curated Parquet, L14)


def curated_s3() -> str | None:
    return json.loads(DATA_LAYER.read_text())["curated_s3"] if DATA_LAYER.exists() else None
TAGS = {"project": "alphaforge", "component": "quant-gateway"}
STATE = BUILD / "quant_gateway.json"

LAMBDA_TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def gateway_trust(account: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}


# ---------- Build ----------

def build_zip() -> Path:
    pkg = BUILD / "quant_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    (pkg / "app" / "tools").mkdir(parents=True)
    print("• Installing numpy/pandas (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    for f in ("risk_tools.py", "quant_gateway.py", "curated_store.py", "macro_tools.py"):  # what the Lambda imports
        shutil.copy(ROOT / "backend" / "app" / "tools" / f, pkg / "app" / "tools" / f)
    if curated_s3() is None:  # no S3 data layer: ship the CSVs inside the zip (previous behaviour)
        (pkg / "data" / "curated").mkdir(parents=True)
        for f in CSVS:
            shutil.copy(ROOT / "data" / "curated" / f, pkg / "data" / "curated" / f)
    zip_path = BUILD / "quant_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc"):
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


# ---------- IAM ----------

def ensure_role(iam, name: str, trust: dict, description: str) -> tuple[str, bool]:
    try:
        return iam.get_role(RoleName=name)["Role"]["Arn"], False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description,
                              Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        print(f"• Role created: {arn}")
        return arn, True


# ---------- Lambda ----------

def deploy_lambda(lam, zip_path: Path, role_arn: str) -> str:
    code = zip_path.read_bytes()
    cfg = dict(Timeout=60, MemorySize=1024,
               Environment={"Variables": {"AF_CURATED_S3": curated_s3()} if curated_s3()
                            else {"AF_CURATED_DIR": "/var/task/data/curated"}})
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, **cfg)
        print(f"• Lambda updated: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(8):  # a brand-new role takes a few seconds before Lambda can assume it
            try:
                lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                                    Handler="app.tools.quant_gateway.handler", Code={"ZipFile": code},
                                    Description="AlphaForge quant tools (box 6) behind the AgentCore Gateway",
                                    Tags=TAGS, **cfg)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 7:
                    raise
                time.sleep(5)
        print(f"• Lambda created: {FUNCTION}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    return lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]


# ---------- Gateway ----------

def _wait(get, what: str) -> dict:
    for _ in range(60):
        obj = get()
        status = obj.get("status")
        if status == "READY":
            return obj
        if status and "FAIL" in status:
            raise RuntimeError(f"{what}: {status} {obj.get('statusReasons')}")
        time.sleep(3)
    raise TimeoutError(f"{what} not READY")


def find_gateway(ac) -> dict | None:
    for page in ac.get_paginator("list_gateways").paginate():
        for g in page.get("items", []):
            if g["name"] == GATEWAY:
                return ac.get_gateway(gatewayIdentifier=g["gatewayId"])
    return None


def ensure_gateway(ac, role_arn: str) -> dict:
    gw = find_gateway(ac)
    if gw is None:
        for attempt in range(8):  # new role propagation
            try:
                gw = ac.create_gateway(name=GATEWAY, roleArn=role_arn, protocolType="MCP", authorizerType="AWS_IAM",
                                       description="AlphaForge quant tools (metrics, risk, scenarios, optimizer)",
                                       tags=TAGS)
                break
            except ac.exceptions.ValidationException as e:
                if "role" not in str(e).lower() or attempt == 7:
                    raise
                time.sleep(5)
        print(f"• Gateway created: {gw['gatewayId']}")
    else:
        print(f"• Existing gateway: {gw['gatewayId']}")
    gid = gw["gatewayId"]
    return _wait(lambda: ac.get_gateway(gatewayIdentifier=gid), "gateway")


def ensure_targets(ac, gateway_id: str, lambda_arn: str) -> None:
    existing = {}
    for page in ac.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id):
        existing.update({t["name"]: t["targetId"] for t in page.get("items", [])})
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    for name, tools in TARGETS.items():
        cfg = {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}}}
        desc = f"AlphaForge {name} tools: {', '.join(t['name'] for t in tools)}"
        if name in existing:
            tid = existing[name]
            ac.update_gateway_target(gatewayIdentifier=gateway_id, targetId=tid, name=name, description=desc,
                                     targetConfiguration=cfg, credentialProviderConfigurations=creds)
        else:
            tid = ac.create_gateway_target(gatewayIdentifier=gateway_id, name=name, description=desc,
                                           targetConfiguration=cfg,
                                           credentialProviderConfigurations=creds)["targetId"]
        _wait(lambda: ac.get_gateway_target(gatewayIdentifier=gateway_id, targetId=tid), f"target {name}")
        print(f"• Target ready: {name} -> {[t['name'] for t in tools]}")
    for name, tid in existing.items():  # a target removed from TARGETS: delete it, never leave a zombie
        if name not in TARGETS:
            ac.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=tid)
            print(f"• Stale target removed: {name}")


def save_url(url: str, gateway_id: str, region: str) -> None:
    BUILD.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"gateway_id": gateway_id, "gateway_url": url, "region": region}, indent=2))
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith("AF_GATEWAY_URL=")] + [f"AF_GATEWAY_URL={url}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["AF_GATEWAY_URL"] = url
    print(f"• Gateway URL: {url}  (saved in .env as AF_GATEWAY_URL)")


# ---------- Test ----------

ROBERT = {"XIU.TO": 0.3, "XBB.TO": 0.3, "ZST.TO": 0.1, "RY.TO": 0.1, "FTS.TO": 0.1, "JNJ": 0.1}
ROBERT_NVDA = {**{k: round(v * 0.8, 4) for k, v in ROBERT.items()}, "NVDA": 0.2}
TEST_CALLS = [
    ("metrics___metrics", {"tickers": ["NVDA", "XBB.TO"]}),
    ("metrics___correlation", {"tickers": ["XIU.TO", "XBB.TO"], "start": "2022-01-01", "end": "2022-12-31"}),
    ("risk___risk_contribution", {"weights": ROBERT_NVDA}),
    ("risk___concentration", {"weights": ROBERT_NVDA}),
    ("macro___macro_snapshot", {"as_of": "2022-06-30"}),
    ("macro___fx_usdcad", {"as_of": "2022-12-30", "start": "2022-01-03"}),
]


def test(session, url: str) -> bool:
    from app.tools import quant_gateway as local
    from app.tools.gateway_client import GatewayClient

    client = GatewayClient(url, session=session)
    info = client.initialize()
    print(f"• MCP initialize: server={info.get('serverInfo')} protocol={info.get('protocolVersion')}")
    names = sorted(t["name"] for t in client.list_tools())
    print(f"• tools/list ({len(names)}): {names}")
    ok = True
    for name, args in TEST_CALLS:
        t0 = time.time()
        remote = client.call_tool(name, args)
        ms = (time.time() - t0) * 1000
        ref = local.call(name, args)
        same = isinstance(remote, dict) and remote.get("evidence_id") == ref.get("evidence_id") \
            and remote.get("output") == ref.get("output")
        ok &= same
        mark = "✓" if same else "✗"
        print(f"  {mark} {name:<26} {ms:6.0f} ms  {remote.get('evidence_id') if isinstance(remote, dict) else '?'}"
              f"  {(remote.get('say') or remote.get('error')) if isinstance(remote, dict) else remote}")
    bad = client.call_tool("metrics___metrics", {"tickers": ["NVDA"], "start": "1929-01-01", "end": "1929-12-31"})
    print(f"  {'✓' if 'error' in bad else '✗'} bad window returns an error the agent can fix: {bad.get('error')}")
    ok &= "error" in bad
    print("• Gateway == local for every tool" if ok else "✗ Gateway and local results differ")
    return ok


# ---------- Delete ----------

def delete_all(session) -> None:
    ac, lam, iam = session.client("bedrock-agentcore-control"), session.client("lambda"), session.client("iam")
    gw = find_gateway(ac)
    if gw:
        gid = gw["gatewayId"]
        for page in ac.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gid):
            for t in page.get("items", []):
                ac.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
        for _ in range(40):
            if not ac.list_gateway_targets(gatewayIdentifier=gid).get("items"):
                break
            time.sleep(3)
        ac.delete_gateway(gatewayIdentifier=gid)
        print(f"• Gateway deleted: {gid}")
    try:
        lam.delete_function(FunctionName=FUNCTION)
        print(f"• Lambda deleted: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        pass
    for role in (GATEWAY_ROLE, LAMBDA_ROLE):
        try:
            for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
            iam.delete_role(RoleName=role)
            print(f"• Role deleted: {role}")
        except iam.exceptions.NoSuchEntityException:
            pass
    STATE.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="AWS profile; otherwise AWS_* variables, otherwise .env")
    ap.add_argument("--region", default=None, help="default: AWS_REGION, otherwise us-east-1")
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
    print(f"• Account: {account}  region: {region}")

    if args.delete:
        delete_all(session)
        return
    if not args.test_only:
        iam = session.client("iam")
        lambda_role, new1 = ensure_role(iam, LAMBDA_ROLE, LAMBDA_TRUST, "AlphaForge quant tools Lambda (box 6)")
        iam.attach_role_policy(RoleName=LAMBDA_ROLE,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        if curated_s3():  # read-only access to the curated Parquet prefix (L14)
            from app.tools.curated_store import parse_s3_uri

            bucket, prefix = parse_s3_uri(curated_s3())
            iam.put_role_policy(RoleName=LAMBDA_ROLE, PolicyName="read-curated-parquet", PolicyDocument=json.dumps({
                "Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/{prefix}*"},
                    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
                     "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}}}]}))
            print(f"• Lambda reads curated Parquet from {curated_s3()}")
        lambda_arn = deploy_lambda(session.client("lambda"), build_zip(), lambda_role)

        gw_role, new2 = ensure_role(iam, GATEWAY_ROLE, gateway_trust(account),
                                    "AlphaForge AgentCore Gateway: may invoke the quant tools Lambda only")
        iam.put_role_policy(RoleName=GATEWAY_ROLE, PolicyName="invoke-quant-lambda", PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}]}))
        if new2:
            time.sleep(10)  # IAM propagation before the Gateway can assume the role
        ac = session.client("bedrock-agentcore-control")
        gw = ensure_gateway(ac, gw_role)
        ensure_targets(ac, gw["gatewayId"], lambda_arn)
        save_url(gw["gatewayUrl"], gw["gatewayId"], region)
    if args.test or args.test_only:
        url = os.environ.get("AF_GATEWAY_URL") or json.loads(STATE.read_text())["gateway_url"]
        sys.exit(0 if test(session, url) else 1)


if __name__ == "__main__":
    main()
