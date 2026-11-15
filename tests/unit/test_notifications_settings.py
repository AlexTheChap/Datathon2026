"""Notification choice (channel, alert types), account deletion, and the storage default (no setting needed)."""
import pytest
from app.api.app import create_app
from app.api.service import ApiService, NoDispatcher, service_from_env
from app.intake import MockBackend
from app.notify.alerts import DryRunSender, run_alerts
from app.notify.contacts import (
    InMemoryContactStore,
    JsonFileContactStore,
    NotifyPrefsIn,
    SmsContact,
    SmsPrefsIn,
    save_notify,
    save_prefs,
)
from app.notify.news import NewsEvent
from app.schemas import Profile
from app.store.ledger import InMemoryLedger
from app.store.profiles import InMemoryProfileStore, JsonFileProfileStore
from app.store.runs import InMemoryRunStore
from fastapi.testclient import TestClient
from pydantic import ValidationError

FORM = {"risk_band": "moderate", "experience": "beginner", "language": "en"}


# ---------- The choice ----------

def test_text_needs_a_phone_and_email_needs_an_address():
    with pytest.raises(ValidationError, match="phone number"):
        SmsContact(actor_id="a", opt_in=True, channel="sms")
    with pytest.raises(ValidationError, match="email address is required"):
        SmsContact(actor_id="a", opt_in=True, channel="email")
    with pytest.raises(ValidationError, match="valid email"):
        SmsContact(actor_id="a", opt_in=True, channel="email", email="not-an-email")
    with pytest.raises(ValidationError, match="at least one alert"):
        SmsContact(actor_id="a", opt_in=True, channel="sms", phone="514 555 0123", alerts=[])


def test_only_the_channels_address_is_kept_and_opting_out_forgets_both():
    c = SmsContact(actor_id="a", opt_in=True, channel="email", email="me@x.org", phone="514 555 0123")
    assert c.email == "me@x.org" and c.phone is None
    off = SmsContact(actor_id="a", opt_in=False, channel="email", email="me@x.org", phone="514 555 0123")
    assert (off.channel, off.phone, off.email, off.consent_at) == (None, None, None, None)


def test_records_saved_before_channels_existed_read_as_text_news():
    old = SmsContact.model_validate({"actor_id": "a", "opt_in": True, "phone": "+15145550123",
                                     "consent_at": "2026-11-15T10:00:00+00:00", "updated_at": "2026-11-15T10:00:00+00:00"})
    assert old.channel == "sms" and old.alerts == ["news"] and old.texts_news


def test_email_defaults_to_the_account_address_and_consent_is_kept():
    store = InMemoryContactStore()
    first = save_notify(store, "a", NotifyPrefsIn(opt_in=True, channel="email", alerts=["down", "up", "up"]), "me@x.org")
    assert first.email == "me@x.org" and first.alerts == ["up", "down"] and first.consent_at
    again = save_notify(store, "a", NotifyPrefsIn(opt_in=True, channel="sms", phone="514-555-0123"), "me@x.org")
    assert again.consent_at == first.consent_at and again.phone == "+15145550123" and again.email is None


def test_the_old_text_route_keeps_the_chosen_alerts():
    store = InMemoryContactStore()
    save_notify(store, "a", NotifyPrefsIn(opt_in=True, channel="sms", phone="5145550123", alerts=["up", "news"]))
    assert save_prefs(store, "a", SmsPrefsIn(opt_in=True, phone="5145550123")).alerts == ["up", "news"]


# ---------- The SMS news sender follows the choice ----------

class OneNews:
    def events(self, tickers, since, until):
        return [NewsEvent(event_id="e1", ticker="AAPL", published_at="2026-11-10", source="SEC 8-K",
                          headline="financial results", url="https://www.sec.gov/x")]


def test_news_texts_go_only_to_people_who_chose_texts_and_news():
    contacts, profiles = InMemoryContactStore(), InMemoryProfileStore()
    for actor in ("sms-news", "sms-moves", "email-news"):
        profiles.put(Profile(actor_id=actor, portfolio={"AAPL": 1.0}, **FORM))
    save_notify(contacts, "sms-news", NotifyPrefsIn(opt_in=True, channel="sms", phone="5145550101", alerts=["news"]))
    save_notify(contacts, "sms-moves", NotifyPrefsIn(opt_in=True, channel="sms", phone="5145550102", alerts=["up", "down"]))
    save_notify(contacts, "email-news", NotifyPrefsIn(opt_in=True, channel="email", email="e@x.org", alerts=["news"]))
    sender = DryRunSender()
    report = run_alerts(contacts, profiles, OneNews(), sender, "2026-11-01", "2026-11-12")
    assert [phone for phone, _ in sender.sent] == ["+15145550101"] and report["opted_in"] == 1


# ---------- Through the API ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    svc = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher(), runs=InMemoryRunStore(),
                     ledger=InMemoryLedger())
    return TestClient(create_app(svc)), svc


def H(actor):
    return {"X-Actor-Id": actor}


def test_get_and_put_notifications(client):
    c, _ = client
    off = c.get("/v1/notifications", headers=H("a")).json()
    assert off["opt_in"] is False and [x["id"] for x in off["choices"]] == ["up", "down", "news"]
    r = c.put("/v1/notifications", json={"opt_in": True, "channel": "sms", "phone": "514 555 0123",
                                         "alerts": ["news", "down"]}, headers=H("a"))
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "sms" and body["phone"] == "+15145550123" and body["alerts"] == ["down", "news"]
    assert c.get("/v1/notifications/sms", headers=H("a")).json()["opt_in"] is True        # same item as the text route
    bad = c.put("/v1/notifications", json={"opt_in": True, "channel": "email"}, headers=H("a"))
    assert bad.status_code == 422 and "email address" in bad.json()["detail"][0]["msg"]  # dev mode: no account email
    assert c.get("/v1/notifications", headers=H("b")).json()["opt_in"] is False          # per person
    assert c.put("/v1/notifications", json={"opt_in": True, "actor_id": "b"}, headers=H("a")).status_code == 422
    out = c.put("/v1/notifications", json={"opt_in": False}, headers=H("a")).json()
    assert out["opt_in"] is False and out["phone"] is None


def test_email_users_are_off_for_the_text_route(client):
    c, _ = client
    c.put("/v1/notifications", json={"opt_in": True, "channel": "sms", "phone": "5145550123"}, headers=H("a"))
    c.put("/v1/notifications", json={"opt_in": False}, headers=H("a"))
    assert c.get("/v1/notifications/sms", headers=H("a")).json()["opt_in"] is False


def test_delete_account_erases_profile_notifications_runs_and_evidence(client):
    c, svc = client
    c.put("/v1/profile", json=FORM, headers=H("a"))
    c.put("/v1/profile", json=FORM, headers=H("b"))
    c.put("/v1/notifications", json={"opt_in": True, "channel": "sms", "phone": "5145550123"}, headers=H("a"))
    assert c.post("/v1/runs", json={"text": "Is my portfolio too risky?"}, headers=H("a")).status_code == 202
    run_id = svc.runs.list_for_actor("a")[0].run_id
    svc.ledger.append(run_id, {"tool": "metrics", "evidence_id": "ev-1"})
    assert c.delete("/v1/account").status_code == 401                                    # signed-in callers only
    r = c.delete("/v1/account", headers=H("a"))
    assert r.status_code == 200 and r.json() == {"deleted": True, "runs": 1}
    assert svc.profiles.get("a") is None and svc.contacts.get("a") is None
    assert svc.runs.list_for_actor("a") == [] and svc.ledger.records(run_id) == []
    assert svc.profiles.get("b") is not None                                             # nobody else's data


def test_file_stores_delete_too(tmp_path):
    profiles, contacts = JsonFileProfileStore(tmp_path / "p"), JsonFileContactStore(tmp_path / "c")
    profiles.put(Profile(actor_id="a", **FORM))
    save_notify(contacts, "a", NotifyPrefsIn(opt_in=True, channel="sms", phone="5145550123"))
    contacts.mark_sent("a", "e1", "now")
    profiles.delete("a")
    contacts.delete("a")
    assert profiles.get("a") is None and contacts.get("a") is None and not contacts.was_sent("a", "e1")
    assert list((tmp_path / "p").iterdir()) == [] and list((tmp_path / "c").iterdir()) == []


# ---------- Storage needs no setting ----------

def test_storage_default_is_file_locally_and_dynamodb_on_lambda(tmp_path, monkeypatch):
    svc = service_from_env({"AF_DATA_DIR": str(tmp_path), "AF_DISPATCH": "none"})
    assert isinstance(svc.profiles, JsonFileProfileStore) and isinstance(svc.contacts, JsonFileContactStore)
    import boto3

    monkeypatch.setattr(boto3, "resource", lambda *a, **k: type("R", (), {"Table": lambda self, n: object()})())
    on_lambda = service_from_env({"AWS_LAMBDA_FUNCTION_NAME": "alphaforge-api", "AF_DISPATCH": "none"})
    assert type(on_lambda.profiles).__name__ == "DynamoDBProfileStore"
    assert type(on_lambda.contacts).__name__ == "DynamoDBContactStore"


def test_a_shared_demo_account_cannot_be_deleted():
    svc = ApiService(dispatcher=NoDispatcher())
    svc.ensure_demo_profile("demo-sub", "robert")
    from app.api.service import ApiError

    with pytest.raises(ApiError) as e:
        svc.delete_account("demo-sub", demo_persona="robert")
    assert e.value.status == 403
    assert svc.profiles.get("demo-sub") is not None            # nothing erased


def test_setup_cognito_resets_an_existing_demo_account(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("setup_cognito", root / "scripts" / "setup_cognito.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Idp:
        class exceptions:
            class UsernameExistsException(Exception):
                pass

        def __init__(self):
            self.calls = []

        def admin_create_user(self, **kw):
            raise self.exceptions.UsernameExistsException()

        def admin_update_user_attributes(self, **kw):
            self.calls.append(("attrs", kw["Username"], kw["UserAttributes"]))

        def admin_set_user_password(self, **kw):
            self.calls.append(("password", kw["Username"], kw["Password"]))

    idp = Idp()
    mod.ensure_demo_users(idp, "pool", "Secret-123456")
    resets = [c for c in idp.calls if c[0] == "attrs"]
    assert {c[1] for c in resets} == {"amira.demo@alphaforge.example", "mateo.demo@alphaforge.example",
                                      "robert.demo@alphaforge.example"}
    assert all({"Name": "email", "Value": c[1]} in c[2] for c in resets)
    assert sum(1 for c in idp.calls if c[0] == "password") == 3
