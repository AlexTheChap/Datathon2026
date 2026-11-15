"""News alerts by SMS (app.notify): contact preferences, news sources, wording, the alert agent, the API routes.

Offline: no AWS, no SEC call, no text sent (fake opener, DryRunSender, fake DynamoDB table).
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import ApiService, service_from_env
from app.intake import MockBackend
from app.notify import alerts
from app.notify.alerts import DryRunSender, SnsSmsSender, default_window, held_tickers, run_alerts
from app.notify.contacts import (
    CONTACT_SK,
    DynamoDBContactStore,
    InMemoryContactStore,
    JsonFileContactStore,
    SmsContact,
    SmsPrefsIn,
    mask,
    normalize_phone,
    save_prefs,
)
from app.notify.messages import CLOSING, MAX_CHARS, AdviceWordingError, check_wording, compose
from app.notify.news import CorpusNewsSource, NewsEvent, SecFilingsSource
from app.schemas import Profile
from app.store.profiles import InMemoryProfileStore


def ev(ticker, day, headline="financial results", eid=None):
    return NewsEvent(event_id=eid or f"N-{ticker}-{day}", ticker=ticker, published_at=day, headline=headline,
                     source="SEC 8-K filing", url="https://www.sec.gov/x")


class ListSource:
    def __init__(self, events):
        self.items, self.asked = events, []

    def events(self, tickers, since, until):
        self.asked.append(sorted(tickers))
        return [e for e in self.items if e.ticker in set(tickers) and since < e.published_at <= until]


def profile(actor, portfolio):
    return Profile(actor_id=actor, risk_band="moderate", experience="beginner", language="en", portfolio=portfolio)


# ---------- contacts ----------

@pytest.mark.parametrize("raw", ["(514) 555-0123", "514-555-0123", "+1 514 555 0123", "15145550123"])
def test_phone_numbers_are_normalized(raw):
    assert normalize_phone(raw) == "+15145550123"


@pytest.mark.parametrize("raw", ["555-0123", "+44 20 7946 0958", "123 456 7890", ""])
def test_bad_phone_numbers_are_refused(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_opting_out_forgets_the_number_and_opting_in_needs_one():
    assert SmsContact(actor_id="a", opt_in=False, phone="5145550123").phone is None
    with pytest.raises(ValueError):
        SmsContact(actor_id="a", opt_in=True)
    assert mask("+15145550123").endswith("0123") and "514" not in mask("+15145550123")


def test_consent_time_is_kept_while_opted_in():
    s = InMemoryContactStore()
    first = save_prefs(s, "a", SmsPrefsIn(opt_in=True, phone="5145550123"))
    again = save_prefs(s, "a", SmsPrefsIn(opt_in=True, phone="4385550199"))
    assert first.consent_at and again.consent_at == first.consent_at and again.phone == "+14385550199"
    off = save_prefs(s, "a", SmsPrefsIn(opt_in=False, phone="4385550199"))
    assert off.phone is None and off.consent_at is None and s.opted_in() == []


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = Item

    def scan(self, **kwargs):
        return {"Items": [i for (pk, sk), i in self.items.items() if sk == CONTACT_SK and i.get("opt_in")]}


@pytest.mark.parametrize("make", [lambda tmp: InMemoryContactStore(), lambda tmp: JsonFileContactStore(tmp),
                                  lambda tmp: DynamoDBContactStore(FakeTable())])
def test_every_store_keeps_choices_and_the_sent_log(make, tmp_path):
    s = make(tmp_path)
    s.put(SmsContact(actor_id="a", opt_in=True, phone="5145550123"))
    s.put(SmsContact(actor_id="b", opt_in=False))
    assert s.get("a").phone == "+15145550123" and s.get("zz") is None
    assert [c.actor_id for c in s.opted_in()] == ["a"]
    assert not s.was_sent("a", "N-1")
    s.mark_sent("a", "N-1", "2026-11-15T00:00:00+00:00")
    assert s.was_sent("a", "N-1") and not s.was_sent("b", "N-1")


# ---------- wording ----------

def test_a_text_is_short_factual_and_closes_with_the_disclaimer():
    text = compose([ev("NVDA", "2026-11-12", "executive departure or appointment"), ev("JNJ", "2026-10-13")])
    assert text.startswith("Fitfolio: news on stocks you hold.")
    assert 'NVDA: "executive departure or appointment" (SEC 8-K filing, Nov 12).' in text
    assert text.endswith(CLOSING) and len(text) <= MAX_CHARS


def test_many_news_items_stay_within_two_segments():
    text = compose([ev(f"T{i}", "2026-11-12", "non-reliance on financial statements (restatement)") for i in range(8)],
                   app_url="https://app.example/#/portfolio")
    assert len(text) <= MAX_CHARS and "more in the app." in text and "https://app.example" in text


@pytest.mark.parametrize("word", ["Buy", "sell", "We recommend", "you should", "price target", "upgrade"])
def test_advice_wording_is_refused(word):
    with pytest.raises(AdviceWordingError):
        check_wording(f"Fitfolio: {word} this. {CLOSING}")


# ---------- news sources ----------

def test_corpus_source_returns_only_held_company_news_in_the_window():
    events = CorpusNewsSource().events(["NVDA", "JNJ", "XIU.TO"], "2026-09-01", "2026-11-12")
    assert {e.ticker for e in events} == {"NVDA", "JNJ"}
    assert all("2026-09-01" < e.published_at <= "2026-11-12" for e in events)
    assert CorpusNewsSource("missing.csv").events(["NVDA"], "2020-01-01", "2030-01-01") == []


def test_sec_source_reads_8k_filings_of_held_tickers():
    tickers = {"0": {"ticker": "NVDA", "cik_str": 1045810}, "1": {"ticker": "BRK.B", "cik_str": 1067983}}
    filings = {"filings": {"recent": {
        "form": ["8-K", "10-Q", "8-K", "8-K"], "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-26-4"],
        "filingDate": ["2026-11-12", "2026-11-12", "2026-11-01", "2026-11-11"],
        "items": ["5.02", "", "2.02", "9.01"], "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"]}}}
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.headers.get("User-agent")))
        body = tickers if req.full_url.endswith("company_tickers.json") else filings
        return io.BytesIO(json.dumps(body).encode())

    src = SecFilingsSource(contact="Fitfolio test@example.com", pause=0, opener=opener)
    events = src.events(["NVDA", "XIU.TO"], "2026-11-05", "2026-11-12")
    assert [(e.ticker, e.published_at, e.headline) for e in events] == [
        ("NVDA", "2026-11-12", "executive departure or appointment")]   # 10-Q, out of window, exhibits-only: out
    assert all(ua == "Fitfolio test@example.com" for _, ua in seen)
    assert src.cik("BRK-B") == "0001067983" and src.cik("XOM") == "0000034088"


def test_sec_source_needs_a_contact(monkeypatch):
    monkeypatch.delenv("AF_SEC_CONTACT", raising=False)
    with pytest.raises(ValueError):
        SecFilingsSource()


# ---------- the alert agent ----------

@pytest.fixture
def world():
    contacts, profiles = InMemoryContactStore(), InMemoryProfileStore()
    contacts.put(SmsContact(actor_id="robert", opt_in=True, phone="5145550123"))
    profiles.put(profile("robert", {"XIU.TO": 0.3, "JNJ": 0.1, "NVDA": 0.2, "XBB.TO": 0.4}))
    contacts.put(SmsContact(actor_id="amira", opt_in=True, phone="4385550199"))      # no portfolio yet
    contacts.put(SmsContact(actor_id="mateo", opt_in=False))                         # said no
    profiles.put(profile("mateo", {"NVDA": 1.0}))
    source = ListSource([ev("NVDA", "2026-11-12"), ev("AAPL", "2026-11-12"), ev("JNJ", "2026-11-10")])
    return contacts, profiles, source


def test_only_people_who_opted_in_get_news_on_stocks_they_hold(world):
    contacts, profiles, source = world
    sender = DryRunSender()
    r = run_alerts(contacts, profiles, source, sender, "2026-11-09", "2026-11-12")
    assert source.asked == [["JNJ", "NVDA", "XBB.TO", "XIU.TO"]]       # held tickers only, fetched once
    assert r["texts_sent"] == 1 and [p for p, _ in sender.sent] == ["+15145550123"]
    text = sender.sent[0][1]
    assert "NVDA" in text and "JNJ" in text and "AAPL" not in text     # AAPL is not held
    by_actor = {p["actor_id"]: p for p in r["people"]}
    assert by_actor["amira"]["skipped"] == "no saved portfolio" and "mateo" not in by_actor
    assert "514" not in json.dumps({k: v for k, v in by_actor["robert"].items() if k != "text"})  # masked


def test_the_same_news_is_never_texted_twice_but_a_dry_run_records_nothing(world):
    contacts, profiles, source = world
    dry = run_alerts(contacts, profiles, source, DryRunSender(), "2026-11-09", "2026-11-12", record=False)
    assert dry["texts_sent"] == 1
    first = run_alerts(contacts, profiles, source, DryRunSender(), "2026-11-09", "2026-11-12")
    assert first["texts_sent"] == 1                                     # the dry run did not consume it
    second = run_alerts(contacts, profiles, source, DryRunSender(), "2026-11-09", "2026-11-12")
    assert second["texts_sent"] == 0


def test_advice_wording_blocks_the_text(world, monkeypatch):
    contacts, profiles, _ = world
    source = ListSource([ev("NVDA", "2026-11-12", headline="analysts recommend it")])
    sender = DryRunSender()
    r = run_alerts(contacts, profiles, source, sender, "2026-11-09", "2026-11-12")
    assert sender.sent == [] and "advice wording" in r["people"][0]["skipped"]
    assert not contacts.was_sent("robert", "N-NVDA-2026-11-12")


def test_held_tickers_and_default_window():
    from datetime import date

    assert held_tickers(profile("a", {"NVDA": 0.5, "JNJ": 0.0, "SPY": 0.5})) == ["NVDA", "SPY"]
    assert held_tickers(None) == []
    assert default_window(date(2026, 11, 16), 3) == ("2026-11-13", "2026-11-16")


def test_sns_sender_publishes_a_transactional_sms():
    calls = []

    class FakeSns:
        def publish(self, **kw):
            calls.append(kw)
            return {"MessageId": "m-1"}

    assert SnsSmsSender(FakeSns()).send("+15145550123", "hello") == "m-1"
    assert calls[0]["PhoneNumber"] == "+15145550123"
    assert calls[0]["MessageAttributes"]["AWS.SNS.SMS.SMSType"]["StringValue"] == "Transactional"


def test_lambda_handler_uses_the_sec_source_and_dry_run(world, monkeypatch, capsys):
    contacts, profiles, source = world
    monkeypatch.setattr(alerts, "stores_from_env", lambda: (contacts, profiles))
    monkeypatch.setattr(alerts, "SecFilingsSource", lambda: source)
    monkeypatch.setattr(alerts, "SnsSmsSender", lambda: pytest.fail("a dry run must not build the SNS sender"))
    r = alerts.handler({"dry_run": True, "since": "2026-11-09", "until": "2026-11-12"})
    assert r["texts_sent"] == 1 and not contacts.was_sent("robert", "N-NVDA-2026-11-12")
    logged = capsys.readouterr().out
    assert "5145550123" not in logged and "Fitfolio: news" not in logged   # CloudWatch: no number, no text


# ---------- API ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    return TestClient(create_app(ApiService(intake_backend=MockBackend())))


def test_api_reads_and_saves_the_text_alert_choice(client):
    h = {"X-Actor-Id": "u-sms"}
    assert client.get("/v1/notifications/sms", headers=h).json()["opt_in"] is False
    r = client.put("/v1/notifications/sms", json={"opt_in": True, "phone": "514 555 0123"}, headers=h)
    assert r.status_code == 200 and r.json()["phone"] == "+15145550123" and r.json()["consent_at"]
    assert client.get("/v1/notifications/sms", headers={"X-Actor-Id": "someone-else"}).json()["phone"] is None
    off = client.put("/v1/notifications/sms", json={"opt_in": False}, headers=h).json()
    assert off["opt_in"] is False and off["phone"] is None


def test_api_explains_a_bad_number(client):
    r = client.put("/v1/notifications/sms", json={"opt_in": True, "phone": "123"}, headers={"X-Actor-Id": "u"})
    assert r.status_code == 422 and "Canadian or U.S. mobile number" in json.dumps(r.json())
    r = client.put("/v1/notifications/sms", json={"opt_in": True}, headers={"X-Actor-Id": "u"})
    assert r.status_code == 422 and "phone number is required" in json.dumps(r.json())


def test_the_phone_number_never_enters_the_profile(client):
    h = {"X-Actor-Id": "u-p"}
    client.put("/v1/profile", json={"risk_band": "moderate", "experience": "beginner", "language": "en"}, headers=h)
    client.put("/v1/notifications/sms", json={"opt_in": True, "phone": "5145550123"}, headers=h)
    assert "5550123" not in json.dumps(client.get("/v1/profile", headers=h).json())


def test_file_store_keeps_contacts_apart_from_profiles(tmp_path):
    svc = service_from_env({"AF_STORE": "file", "AF_DATA_DIR": str(tmp_path)})
    svc.put_sms("a", SmsPrefsIn(opt_in=True, phone="5145550123"))
    assert (tmp_path / "contacts" / "a.json").exists() and not (tmp_path / "profiles" / "a.json").exists()


def test_a_failed_text_does_not_stop_the_others_and_is_retried(world):
    contacts, profiles, source = world
    contacts.put(SmsContact(actor_id="zoe", opt_in=True, phone="4165550111"))
    profiles.put(profile("zoe", {"NVDA": 1.0}))

    class FlakySender(DryRunSender):
        def send(self, phone, text):
            if phone == "+15145550123":
                raise RuntimeError("InvalidParameter: phone")
            return super().send(phone, text)

    sender = FlakySender()
    r = run_alerts(contacts, profiles, source, sender, "2026-11-09", "2026-11-12")
    by_actor = {p["actor_id"]: p for p in r["people"]}
    assert "InvalidParameter" in by_actor["robert"]["error"] and r["texts_failed"] == 1
    assert [p for p, _ in sender.sent] == ["+14165550111"] and r["texts_sent"] == 1      # zoe still gets hers
    assert not contacts.was_sent("robert", "N-NVDA-2026-11-12")                            # retried next run
    assert contacts.was_sent("zoe", "N-NVDA-2026-11-12")
