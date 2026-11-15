"""Deploy the news-alert agent: SEC news on held stocks -> SMS (Amazon SNS), weekdays 18:00 Toronto. boto3 only.

    python scripts/deploy_sms_alerts.py --sec-contact "AlphaForge Datathon you@example.com"   # create / update
    python scripts/deploy_sms_alerts.py --test                    # dry run in AWS: who would get which text
    python scripts/deploy_sms_alerts.py --sandbox-status          # SMS sandbox? verified numbers?
    python scripts/deploy_sms_alerts.py --verify-phone +15145550123              # sends a code by SMS
    python scripts/deploy_sms_alerts.py --verify-phone +15145550123 --code 123456
    python scripts/deploy_sms_alerts.py --send-test +15145550123  # one fixed test text, to check delivery
    python scripts/deploy_sms_alerts.py --delete

Prerequisite: scripts/deploy_data_layer.py (DynamoDB table "alphaforge", build/data_layer.json). The web API
must run the same code (GET/PUT /v1/notifications/sms) with AF_STORE=dynamodb so the choices land in that table.

What it creates:
1. Lambda "alphaforge-news-alerts" (handler app.notify.alerts.handler): pydantic + app/notify, app/schemas,
   app/store/profiles. Env: AF_STORE=dynamodb, AF_TABLE, AF_SEC_CONTACT (the SEC requires a contact in the
   User-Agent), AF_APP_URL (optional link in the text).
2. IAM role: CloudWatch logs; DynamoDB GetItem/PutItem/Scan on the table only; sns:Publish (direct SMS
   publishing has no topic ARN to scope it to).
3. EventBridge Scheduler "alphaforge-news-alerts": cron(0 18 ? * MON-FRI *) America/Toronto.
4. SNS SMS default type "Transactional".

SMS sandbox: a new AWS account can only text VERIFIED numbers (--verify-phone, then --code). That is enough for
a demo. Texting real customers needs leaving the sandbox (AWS support request) and an origination number.
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
from deploy_quant_gateway import LAMBDA_TRUST, ensure_role  # noqa: E402

FUNCTION = ROLE = SCHEDULE = "alphaforge-news-alerts"
SCHEDULER_ROLE = "alphaforge-news-alerts-scheduler"
CRON, TIMEZONE = "cron(0 18 ? * MON-FRI *)", "America/Toronto"
RUNTIME = "python3.12"
DEPS = ["pydantic>=2.7,<3", "tzdata"]
TAGS = {"project": "alphaforge", "component": "news-alerts"}
DATA_LAYER = BUILD / "data_layer.json"
STATE = BUILD / "sms_alerts.json"
TEST_TEXT = "AlphaForge: test text. News alerts on the stocks you hold are set up. Info only. Reply STOP to opt out."


def build_zip() -> Path:
    pkg = BUILD / "alerts_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    for d in ("notify", "schemas", "store"):
        (pkg / "app" / d).mkdir(parents=True)
    print("• Installing pydantic (Linux wheels, Python 3.12)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    src = ROOT / "backend" / "app"
    for f in ("contacts.py", "news.py", "messages.py", "alerts.py"):
        shutil.copy(src / "notify" / f, pkg / "app" / "notify" / f)
    for f in (src / "schemas").glob("*.py"):
        shutil.copy(f, pkg / "app" / "schemas" / f.name)
    shutil.copy(src / "store" / "profiles.py", pkg / "app" / "store" / "profiles.py")
    zip_path = BUILD / "alerts_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc"):
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Package: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")
    return zip_path


def ensure_roles(iam, account: str, region: str, table_arn: str) -> tuple[str, str]:
    arn, new1 = ensure_role(iam, ROLE, LAMBDA_TRUST, "AlphaForge news-alert agent (SEC news -> SMS)")
    iam.attach_role_policy(RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE, PolicyName="news-alerts", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"], "Resource": table_arn},
            {"Effect": "Allow", "Action": "sns:Publish", "Resource": "*"}]}))
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    sched_arn, new2 = ensure_role(iam, SCHEDULER_ROLE, trust, "EventBridge Scheduler: invoke the news-alert agent")
    iam.put_role_policy(RoleName=SCHEDULER_ROLE, PolicyName="invoke-alerts", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
                                                "Resource": f"arn:aws:lambda:{region}:{account}:function:{FUNCTION}"}]}))
    if new1 or new2:
        time.sleep(10)  # IAM propagation
    return arn, sched_arn


def deploy_lambda(lam, zip_path: Path, role_arn: str, env: dict) -> str:
    code = zip_path.read_bytes()
    cfg = dict(Timeout=300, MemorySize=512, Environment={"Variables": env})
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, Role=role_arn, **cfg)
        print(f"• Lambda updated: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):
            try:
                lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                                    Handler="app.notify.alerts.handler", Code={"ZipFile": code}, Tags=TAGS,
                                    Description="AlphaForge news-alert agent: SEC news on held stocks -> SMS", **cfg)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 9:
                    raise
                time.sleep(6)
        print(f"• Lambda created: {FUNCTION}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    return lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]


def ensure_schedule(sch, fn_arn: str, role_arn: str) -> None:
    kwargs = dict(Name=SCHEDULE, ScheduleExpression=CRON, ScheduleExpressionTimezone=TIMEZONE,
                  FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED", Description="AlphaForge news alerts by SMS",
                  Target={"Arn": fn_arn, "RoleArn": role_arn, "Input": "{}",
                          "RetryPolicy": {"MaximumRetryAttempts": 1, "MaximumEventAgeInSeconds": 3600}})
    try:
        sch.get_schedule(Name=SCHEDULE)
        sch.update_schedule(**kwargs)
    except sch.exceptions.ResourceNotFoundException:
        sch.create_schedule(**kwargs)
    print(f"• Schedule: {SCHEDULE} {CRON} {TIMEZONE}")


def invoke(lam, payload: dict) -> dict:
    r = lam.invoke(FunctionName=FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    body = json.loads(r["Payload"].read() or b"null")
    if r.get("FunctionError"):
        raise RuntimeError(f"{FUNCTION} failed: {body}")
    return body


def delete_all(session) -> None:
    sch, lam, iam = session.client("scheduler"), session.client("lambda"), session.client("iam")
    try:
        sch.delete_schedule(Name=SCHEDULE)
        print(f"• Schedule deleted: {SCHEDULE}")
    except sch.exceptions.ResourceNotFoundException:
        pass
    try:
        lam.delete_function(FunctionName=FUNCTION)
        print(f"• Lambda deleted: {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        pass
    for role in (ROLE, SCHEDULER_ROLE):
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
    print("• Kept: the CONTACT#sms / SMSSENT# items in DynamoDB (the people's choices).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--region", default=None)
    ap.add_argument("--sec-contact", help='SEC User-Agent contact, e.g. "AlphaForge Datathon you@example.com" '
                                          "(or AF_SEC_CONTACT in .env)")
    ap.add_argument("--app-url", help="link to the web app added to each text (or AF_APP_URL)")
    ap.add_argument("--test", action="store_true", help="dry run in AWS after deploying")
    ap.add_argument("--test-only", action="store_true")
    ap.add_argument("--sandbox-status", action="store_true")
    ap.add_argument("--verify-phone")
    ap.add_argument("--code")
    ap.add_argument("--send-test")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    if not args.profile:
        load_dotenv()
    import boto3

    from app.notify.contacts import normalize_phone

    region = args.region or os.environ.get("AWS_REGION") or "us-east-1"
    session = boto3.Session(profile_name=args.profile, region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    sns = session.client("sns")
    print(f"• Account: {account}  region: {region}")

    if args.delete:
        delete_all(session)
        return
    if args.sandbox_status:
        st = sns.get_sms_sandbox_account_status()
        nums = sns.list_sms_sandbox_phone_numbers().get("PhoneNumbers", [])
        print(f"• SMS sandbox: {st['IsInSandbox']}")
        for n in nums:
            print(f"  {n['PhoneNumber'][:-4]}•••• {n['Status']}")
        return
    if args.verify_phone:
        phone = normalize_phone(args.verify_phone)
        if args.code:
            sns.verify_sms_sandbox_phone_number(PhoneNumber=phone, OneTimePassword=args.code)
            print("• Number verified: it can receive texts from the sandbox.")
        else:
            sns.create_sms_sandbox_phone_number(PhoneNumber=phone, LanguageCode="en-US")
            print("• Code sent by SMS. Run again with --code <6 digits>.")
        return
    if args.send_test:
        mid = sns.publish(PhoneNumber=normalize_phone(args.send_test), Message=TEST_TEXT, MessageAttributes={
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}})["MessageId"]
        print(f"• Test text sent (MessageId {mid}).")
        return

    if not args.test_only:
        if not DATA_LAYER.exists():
            sys.exit("build/data_layer.json missing: run scripts/deploy_data_layer.py first (DynamoDB table).")
        layer = json.loads(DATA_LAYER.read_text())
        contact = args.sec_contact or os.environ.get("AF_SEC_CONTACT")
        if not contact:
            sys.exit('The SEC requires a contact: --sec-contact "AlphaForge Datathon you@example.com" '
                     "(or AF_SEC_CONTACT in .env).")
        env = {"AF_STORE": "dynamodb", "AF_TABLE": layer["table"], "AF_SEC_CONTACT": contact}
        if args.app_url or os.environ.get("AF_APP_URL"):
            env["AF_APP_URL"] = args.app_url or os.environ["AF_APP_URL"]
        role_arn, sched_arn = ensure_roles(session.client("iam"), account, region, layer["table_arn"])
        fn_arn = deploy_lambda(session.client("lambda"), build_zip(), role_arn, env)
        ensure_schedule(session.client("scheduler"), fn_arn, sched_arn)
        sns.set_sms_attributes(attributes={"DefaultSMSType": "Transactional"})
        BUILD.mkdir(exist_ok=True)
        STATE.write_text(json.dumps({"function": FUNCTION, "schedule": CRON, "timezone": TIMEZONE,
                                     "region": region}, indent=2))
        st = sns.get_sms_sandbox_account_status()
        if st["IsInSandbox"]:
            print("• SMS sandbox: only verified numbers receive texts (--verify-phone).")
    if args.test or args.test_only:
        r = invoke(session.client("lambda"), {"dry_run": True})
        print(f"• Dry run {r['window']}: {r['opted_in']} opted in, {r['tickers_checked']} held tickers, "
              f"{r['news_found']} news, {r['texts_sent']} text(s) that would be sent")
        for p in r["people"]:
            print(f"  {p['phone']}: {p.get('skipped') or p.get('text')}")


if __name__ == "__main__":
    main()
