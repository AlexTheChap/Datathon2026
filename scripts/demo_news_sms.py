"""Live demo: text a scripted "news article" at an exact moment, through the deployed news-alert agent.

    # 1. see the text locally (no AWS)
    python scripts/demo_news_sms.py preview --ticker NVDA --source Reuters \
        --headline "Nvidia unveils a new AI chip and raises its data-center outlook"

    # 2. schedule it: EventBridge Scheduler calls the alert Lambda once at 14:30 Toronto time, then deletes itself
    python scripts/demo_news_sms.py schedule --at "2026-11-20 14:30" --ticker NVDA --source Reuters \
        --headline "Nvidia unveils a new AI chip and raises its data-center outlook"
    python scripts/demo_news_sms.py schedule --at 14:30 ...        # today
    python scripts/demo_news_sms.py schedule --in 2m ...           # in 2 minutes (rehearsal)

    # right away (rehearsal) · who would get it, sending nothing · scheduled demos · cancel one
    python scripts/demo_news_sms.py now --ticker NVDA --headline "..."
    python scripts/demo_news_sms.py now --ticker NVDA --headline "..." --dry-run
    python scripts/demo_news_sms.py list
    python scripts/demo_news_sms.py cancel alphaforge-demo-news-20261120-1430

Who gets the text:
  default    every person who chose texts + "news" and HOLDS the ticker (the real pipeline: set the demo
             account's phone in My profile > Notifications and make sure its portfolio holds the ticker);
  --to NUM   only that number (fallback, no profile needed).
SMS sandbox: the number must be verified first (scripts/deploy_sms_alerts.py --verify-phone).
Prerequisite: scripts/deploy_sms_alerts.py deployed from this branch (the Lambda must include app/notify/demo.py).
`schedule` dry-runs the Lambda first, so a missing redeploy or a refused wording is caught before the demo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import invoke, load_dotenv  # noqa: E402

FUNCTION = "alphaforge-news-alerts"
SCHEDULER_ROLE = "alphaforge-news-alerts-scheduler"
PREFIX = "alphaforge-demo-news-"
TZ_NAME = "America/Toronto"
TZ = ZoneInfo(TZ_NAME)


def payload(a) -> dict:
    spec = {"ticker": a.ticker, "headline": a.headline, "source": a.source}
    for k in ("url", "to", "published_at"):
        if getattr(a, k, None):
            spec[k] = getattr(a, k)
    return {"demo_news": spec}


def parse_when(at: str | None, in_: str | None, now: datetime | None = None) -> datetime:
    """'2026-11-20 14:30' | '14:30' (today) | --in '2m' / '90s' / '1h' -> aware Toronto time, in the future."""
    now = now or datetime.now(TZ)
    if in_:
        m = re.fullmatch(r"(\d+)\s*([smh])", in_.strip())
        if not m:
            raise ValueError("--in takes a number and s, m or h, like 2m.")
        when = now + timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours"}[m[2]]: int(m[1])})
    elif at:
        at = at.strip().replace("T", " ")
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", at):
            at = f"{now.date().isoformat()} {at}"
        when = datetime.fromisoformat(at).replace(tzinfo=TZ)
    else:
        raise ValueError("Give --at or --in.")
    when = when.replace(microsecond=0)
    if when <= now:
        raise ValueError(f"{when:%Y-%m-%d %H:%M:%S} Toronto time is already past.")
    return when


def show(report: dict) -> None:
    print(f"• Mode: {report.get('mode')}  news: {report.get('news')}  texts: {report.get('texts_sent')}")
    for p in report.get("people", []):
        print(f"  {p.get('phone')}: {p.get('skipped') or p.get('error') or p.get('text')}")
    if report.get("mode") == "holders" and not report.get("texts_sent"):
        print("  Nobody would get it: check the account's notification settings (texts + news) and that its "
              "portfolio holds the ticker, or use --to.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scripted news SMS for a live demo.")
    ap.add_argument("--profile")
    ap.add_argument("--region")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def news_args(p):
        p.add_argument("--ticker", required=True)
        p.add_argument("--headline", required=True, help="the article's title, factual (no advice wording)")
        p.add_argument("--source", default="Reuters", help="shown as '<ticker>: <source> on <date> (...)'")
        p.add_argument("--url")
        p.add_argument("--to", help="send only to this number instead of the holders of the ticker")

    news_args(sub.add_parser("preview", help="print the text locally, no AWS"))
    p_now = sub.add_parser("now", help="invoke the alert Lambda right away")
    news_args(p_now)
    p_now.add_argument("--dry-run", action="store_true", help="who would get which text; nothing sent")
    p_s = sub.add_parser("schedule", help="one-time EventBridge schedule at an exact moment")
    news_args(p_s)
    p_s.add_argument("--at", help="'YYYY-MM-DD HH:MM' or 'HH:MM' (today), Toronto time")
    p_s.add_argument("--in", dest="in_", help="from now: 90s, 2m, 1h")
    p_s.add_argument("--name", help=f"schedule name (default {PREFIX}<date>-<time>)")
    sub.add_parser("list", help="scheduled demo texts")
    sub.add_parser("cancel", help="delete a scheduled demo text").add_argument("name")
    a = ap.parse_args()

    if a.cmd == "preview":
        from app.notify.demo import demo_event
        from app.notify.messages import compose

        text = compose([demo_event(payload(a)["demo_news"])], os.environ.get("AF_APP_URL"))
        print(f"{len(text)} characters:\n{text}")
        return

    if not a.profile:
        load_dotenv()
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    import boto3

    region = a.region or os.environ.get("AWS_REGION") or "us-east-1"
    session = boto3.Session(profile_name=a.profile, region_name=region)
    lam, sch = session.client("lambda"), session.client("scheduler")

    if a.cmd == "list":
        found = sch.list_schedules(NamePrefix=PREFIX).get("Schedules", [])
        if not found:
            print("• No scheduled demo text.")
        for s in found:
            d = sch.get_schedule(Name=s["Name"])
            news = json.loads(d["Target"]["Input"])["demo_news"]
            print(f"• {s['Name']}  {d['ScheduleExpression']} {d.get('ScheduleExpressionTimezone', '')}  "
                  f"{news['ticker']}: {news['headline']}" + (f"  -> {news['to'][:-4]}••••" if news.get("to") else ""))
        return
    if a.cmd == "cancel":
        sch.delete_schedule(Name=a.name)
        print(f"• Deleted: {a.name}")
        return

    body = payload(a)
    if a.cmd == "now":
        show(invoke(lam, FUNCTION, {**body, "dry_run": a.dry_run}))
        return

    when = parse_when(a.at, a.in_)
    check = invoke(lam, FUNCTION, {**body, "dry_run": True})   # catches an old Lambda or a refused wording now
    if not check.get("demo"):
        sys.exit("The deployed Lambda has no demo mode: redeploy it from this branch "
                 "(python scripts/deploy_sms_alerts.py).")
    print("• Dry run OK:")
    show(check)
    fn_arn = lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]
    role_arn = session.client("iam").get_role(RoleName=SCHEDULER_ROLE)["Role"]["Arn"]
    name = a.name or f"{PREFIX}{when:%Y%m%d-%H%M%S}"
    sch.create_schedule(
        Name=name, ScheduleExpression=f"at({when:%Y-%m-%dT%H:%M:%S})", ScheduleExpressionTimezone=TZ_NAME,
        FlexibleTimeWindow={"Mode": "OFF"}, ActionAfterCompletion="DELETE", State="ENABLED",
        Description=f"AlphaForge demo news SMS: {body['demo_news']['ticker']}",
        Target={"Arn": fn_arn, "RoleArn": role_arn, "Input": json.dumps(body),
                "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 300}})
    print(f"• Scheduled: {name} at {when:%Y-%m-%d %H:%M:%S} {TZ_NAME} (deletes itself after it runs; "
          f"usually lands within a minute).")


if __name__ == "__main__":
    main()
