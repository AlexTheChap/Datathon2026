"""Demo mode of the news-alert agent: one scripted news item, texted at a chosen moment.

For a live demo we cannot wait for a real SEC filing. A "demo news" item stands in for a news article and goes
through the SAME path as a real one: the same wording (app.notify.messages.compose, advice check included) and,
by default, the same targeting (people who chose texts + "news" and HOLD that ticker).

The alert Lambda receives it in its event (scripts/demo_news_sms.py puts it there, from a one-time EventBridge
schedule at the chosen time, or right away):

    {"demo_news": {"ticker": "NVDA", "headline": "...", "source": "Reuters",
                   "url"?: "...", "published_at"?: "YYYY-MM-DD", "to"?: "+15145550123"},
     "dry_run"?: true}

  without "to": every opted-in person holding the ticker gets it (shows the real pipeline);
  with "to":    only that number gets it, no profile needed (fallback if the demo account is not set up).

Nothing is marked as sent (record=False): the same demo can be rehearsed again, and the regular 15-minute run
only reads SEC filings, so it never re-sends a demo item.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.notify.contacts import mask, normalize_phone
from app.notify.messages import compose
from app.notify.news import NewsEvent

TZ = ZoneInfo("America/Toronto")
MAX_HEADLINE = 160       # the whole text stays within two SMS segments (messages.MAX_CHARS)


def demo_event(spec: dict[str, Any], today: str | None = None) -> NewsEvent:
    """{"ticker", "headline", "source"?, "url"?, "published_at"?} -> NewsEvent; ValueError if incomplete."""
    ticker = str(spec.get("ticker") or "").strip().upper()
    headline = " ".join(str(spec.get("headline") or "").split()).rstrip(".")
    if not ticker or not headline:
        raise ValueError("demo_news needs a ticker and a headline.")
    if len(headline) > MAX_HEADLINE:
        raise ValueError(f"headline too long ({len(headline)} > {MAX_HEADLINE} characters): one SMS is short.")
    day = spec.get("published_at") or today or datetime.now(TZ).date().isoformat()
    source = str(spec.get("source") or "News").strip()
    eid = "DEMO-" + hashlib.sha1(f"{ticker}|{headline}|{day}".encode()).hexdigest()[:10]
    return NewsEvent(event_id=eid, ticker=ticker, published_at=day, headline=headline, source=source,
                     url=str(spec.get("url") or ""))


class ScriptedNewsSource:
    """A fixed list of news items, whatever the window (the demo item is "now")."""

    def __init__(self, events: list[NewsEvent]):
        self.items = events

    def events(self, tickers: Iterable[str], since: str, until: str) -> list[NewsEvent]:
        held = set(tickers)
        return [e for e in self.items if e.ticker in held]


def run_demo(spec: dict[str, Any], contacts, profiles, sender, app_url: str | None = None) -> dict[str, Any]:
    from app.notify.alerts import run_alerts   # here: alerts imports this module

    event = demo_event(spec)
    if spec.get("to"):
        phone = normalize_phone(str(spec["to"]))
        text = compose([event], app_url)
        mid = sender.send(phone, text)
        return {"demo": True, "mode": "direct", "news": f"{event.ticker} {event.headline}", "texts_sent": 1,
                "texts_failed": 0, "people": [{"phone": mask(phone), "message_id": mid, "text": text}]}
    report = run_alerts(contacts, profiles, ScriptedNewsSource([event]), sender, event.published_at,
                        event.published_at, app_url, record=False)
    return {"demo": True, "mode": "holders", "news": f"{event.ticker} {event.headline}", **report}
