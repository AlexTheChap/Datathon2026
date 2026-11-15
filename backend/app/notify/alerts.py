"""News-alert agent: fetches the news on the stocks each person holds and texts it to them (Amazon SNS SMS).

Deterministic, no language model (the wording is fixed in app.notify.messages). One run (EventBridge Scheduler,
weekdays 18:00 America/Toronto, scripts/deploy_sms_alerts.py):

  1. people who opted in to texts (ContactStore: opt_in + phone, set at sign-up or on the profile page);
  2. their HELD stocks = the tickers of their saved portfolio (Profile.portfolio), nothing else: no "similar"
     stock, no suggestion;
  3. the news on those tickers in (since, until] (SEC 8-K filings), fetched once for everybody;
  4. per person, the news not texted yet (SMSSENT log) -> ONE text (at most 3 items) -> marked as sent.

Lambda handler: {"since"?, "until"?, "dry_run"?}. Default window: the last AF_ALERT_LOOKBACK_DAYS days (3: a
Friday filing is still caught on Monday; the SMSSENT log prevents a second text).

Local run (prints the texts, sends nothing):
    python -m app.notify.alerts --source corpus --since 2026-11-01 --until 2026-11-12 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.notify.contacts import ContactStore, mask, now_iso
from app.notify.messages import AdviceWordingError, compose
from app.notify.news import CorpusNewsSource, NewsSource, SecFilingsSource

TZ = ZoneInfo("America/Toronto")


class Sender(Protocol):
    def send(self, phone: str, text: str) -> str: ...


class SnsSmsSender:
    """Amazon SNS direct SMS publish. In the SMS sandbox only verified numbers receive it."""

    def __init__(self, client=None):
        if client is None:
            import boto3

            client = boto3.client("sns")
        self.client = client

    def send(self, phone: str, text: str) -> str:
        r = self.client.publish(PhoneNumber=phone, Message=text, MessageAttributes={
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}})
        return r["MessageId"]


class DryRunSender:
    """Keeps the texts instead of sending them (local runs, tests, --dry-run)."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, phone: str, text: str) -> str:
        self.sent.append((phone, text))
        return f"dry-run-{len(self.sent)}"


def held_tickers(profile) -> list[str]:
    return sorted(t for t, w in ((profile.portfolio or {}) if profile else {}).items() if w and w > 0)


def default_window(today: date | None = None, lookback_days: int | None = None) -> tuple[str, str]:
    today = today or datetime.now(TZ).date()
    days = lookback_days or int(os.environ.get("AF_ALERT_LOOKBACK_DAYS", "3"))
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def run_alerts(contacts: ContactStore, profiles, source: NewsSource, sender: Sender, since: str, until: str,
               app_url: str | None = None, record: bool = True) -> dict[str, Any]:
    """record=False (dry runs): nothing is marked as sent, so the real run still texts the same news."""
    people = []
    for c in contacts.opted_in():
        held = held_tickers(profiles.get(c.actor_id))
        people.append((c, held))
    universe = sorted({t for _, held in people for t in held})
    events = source.events(universe, since, until) if universe else []

    report: dict[str, Any] = {"window": [since, until], "opted_in": len(people), "tickers_checked": len(universe),
                              "news_found": len(events), "texts_sent": 0, "people": []}
    for c, held in people:
        mine = [e for e in events if e.ticker in held and not contacts.was_sent(c.actor_id, e.event_id)]
        line: dict[str, Any] = {"actor_id": c.actor_id, "phone": mask(c.phone), "held": len(held)}
        if not held:
            line["skipped"] = "no saved portfolio"
        elif not mine:
            line["skipped"] = "no new news on held stocks"
        else:
            try:
                text = compose(mine, app_url)
            except AdviceWordingError as e:   # never send; surfaced in the report and the logs
                line["skipped"] = str(e)
                report["people"].append(line)
                continue
            line["message_id"] = sender.send(c.phone, text)
            line["news"] = [f"{e.ticker} {e.published_at}" for e in mine]
            line["text"] = text
            sent_at = now_iso()
            for e in mine if record else []:
                contacts.mark_sent(c.actor_id, e.event_id, sent_at)
            report["texts_sent"] += 1
        report["people"].append(line)
    return report


# ---------- wiring (Lambda + CLI) ----------

def stores_from_env(env: dict[str, str] | None = None):
    """Same choice as the API (AF_STORE): dynamodb (Lambda), file (local, .af_store/), memory."""
    env = os.environ if env is None else env
    kind = env.get("AF_STORE", "dynamodb" if env.get("AWS_LAMBDA_FUNCTION_NAME") else "memory").lower()
    if kind == "dynamodb":
        import boto3

        from app.notify.contacts import DynamoDBContactStore
        from app.store.profiles import DynamoDBProfileStore

        table = boto3.resource("dynamodb").Table(env.get("AF_TABLE", "alphaforge"))
        return DynamoDBContactStore(table), DynamoDBProfileStore(table)
    if kind == "file":
        from app.core.config import REPO_ROOT
        from app.notify.contacts import JsonFileContactStore
        from app.store.profiles import JsonFileProfileStore

        root = Path(env.get("AF_DATA_DIR") or ".af_store")
        root = root if root.is_absolute() else REPO_ROOT / root
        return JsonFileContactStore(root / "contacts"), JsonFileProfileStore(root / "profiles")
    from app.notify.contacts import InMemoryContactStore
    from app.store.profiles import InMemoryProfileStore

    return InMemoryContactStore(), InMemoryProfileStore()


def handler(event: dict | None, context: Any = None) -> dict:
    event = event or {}
    since, until = default_window()
    since, until = event.get("since") or since, event.get("until") or until
    contacts, profiles = stores_from_env()
    dry = bool(event.get("dry_run"))
    sender = DryRunSender() if dry else SnsSmsSender()
    report = run_alerts(contacts, profiles, SecFilingsSource(), sender, since, until, os.environ.get("AF_APP_URL"),
                        record=not dry)
    log = {**report, "people": [{k: v for k, v in p.items() if k != "text"} for p in report["people"]]}
    print(json.dumps(log))   # CloudWatch: masked phones, no message text
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="News-alert agent (local run).")
    ap.add_argument("--source", choices=["corpus", "sec"], default="corpus")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--dry-run", action="store_true", help="print the texts instead of sending them")
    a = ap.parse_args()
    since, until = default_window()
    contacts, profiles = stores_from_env()
    source = CorpusNewsSource() if a.source == "corpus" else SecFilingsSource()
    sender = DryRunSender() if a.dry_run else SnsSmsSender()
    report = run_alerts(contacts, profiles, source, sender, a.since or since, a.until or until,
                        os.environ.get("AF_APP_URL"), record=not a.dry_run)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
