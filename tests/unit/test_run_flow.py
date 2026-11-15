"""Run flow with the Explainer: dispatcher → orchestrator → Explainer → RunRecord.result["answer"], the background
thread (LocalDispatcher), and GET /v1/runs/{id}/evidence/catalog. No AWS: fake orchestrator, fake Bedrock client, real ledger fixture."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.explainer import explain
from app.api.app import create_app
from app.api.dispatch import InlineDispatcher
from app.api.dispatcher import LocalDispatcher
from app.api.service import ApiService, MessageIn, NoDispatcher, service_from_env
from app.context.prompt_loader import LocalPromptSource
from app.intake import MockBackend
from app.schemas.models import OrchestratorOutput
from app.store.runs import InMemoryRunStore

from test_explainer import SETTINGS, FakeClient, draft  # noqa: E402  (same fixtures as the Explainer tests)

LEDGER = [json.loads(line) for line in
          (Path(__file__).resolve().parents[1] / "fixtures" / "ledger_robert_nvda.jsonl").read_text().splitlines()]
FORM = {"risk_band": "low_moderate", "experience": "advanced", "language": "en"}


class FakeLedger:
    def records(self, run_id):
        return LEDGER


def queued_run(svc: ApiService, text="I'm thinking of putting 20% in Nvidia, everyone is talking about it."):
    status, out = svc.post_message("robert", MessageIn(text=text, profile_form=FORM))
    assert status == 202
    return svc.runs.get(out.run_id)


def fake_orchestrator(inp):
    return OrchestratorOutput(run_id=inp.run_id, summary="draft", evidence_ids=["ev-c5f01b9379"])


def explainer_with(client):
    return lambda run, orch, recs: explain(run, orch, recs, client=client, settings=SETTINGS,
                                           prompt_source=LocalPromptSource())


def test_dispatcher_stores_a_checked_answer():
    svc = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
    run = queued_run(svc)
    statuses = []
    orig = svc.runs.put
    svc.runs.put = lambda r: (statuses.append(r.status), orig(r))
    InlineDispatcher(svc.runs, run_fn=fake_orchestrator, explain_fn=explainer_with(FakeClient(draft())),
                     records_fn=FakeLedger().records).dispatch(run)
    saved = svc.runs.get(run.run_id)
    assert statuses == ["RUNNING", "DONE"]
    assert saved.result["summary"] == "draft"                       # the orchestrator's fields stay
    answer = saved.result["answer"]
    assert answer["kind"] == "explained" and answer["verification"]["claims_checked"] == 5
    assert answer["prompt"]["key"] == saved.prompt_ref.key           # the part ④ template the plan named


def test_explainer_crash_never_loses_the_run():
    svc = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
    run = queued_run(svc)

    def boom(*a):
        raise RuntimeError("explainer down")

    InlineDispatcher(svc.runs, run_fn=fake_orchestrator, explain_fn=boom, records_fn=FakeLedger().records).dispatch(run)
    saved = svc.runs.get(run.run_id)
    assert saved.status == "DONE" and saved.result["answer"] is None and "explainer down" in saved.result["answer_error"]


def test_failed_orchestrator_still_gets_an_honest_answer():
    svc = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
    run = queued_run(svc)

    def boom(inp):
        raise RuntimeError("bedrock down")

    client = FakeClient()
    InlineDispatcher(svc.runs, run_fn=boom, explain_fn=explainer_with(client), records_fn=lambda _: []).dispatch(run)
    saved = svc.runs.get(run.run_id)
    assert saved.status == "FAILED" and saved.result["answer"]["kind"] == "fallback" and client.calls == []


def test_local_dispatcher_runs_the_explainer_in_the_background():
    runs = InMemoryRunStore()
    svc = ApiService(intake_backend=MockBackend(), runs=runs, dispatcher=LocalDispatcher(
        runs, runner=fake_orchestrator, explain_fn=explainer_with(FakeClient(draft())), records_fn=FakeLedger().records))
    status, out = svc.post_message("robert", MessageIn(text="I'm thinking of putting 20% in Nvidia.", profile_form=FORM))
    assert status == 202  # the API answered at once; the analysis runs in a thread
    end = time.time() + 5
    while runs.get(out.run_id).status != "DONE" and time.time() < end:
        time.sleep(0.01)
    answer = runs.get(out.run_id).result["answer"]
    assert answer["kind"] == "explained" and answer["verification"]["claims_checked"] == 5


def test_every_dispatch_mode_gets_the_explainer():
    assert isinstance(service_from_env({"AF_STORE": "memory"}).dispatcher, InlineDispatcher)             # queue-free default
    assert isinstance(service_from_env({"AF_STORE": "memory", "AF_DISPATCH": "local"}).dispatcher, LocalDispatcher)
    assert isinstance(service_from_env({"AF_STORE": "memory", "AF_DISPATCH": "none"}).dispatcher, NoDispatcher)
    with pytest.raises(ValueError):
        service_from_env({"AF_STORE": "memory", "AF_DISPATCH": "lambda"})


def test_evidence_catalog_endpoint_is_owner_only(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    svc = ApiService(intake_backend=MockBackend(), runs=InMemoryRunStore(), ledger=FakeLedger(), dispatcher=NoDispatcher())
    client = TestClient(create_app(svc))
    run_id = client.post("/v1/runs", json={"text": "Is my portfolio too risky?", "profile_form": FORM},
                         headers={"X-Actor-Id": "robert"}).json()["run_id"]
    items = client.get(f"/v1/runs/{run_id}/evidence/catalog", headers={"X-Actor-Id": "robert"}).json()
    by_id = {i["id"]: i for i in items}
    assert "ev-9de6c73e44" in by_id and by_id["ev-c5f01b9379"]["say"].startswith("In 2022")
    assert by_id["ev-c5f01b9379"]["data_sources"]  # sources shown in the panel
    assert client.get(f"/v1/runs/{run_id}/evidence/catalog", headers={"X-Actor-Id": "mallory"}).status_code == 404
    raw = client.get(f"/v1/runs/{run_id}/evidence", headers={"X-Actor-Id": "robert"}).json()
    assert raw == LEDGER  # the raw ledger stays available for the dashboard
