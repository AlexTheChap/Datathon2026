"""Box ② API: routes, identity, intake → context → run flow, profile form, stores. Mock intake, no AWS."""
import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import actor_id_from, create_app, handler
from app.api.service import ApiService, MessageIn, service_from_env
from app.intake import MockBackend
from app.store.profiles import DynamoDBProfileStore
from app.store.runs import DynamoDBRunStore, InMemoryRunStore

FORM = {"risk_band": "low_moderate", "experience": "advanced", "language": "en"}


@pytest.fixture
def svc():
    return ApiService(intake_backend=MockBackend())


@pytest.fixture
def client(svc, monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    return TestClient(create_app(svc))


def post(client, actor, **body):
    return client.post("/v1/runs", json=body, headers={"X-Actor-Id": actor})


# ---------- Flow: question / message / run ----------

def test_unknown_user_gets_one_question(client):
    r = post(client, "u-new", text="Is my portfolio too risky?")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "question" and body["question"].startswith("What level of risk")
    assert "risk_band" in body["missing_fields"] and "run_id" not in body


def test_unknown_user_asking_for_a_trade_is_told_to_create_a_profile(client):
    r = post(client, "u-new", text="Buy 100 Tesla shares for me right now.")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "message" and "create your investor profile" in body["message"]


def test_ready_request_creates_a_queued_run(client):
    r = post(client, "robert", text="Is my portfolio too risky compared to SPY? Show me a crash.", profile_form=FORM)
    assert r.status_code == 202
    body = r.json()
    assert body["kind"] == "run" and body["run_status"] == "QUEUED"
    assert body["enabled_agents"] == ["risk", "macro_scenarios"]

    run = client.get(f"/v1/runs/{body['run_id']}", headers={"X-Actor-Id": "robert"}).json()
    assert run["status"] == "QUEUED" and run["actor_id"] == "robert"
    assert run["execution_plan"]["enabled_agents"][0]["parameters"] == {"benchmark": "SPY"}
    assert run["prompt_ref"]["key"] == "explainer.advanced.md"
    assert run["note"]  # says box 5 is not connected yet


def test_out_of_scope_with_profile_still_creates_a_run_with_no_agents(client):
    """The Explainer will decline politely; box ④ planned nothing to analyse."""
    r = post(client, "robert", text="Buy 100 Tesla shares for me right now.", profile_form=FORM)
    assert r.status_code == 202 and r.json()["enabled_agents"] == []


def test_another_user_cannot_read_my_run(client):
    run_id = post(client, "robert", text="Is my portfolio too risky?", profile_form=FORM).json()["run_id"]
    assert client.get(f"/v1/runs/{run_id}", headers={"X-Actor-Id": "mallory"}).status_code == 404
    assert client.get("/v1/runs/run-doesnotexist", headers={"X-Actor-Id": "robert"}).status_code == 404


# ---------- Profile saved across messages (incl. toggles said in chat) ----------

def test_profile_from_the_first_message_is_reused(client):
    post(client, "robert", text="Is my portfolio too risky?", profile_form=FORM)
    r = post(client, "robert", text="What would happen in a crash?")  # no form this time
    assert r.status_code == 202


def test_news_switched_off_in_chat_stays_off(client):
    first = post(client, "robert", text="I don't care about the news. Is my portfolio too risky?", profile_form=FORM)
    assert [c["agent"] for c in first.json()["toggle_changes"]] == ["news"]
    second = post(client, "robert", text="I'm thinking of putting 20% in Nvidia, everyone is talking about it.")
    assert "news" not in second.json()["enabled_agents"]
    run = client.get(f"/v1/runs/{second.json()['run_id']}", headers={"X-Actor-Id": "robert"}).json()
    news = next(d for d in run["execution_plan"]["disabled_agents"] if d["agent"] == "news")
    assert "don't care about the news" in news["reason"]
    profile = client.get("/v1/profile", headers={"X-Actor-Id": "robert"}).json()
    assert profile["agent_toggles"]["news"] is False


# ---------- Profile form ----------

def test_profile_form_roundtrip_and_partial_update(client):
    h = {"X-Actor-Id": "amira"}
    assert client.get("/v1/profile", headers=h).status_code == 404
    r = client.put("/v1/profile", json={"risk_band": "high", "experience": "intermediate", "language": "en",
                                        "horizon_years": {"min": 15, "max": None}}, headers=h)
    assert r.status_code == 200 and r.json()["actor_id"] == "amira"
    r = client.put("/v1/profile", json={"agent_toggles": {"news": False}}, headers=h)
    assert r.json()["agent_toggles"]["news"] is False and r.json()["agent_toggles"]["esg"] is True
    assert r.json()["risk_band"] == "high"  # untouched by the partial update
    assert post(client, "amira", text="Is my portfolio too risky?").status_code == 202  # profile now known


def test_profile_form_cannot_change_identity_or_forge_quotes(client):
    r = client.put("/v1/profile", json={**FORM, "actor_id": "someone-else",
                                        "agent_toggle_reasons": {"news": "I never said that"}},
                   headers={"X-Actor-Id": "robert"})
    assert r.json()["actor_id"] == "robert" and r.json()["agent_toggle_reasons"] == {}


def test_incomplete_or_invalid_profile_form_is_rejected(client):
    h = {"X-Actor-Id": "u"}
    assert client.put("/v1/profile", json={"risk_band": "high"}, headers=h).status_code == 422
    assert client.put("/v1/profile", json={**FORM, "risk_band": "yolo"}, headers=h).status_code == 422


# ---------- Input validation and identity ----------

@pytest.mark.parametrize("body", [{"text": ""}, {"text": "x" * 4001}, {"text": "hi", "unexpected": 1}, {}])
def test_bad_bodies_are_rejected(client, body):
    assert client.post("/v1/runs", json=body, headers={"X-Actor-Id": "u"}).status_code == 422


def test_no_identity_is_401_outside_dev_mode(svc, monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "jwt")
    c = TestClient(create_app(svc))
    assert c.post("/v1/runs", json={"text": "hi"}, headers={"X-Actor-Id": "robert"}).status_code == 401
    assert c.get("/health").status_code == 200


def test_actor_comes_from_the_verified_jwt_claims(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "jwt")

    class Req:
        scope = {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "cognito-sub-123"}}}}}}
        headers = {"x-actor-id": "spoofed"}

    assert actor_id_from(Req()) == "cognito-sub-123"


def test_lambda_handler_through_mangum(monkeypatch):
    """API Gateway HTTP API (payload v2) event with JWT claims → Mangum → FastAPI."""
    event = {
        "version": "2.0", "routeKey": "GET /health", "rawPath": "/health", "rawQueryString": "",
        "headers": {"host": "api.example"},
        "requestContext": {"http": {"method": "GET", "path": "/health", "sourceIp": "1.2.3.4",
                                    "protocol": "HTTP/1.1", "userAgent": "test"},
                           "authorizer": {"jwt": {"claims": {"sub": "abc"}}},
                           "stage": "$default", "requestId": "r1", "routeKey": "GET /health"},
        "isBase64Encoded": False,
    }
    out = handler(event, None)
    assert out["statusCode"] == 200 and json.loads(out["body"]) == {"status": "ok"}


# ---------- DynamoDB stores (fake table, no AWS) ----------

class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}


def test_dynamodb_stores_roundtrip_with_planned_keys():
    table = FakeTable()
    svc = ApiService(profiles=DynamoDBProfileStore(table), runs=DynamoDBRunStore(table), intake_backend=MockBackend())
    _, out = svc.post_message("robert", MessageIn(text="Is my portfolio too risky?", profile_form=FORM))
    assert ("ACTOR#robert", "PROFILE#current") in table.items
    assert (f"RUN#{out.run_id}", "META") in table.items
    assert svc.get_run("robert", out.run_id).execution_plan.enabled_names == ["risk"]
    assert svc.get_profile("robert").risk_band == "low_moderate"


def test_service_from_env():
    svc = service_from_env({})
    assert isinstance(svc, ApiService) and isinstance(svc.runs, InMemoryRunStore)
    with pytest.raises(ValueError):
        service_from_env({"AF_STORE": "postgres"})


# ---------- Example personas and the served frontend ----------

def test_personas_are_ready_to_save_profile_forms(client):
    personas = client.get("/v1/personas").json()
    assert [p["id"] for p in personas] == ["amira", "mateo", "robert"]
    for p in personas:
        assert "actor_id" not in p["profile_form"]
        r = client.put("/v1/profile", json=p["profile_form"], headers={"X-Actor-Id": f"try-{p['id']}"})
        assert r.status_code == 200, p["id"]
    robert = personas[2]
    assert robert["profile_form"]["exclusions"] == [{"type": "asset_class", "value": "crypto", "min_severity": None}]
    assert robert["example_question"].startswith("I'm thinking of putting 20% in Nvidia")


def test_frontend_is_served_and_api_routes_still_win(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>AlphaForge" in page.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}  # API route, not a static file


def test_frontend_serving_can_be_turned_off(svc, monkeypatch):
    monkeypatch.setenv("AF_SERVE_FRONTEND", "0")
    assert TestClient(create_app(svc)).get("/").status_code == 404
