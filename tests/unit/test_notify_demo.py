"""Demo mode of the news-alert agent (app.notify.demo) and the scheduling helper. Offline: DryRunSender."""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from app.notify import alerts
from app.notify.alerts import DryRunSender
from app.notify.contacts import InMemoryContactStore, SmsContact
from app.notify.demo import MAX_HEADLINE, ScriptedNewsSource, demo_event, run_demo
from app.notify.messages import CLOSING, MAX_CHARS, AdviceWordingError
from app.schemas import Profile
from app.store.profiles import InMemoryProfileStore

SPEC = {"ticker": "nvda", "headline": "Nvidia unveils a new AI chip.", "source": "Reuters",
        "published_at": "2026-11-20"}


def stores():
    contacts, profiles = InMemoryContactStore(), InMemoryProfileStore()
    for actor, phone, portfolio in (("ana", "5145550123", {"NVDA": 0.5, "XOM": 0.5}),
                                    ("bob", "4385550199", {"XOM": 1.0})):
        contacts.put(SmsContact(actor_id=actor, opt_in=True, channel="sms", phone=phone, alerts=["news"]))
        profiles.put(Profile(actor_id=actor, risk_band="moderate", experience="beginner", language="en",
                             portfolio=portfolio))
    return contacts, profiles


def test_demo_event_is_normalized_and_stable():
    e = demo_event(SPEC)
    assert (e.ticker, e.headline, e.source, e.published_at) == ("NVDA", "Nvidia unveils a new AI chip", "Reuters",
                                                                "2026-11-20")
    assert e.event_id.startswith("DEMO-") and e.event_id == demo_event(SPEC).event_id


@pytest.mark.parametrize("spec", [{"ticker": "NVDA"}, {"headline": "x"}, {"ticker": "NVDA", "headline": "x" * (MAX_HEADLINE + 1)}])
def test_incomplete_or_long_demo_news_is_refused(spec):
    with pytest.raises(ValueError):
        demo_event(spec)


def test_holders_mode_texts_only_people_holding_the_ticker_and_records_nothing():
    contacts, profiles = stores()
    sender = DryRunSender()
    r = run_demo(SPEC, contacts, profiles, sender)
    assert r["mode"] == "holders" and r["texts_sent"] == 1
    assert [p for p, _ in sender.sent] == ["+15145550123"]
    text = sender.sent[0][1]
    assert "NVDA: Reuters on Nov 20 (Nvidia unveils a new AI chip)." in text and text.endswith(CLOSING)
    assert len(text) <= MAX_CHARS
    run_demo(SPEC, contacts, profiles, sender)           # rehearsal: sent again (nothing marked as sent)
    assert len(sender.sent) == 2


def test_direct_mode_needs_no_profile():
    sender = DryRunSender()
    r = run_demo({**SPEC, "to": "(514) 555-0123"}, InMemoryContactStore(), InMemoryProfileStore(), sender)
    assert r["mode"] == "direct" and sender.sent[0][0] == "+15145550123"


def test_advice_wording_is_still_refused():
    with pytest.raises(AdviceWordingError):
        run_demo({**SPEC, "headline": "Analysts say you should buy Nvidia", "to": "5145550123"},
                 InMemoryContactStore(), InMemoryProfileStore(), DryRunSender())


def test_scripted_source_ignores_the_window():
    src = ScriptedNewsSource([demo_event(SPEC)])
    assert len(src.events(["NVDA"], "2000-01-01", "2000-01-02")) == 1 and src.events(["XOM"], "", "z") == []


def test_lambda_handler_routes_demo_news(monkeypatch):
    contacts, profiles = stores()
    monkeypatch.setattr(alerts, "stores_from_env", lambda: (contacts, profiles))
    r = alerts.handler({"demo_news": SPEC, "dry_run": True})
    assert r["demo"] and r["texts_sent"] == 1


def _script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "demo_news_sms.py"
    spec = importlib.util.spec_from_file_location("demo_news_sms", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_when():
    m = _script()
    now = datetime(2026, 11, 20, 14, 0, 0, tzinfo=m.TZ)
    assert m.parse_when("2026-11-20 14:30", None, now) == datetime(2026, 11, 20, 14, 30, tzinfo=m.TZ)
    assert m.parse_when("15:05", None, now).hour == 15
    assert m.parse_when(None, "2m", now) == datetime(2026, 11, 20, 14, 2, tzinfo=m.TZ)
    for at, in_ in (("13:59", None), (None, "2x"), (None, None)):
        with pytest.raises(ValueError):
            m.parse_when(at, in_, now)
