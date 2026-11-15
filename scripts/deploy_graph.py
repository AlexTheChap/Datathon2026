"""Deploy the market graph: private VPC + Amazon Neptune Serverless + twice-daily Yahoo refresh, boto3 only.

    python scripts/deploy_graph.py                # create or update everything
    python scripts/deploy_graph.py --run-now      # + run a close refresh now (Yahoo -> S3 -> Neptune)
    python scripts/deploy_graph.py --test         # + read the graph back through the loader (selftest)
    python scripts/deploy_graph.py --test-only
    python scripts/deploy_graph.py --delete       # tear everything down (the S3 market/ data is kept)

Prerequisite: scripts/deploy_data_layer.py (S3 bucket, build/data_layer.json). Afterwards run
scripts/deploy_quant_gateway.py: it reads build/graph.json, moves the quant tools Lambda into this VPC and
publishes the `market` Gateway target (market___screen, market___related) on Neptune.

What it creates (names prefixed "alphaforge-graph" / "alphaforge-market"):
1. VPC 10.42.0.0/16, two PRIVATE subnets (two AZs), no Internet gateway, no NAT; an S3 gateway endpoint
   (free) so the Lambdas inside read/write S3; security groups: Lambda -> Neptune on 8182 only.
2. Neptune Serverless cluster "alphaforge-graph" (1-8 NCU, encrypted, IAM database auth, 7-day backups)
   and one db.serverless instance.
3. Lambda "alphaforge-market-refresh" (OUTSIDE the VPC: it needs Yahoo on the Internet): handler
   app.market.refresh.handler; writes s3://<bucket>/market/ then invokes the loader asynchronously.
4. Lambda "alphaforge-graph-loader" (INSIDE the VPC): handler app.market.neptune.handler; S3 -> Neptune.
5. EventBridge Scheduler (America/Toronto, Monday-Friday): 11:00 {"mode": "midday"}, 17:00 {"mode": "close"}.
6. IAM roles, least privilege: refresh = read curated / write market / invoke loader; loader = read S3 +
   neptune-db read/write/delete on this cluster; scheduler = invoke refresh only.
State: build/graph.json (no secret; nothing written to .env).

Cost: Neptune Serverless bills its minimum capacity (1 NCU) around the clock while the cluster exists;
the Lambdas, schedules and the S3 endpoint cost next to nothing. --delete removes the cluster.
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

from deploy_context_lambda import load_dotenv  # noqa: E402
from deploy_quant_gateway import FUNCTION as QUANT_FUNCTION  # noqa: E402
from deploy_quant_gateway import LAMBDA_TRUST, ensure_role  # noqa: E402

NAME = "alphaforge-graph"
CLUSTER = NAME
INSTANCE = f"{NAME}-1"
SUBNET_GROUP = f"{NAME}-subnets"
REFRESH = "alphaforge-market-refresh"
LOADER = "alphaforge-graph-loader"
REFRESH_ROLE, LOADER_ROLE, SCHEDULER_ROLE = REFRESH, LOADER, "alphaforge-market-scheduler"
SCHEDULES = {  # name -> (cron in America/Toronto, payload)
    "alphaforge-market-midday": ("cron(0 11 ? * MON-FRI *)", {"mode": "midday"}),
    "alphaforge-market-close": ("cron(0 17 ? * MON-FRI *)", {"mode": "close"}),
}
TIMEZONE = "America/Toronto"
VPC_CIDR, SUBNET_CIDRS = "10.42.0.0/16", ["10.42.1.0/24", "10.42.2.0/24"]
NCU_MIN, NCU_MAX = 1.0, 8.0
RUNTIME = "python3.12"
DEPS = ["numpy", "pandas", "python-dateutil<3", "fastparquet", "yfinance"]  # dateutil<3: runtime botocore
MARKET_PREFIX = "market/"
TAGS = {"project": "alphaforge", "component": "market-graph"}
STATE = BUILD / "graph.json"
DATA_LAYER = BUILD / "data_layer.json"


def tag_spec(resource: str, name: str) -> list[dict]:
    return [{"ResourceType": resource, "Tags": [{"Key": "Name", "Value": name},
                                                *[{"Key": k, "Value": v} for k, v in TAGS.items()]]}]


def client_error_code(e: Exception) -> str:
    return getattr(e, "response", {}).get("Error", {}).get("Code", "")


# ---------- 1. network ----------

def _by_name(items: list[dict], name: str) -> dict | None:
    for it in items:
        if any(t["Key"] == "Name" and t["Value"] == name for t in it.get("Tags", [])):
            return it
    return None


def ensure_vpc(ec2, region: str) -> dict:
    vpc = _by_name(ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [f"{NAME}-vpc"]}])["Vpcs"],
                   f"{NAME}-vpc")
    if vpc is None:
        vpc = ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=tag_spec("vpc", f"{NAME}-vpc"))["Vpc"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc["VpcId"]])
        print(f"• VPC created: {vpc['VpcId']}")
    vpc_id = vpc["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    azs = sorted(z["ZoneName"] for z in ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}, {"Name": "zone-type", "Values": ["availability-zone"]}]
    )["AvailabilityZones"])[:2]
    existing = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnets = []
    for i, (cidr, az) in enumerate(zip(SUBNET_CIDRS, azs), 1):
        name = f"{NAME}-private-{i}"
        sn = _by_name(existing, name)
        if sn is None:
            sn = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az,
                                   TagSpecifications=tag_spec("subnet", name))["Subnet"]
            print(f"• Private subnet created: {sn['SubnetId']} ({az})")
        subnets.append(sn["SubnetId"])

    rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                            {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    s3_service = f"com.amazonaws.{region}.s3"
    eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                              {"Name": "service-name", "Values": [s3_service]},
                                              {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]}])
    if eps["VpcEndpoints"]:
        endpoint_id = eps["VpcEndpoints"][0]["VpcEndpointId"]
    else:
        endpoint_id = ec2.create_vpc_endpoint(VpcEndpointType="Gateway", VpcId=vpc_id, ServiceName=s3_service,
                                              RouteTableIds=[rt["RouteTableId"]],
                                              TagSpecifications=tag_spec("vpc-endpoint", f"{NAME}-s3"))[
            "VpcEndpoint"]["VpcEndpointId"]
        print(f"• S3 gateway endpoint created: {endpoint_id}")

    def sg(name: str, desc: str) -> str:
        found = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                                                      {"Name": "group-name", "Values": [name]}])["SecurityGroups"]
        if found:
            return found[0]["GroupId"]
        gid = ec2.create_security_group(GroupName=name, Description=desc, VpcId=vpc_id,
                                        TagSpecifications=tag_spec("security-group", name))["GroupId"]
        print(f"• Security group created: {name} {gid}")
        return gid

    sg_lambda = sg(f"{NAME}-lambda", "AlphaForge Lambdas that query Neptune")
    sg_neptune = sg(f"{NAME}-neptune", "AlphaForge Neptune: openCypher 8182 from the Lambdas only")
    try:
        ec2.authorize_security_group_ingress(GroupId=sg_neptune, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 8182, "ToPort": 8182,
            "UserIdGroupPairs": [{"GroupId": sg_lambda, "Description": "Lambdas (loader, quant tools)"}]}])
    except Exception as e:  # rule already there
        if client_error_code(e) != "InvalidPermission.Duplicate":
            raise
    return {"vpc_id": vpc_id, "subnet_ids": subnets, "route_table_id": rt["RouteTableId"],
            "s3_endpoint_id": endpoint_id, "sg_lambda": sg_lambda, "sg_neptune": sg_neptune}


# ---------- 2. Neptune ----------

def ensure_neptune(nep, net: dict) -> dict:
    tags = [{"Key": k, "Value": v} for k, v in TAGS.items()]
    try:
        nep.describe_db_subnet_groups(DBSubnetGroupName=SUBNET_GROUP)
    except nep.exceptions.DBSubnetGroupNotFoundFault:
        nep.create_db_subnet_group(DBSubnetGroupName=SUBNET_GROUP, SubnetIds=net["subnet_ids"], Tags=tags,
                                   DBSubnetGroupDescription="AlphaForge market graph (private subnets)")
        print(f"• Neptune subnet group created: {SUBNET_GROUP}")
    try:
        nep.describe_db_clusters(DBClusterIdentifier=CLUSTER)
        print(f"• Existing Neptune cluster: {CLUSTER}")
    except nep.exceptions.DBClusterNotFoundFault:
        nep.create_db_cluster(DBClusterIdentifier=CLUSTER, Engine="neptune", DBSubnetGroupName=SUBNET_GROUP,
                              VpcSecurityGroupIds=[net["sg_neptune"]], StorageEncrypted=True,
                              IAMDatabaseAuthenticationEnabled=True, DeletionProtection=False,
                              BackupRetentionPeriod=7, Tags=tags,
                              ServerlessV2ScalingConfiguration={"MinCapacity": NCU_MIN, "MaxCapacity": NCU_MAX})
        print(f"• Neptune Serverless cluster created: {CLUSTER} ({NCU_MIN:g}-{NCU_MAX:g} NCU)")
    try:
        nep.describe_db_instances(DBInstanceIdentifier=INSTANCE)
    except nep.exceptions.DBInstanceNotFoundFault:
        nep.create_db_instance(DBInstanceIdentifier=INSTANCE, DBInstanceClass="db.serverless", Engine="neptune",
                               DBClusterIdentifier=CLUSTER, Tags=tags)
        print(f"• Neptune instance created: {INSTANCE} (db.serverless)")
    print("• Waiting for Neptune to be available (10-15 min on creation)...")
    nep.get_waiter("db_instance_available").wait(DBInstanceIdentifier=INSTANCE,
                                                 WaiterConfig={"Delay": 30, "MaxAttempts": 80})
    c = nep.describe_db_clusters(DBClusterIdentifier=CLUSTER)["DBClusters"][0]
    print(f"• Neptune ready: {c['Endpoint']}:{c['Port']} (engine {c.get('EngineVersion')})")
    return {"endpoint": c["Endpoint"], "port": c["Port"], "resource_id": c["DbClusterResourceId"],
            "cluster_arn": c["DBClusterArn"]}


# ---------- 3. package ----------

def build_zip() -> Path:
    pkg = BUILD / "market_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    (pkg / "app" / "tools").mkdir(parents=True)
    (pkg / "app" / "market").mkdir(parents=True)
    print("• Installing numpy/pandas/fastparquet/yfinance (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    for f in ("curated_store.py", "risk_tools.py"):
        shutil.copy(ROOT / "backend" / "app" / "tools" / f, pkg / "app" / "tools" / f)
    for f in (ROOT / "backend" / "app" / "market").glob("*.py"):
        shutil.copy(f, pkg / "app" / "market" / f.name)
    zip_path = BUILD / "market_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc"):
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


# ---------- 4. IAM ----------

def s3_statements(bucket: str, read: list[str], write: list[str]) -> list[dict]:
    st = [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": [f"arn:aws:s3:::{bucket}/{p}*" for p in read]},
          {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
           "Condition": {"StringLike": {"s3:prefix": [f"{p}*" for p in read]}}}]
    if write:
        st.append({"Effect": "Allow", "Action": "s3:PutObject",
                   "Resource": [f"arn:aws:s3:::{bucket}/{p}*" for p in write]})
    return st


def ensure_roles(iam, account: str, region: str, bucket: str, curated_prefix: str, neptune: dict) -> dict:
    basic = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    vpc_access = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    refresh_arn, n1 = ensure_role(iam, REFRESH_ROLE, LAMBDA_TRUST, "AlphaForge market refresh (Yahoo -> S3)")
    iam.attach_role_policy(RoleName=REFRESH_ROLE, PolicyArn=basic)
    iam.put_role_policy(RoleName=REFRESH_ROLE, PolicyName="market-refresh", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            *s3_statements(bucket, [curated_prefix, MARKET_PREFIX], [MARKET_PREFIX]),
            {"Effect": "Allow", "Action": "lambda:InvokeFunction",
             "Resource": f"arn:aws:lambda:{region}:{account}:function:{LOADER}"}]}))

    loader_arn, n2 = ensure_role(iam, LOADER_ROLE, LAMBDA_TRUST, "AlphaForge graph loader (S3 -> Neptune)")
    for p in (basic, vpc_access):
        iam.attach_role_policy(RoleName=LOADER_ROLE, PolicyArn=p)
    iam.put_role_policy(RoleName=LOADER_ROLE, PolicyName="graph-loader", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            *s3_statements(bucket, [curated_prefix, MARKET_PREFIX], []),
            {"Effect": "Allow", "Action": ["neptune-db:connect", "neptune-db:ReadDataViaQuery",
                                           "neptune-db:WriteDataViaQuery", "neptune-db:DeleteDataViaQuery",
                                           "neptune-db:GetQueryStatus", "neptune-db:CancelQuery"],
             "Resource": f"arn:aws:neptune-db:{region}:{account}:{neptune['resource_id']}/*"}]}))

    sched_trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    sched_arn, n3 = ensure_role(iam, SCHEDULER_ROLE, sched_trust, "EventBridge Scheduler: invoke the market refresh")
    iam.put_role_policy(RoleName=SCHEDULER_ROLE, PolicyName="invoke-refresh", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
                                                "Resource": f"arn:aws:lambda:{region}:{account}:function:{REFRESH}"}]}))
    if n1 or n2 or n3:
        time.sleep(10)  # IAM propagation
    return {"refresh": refresh_arn, "loader": loader_arn, "scheduler": sched_arn}


# ---------- 5. Lambdas ----------

def deploy_function(lam, name: str, role_arn: str, handler: str, code: dict, cfg: dict, description: str) -> str:
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, **code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, Role=role_arn, Handler=handler, **cfg)
        print(f"• Lambda updated: {name}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a new role takes a few seconds before Lambda can assume it
            try:
                lam.create_function(FunctionName=name, Runtime=RUNTIME, Role=role_arn, Handler=handler,
                                    Code=code, Description=description, Tags=TAGS, **cfg)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 9:
                    raise
                time.sleep(6)
        print(f"• Lambda created: {name}")
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def deploy_lambdas(session, zip_path: Path, roles: dict, net: dict, neptune: dict, layer: dict) -> dict:
    s3, lam = session.client("s3"), session.client("lambda")
    key = "lambda/market_lambda.zip"  # > 50 MB with yfinance: upload through S3
    s3.upload_file(str(zip_path), layer["bucket"], key)
    code = {"S3Bucket": layer["bucket"], "S3Key": key}
    market_s3 = f"s3://{layer['bucket']}/{MARKET_PREFIX}"
    loader_arn = deploy_function(
        lam, LOADER, roles["loader"], "app.market.neptune.handler", code,
        dict(Timeout=900, MemorySize=1536,
             VpcConfig={"SubnetIds": net["subnet_ids"], "SecurityGroupIds": [net["sg_lambda"]]},
             Environment={"Variables": {"AF_CURATED_S3": layer["curated_s3"], "AF_MARKET_S3": market_s3,
                                        "AF_NEPTUNE_ENDPOINT": neptune["endpoint"],
                                        "AF_NEPTUNE_PORT": str(neptune["port"])}}),
        "AlphaForge market graph loader: S3 market tables -> Neptune (inside the graph VPC)")
    refresh_arn = deploy_function(
        lam, REFRESH, roles["refresh"], "app.market.refresh.handler", code,
        dict(Timeout=900, MemorySize=3008, EphemeralStorage={"Size": 1024},
             Environment={"Variables": {"AF_CURATED_S3": layer["curated_s3"], "AF_MARKET_S3": market_s3,
                                        "AF_GRAPH_LOADER": LOADER, "HOME": "/tmp", "XDG_CACHE_HOME": "/tmp"}}),
        "AlphaForge market refresh: Yahoo Finance (S&P 500 + universe) -> S3, twice a day")
    return {"refresh_arn": refresh_arn, "loader_arn": loader_arn, "market_s3": market_s3}


# ---------- 6. schedules ----------

def ensure_schedules(sch, refresh_arn: str, role_arn: str) -> None:
    for name, (cron, payload) in SCHEDULES.items():
        kwargs = dict(Name=name, ScheduleExpression=cron, ScheduleExpressionTimezone=TIMEZONE,
                      FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED",
                      Description=f"AlphaForge market refresh ({payload['mode']})",
                      Target={"Arn": refresh_arn, "RoleArn": role_arn, "Input": json.dumps(payload),
                              "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}})
        try:
            sch.get_schedule(Name=name)
            sch.update_schedule(**kwargs)
        except sch.exceptions.ResourceNotFoundException:
            sch.create_schedule(**kwargs)
        print(f"• Schedule: {name} {cron} {TIMEZONE} -> {payload}")


# ---------- run / test ----------

def invoke(lam, name: str, payload: dict) -> dict:
    r = lam.invoke(FunctionName=name, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    body = json.loads(r["Payload"].read() or b"null")
    if r.get("FunctionError"):
        raise RuntimeError(f"{name} failed: {body}")
    return body


def run_now(session) -> None:
    lam = session.client("lambda")
    print("• Close refresh now (Yahoo -> S3)...")
    m = invoke(lam, REFRESH, {"mode": "close"})
    print(f"  as_of {m.get('as_of')}  prices {m.get('tickers_with_prices')}/{m.get('tickers_requested')}  "
          f"metrics {m.get('tickers_with_metrics')}  correlation edges {m.get('correlation_edges')}  "
          f"missing {m.get('missing')[:10]}")
    print("• Loading Neptune (synchronous, to report the result)...")
    out = invoke(lam, LOADER, {"mode": "close"})
    print(f"  {out}")


def test(session) -> bool:
    from app.market.graph import LocalGraph, normalize_filters
    from app.tools.curated_store import sync_from_s3

    st = json.loads(STATE.read_text())
    out = invoke(session.client("lambda"), LOADER, {"mode": "selftest", "ticker": "NVDA"})
    print(f"• Neptune snapshot {out['snapshot']}: {out['count']['assets']} assets, edges {out['count']['edges']}")
    cache = BUILD / "market_cache"
    shutil.rmtree(cache, ignore_errors=True)
    sync_from_s3(st["market_s3"], cache, client=session.client("s3"))
    local = LocalGraph.from_dirs(ROOT / "data" / "curated", cache)
    f = normalize_filters({"sector": "Utilities", "sort_by": "ann_vol", "limit": 5})
    same_screen = [r["ticker"] for r in out["screen"]] == [r["ticker"] for r in local.screen(f)]
    rel_l = local.related("NVDA")
    same_rel = (sorted(h["fund"] for h in out["related"]["held_by"]) == sorted(h["fund"] for h in rel_l["held_by"])
                and [c["ticker"] for c in out["related"]["correlated"]] == [c["ticker"] for c in rel_l["correlated"]])
    ok = out["count"]["assets"] >= 500 and same_screen and same_rel
    print(f"  {'✓' if same_screen else '✗'} screen Utilities (lowest vol): {[r['ticker'] for r in out['screen']]}")
    print(f"  {'✓' if same_rel else '✗'} related NVDA: held by {[h['fund'] for h in out['related']['held_by']]}")
    print("• Neptune == in-memory graph" if ok else "✗ Neptune and the in-memory graph differ (or < 500 assets)")
    return ok


# ---------- delete ----------

def delete_all(session) -> None:
    lam, sch, nep, ec2, iam = (session.client(s) for s in ("lambda", "scheduler", "neptune", "ec2", "iam"))
    for name in SCHEDULES:
        try:
            sch.delete_schedule(Name=name)
            print(f"• Schedule deleted: {name}")
        except sch.exceptions.ResourceNotFoundException:
            pass
    for name in (REFRESH, LOADER):
        try:
            lam.delete_function(FunctionName=name)
            print(f"• Lambda deleted: {name}")
        except lam.exceptions.ResourceNotFoundException:
            pass
    try:  # the quant tools Lambda leaves the VPC (redeploy it to drop AF_NEPTUNE_ENDPOINT)
        cfg = lam.get_function_configuration(FunctionName=QUANT_FUNCTION)
        if cfg.get("VpcConfig", {}).get("SubnetIds"):
            env = cfg.get("Environment", {}).get("Variables", {})
            env = {k: v for k, v in env.items() if not k.startswith("AF_NEPTUNE")}
            lam.update_function_configuration(FunctionName=QUANT_FUNCTION, Environment={"Variables": env},
                                              VpcConfig={"SubnetIds": [], "SecurityGroupIds": []})
            print(f"• {QUANT_FUNCTION} detached from the VPC (in-memory graph from now on)")
    except lam.exceptions.ResourceNotFoundException:
        pass
    try:
        nep.delete_db_instance(DBInstanceIdentifier=INSTANCE)
        print(f"• Deleting Neptune instance {INSTANCE}...")
        nep.get_waiter("db_instance_deleted").wait(DBInstanceIdentifier=INSTANCE,
                                                   WaiterConfig={"Delay": 30, "MaxAttempts": 60})
    except nep.exceptions.DBInstanceNotFoundFault:
        pass
    try:
        nep.delete_db_cluster(DBClusterIdentifier=CLUSTER, SkipFinalSnapshot=True)
        print(f"• Deleting Neptune cluster {CLUSTER}...")
        for _ in range(80):
            try:
                nep.describe_db_clusters(DBClusterIdentifier=CLUSTER)
                time.sleep(15)
            except nep.exceptions.DBClusterNotFoundFault:
                break
    except nep.exceptions.DBClusterNotFoundFault:
        pass
    try:
        nep.delete_db_subnet_group(DBSubnetGroupName=SUBNET_GROUP)
    except nep.exceptions.DBSubnetGroupNotFoundFault:
        pass

    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [f"{NAME}-vpc"]}])["Vpcs"]
    for vpc in vpcs:
        vid = vpc["VpcId"]
        eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vid]}])["VpcEndpoints"]
        if eps:
            ec2.delete_vpc_endpoints(VpcEndpointIds=[e["VpcEndpointId"] for e in eps])
        sgs = [g for g in ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vid]}])["SecurityGroups"]
               if g["GroupName"] != "default"]
        for g in sgs:  # drop cross references first
            if g["IpPermissions"]:
                ec2.revoke_security_group_ingress(GroupId=g["GroupId"], IpPermissions=g["IpPermissions"])
        print("• Deleting the VPC (Lambda network interfaces can take up to ~20 min to be released)...")
        deadline = time.time() + 1800
        for g in sgs:
            while True:
                try:
                    ec2.delete_security_group(GroupId=g["GroupId"])
                    break
                except Exception as e:
                    if client_error_code(e) != "DependencyViolation" or time.time() > deadline:
                        raise
                    time.sleep(30)
        for sn in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vid]}])["Subnets"]:
            ec2.delete_subnet(SubnetId=sn["SubnetId"])
        ec2.delete_vpc(VpcId=vid)
        print(f"• VPC deleted: {vid}")
    for role in (REFRESH_ROLE, LOADER_ROLE, SCHEDULER_ROLE):
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
    print("• Kept: s3://<bucket>/market/ (versioned history of the refreshes). Redeploy the quant Gateway.")


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="AWS profile; otherwise AWS_* variables, otherwise .env")
    ap.add_argument("--region", default=None, help="default: AWS_REGION, otherwise us-east-1")
    ap.add_argument("--run-now", action="store_true", help="run a close refresh and load Neptune now")
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
        if not DATA_LAYER.exists():
            sys.exit("build/data_layer.json missing: run scripts/deploy_data_layer.py first (S3 bucket).")
        layer = json.loads(DATA_LAYER.read_text())
        from app.tools.curated_store import parse_s3_uri
        from deploy_data_layer import publish_parquet

        bucket, curated_prefix = parse_s3_uri(layer["curated_s3"])
        rows = publish_parquet(session.client("s3"), bucket)  # ESG screen, controversies, metrics included
        print(f"• Curated Parquet republished ({len(rows)} tables) for the loader")
        net = ensure_vpc(session.client("ec2"), region)
        neptune = ensure_neptune(session.client("neptune"), net)
        roles = ensure_roles(session.client("iam"), account, region, bucket, curated_prefix, neptune)
        fns = deploy_lambdas(session, build_zip(), roles, net, neptune, layer)
        ensure_schedules(session.client("scheduler"), fns["refresh_arn"], roles["scheduler"])
        BUILD.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({**net, "neptune_endpoint": neptune["endpoint"], "neptune_port": neptune["port"],
                                     "neptune_resource_id": neptune["resource_id"], "region": region,
                                     "market_s3": fns["market_s3"], "refresh_function": REFRESH,
                                     "loader_function": LOADER, "schedules": {k: v[0] for k, v in SCHEDULES.items()},
                                     "timezone": TIMEZONE}, indent=2))
        print(f"• State: {STATE}. Next: python scripts/deploy_quant_gateway.py (tools Lambda -> VPC + market target)")
    if args.run_now:
        run_now(session)
    if args.test or args.test_only:
        sys.exit(0 if test(session) else 1)


if __name__ == "__main__":
    main()
