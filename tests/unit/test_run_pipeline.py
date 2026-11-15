"""Run pipeline: the orchestrator, the local dispatcher (orchestrator, then the Explainer) and the ledger. Scripted
fakes only (no AWS, no LLM). The Explainer itself is tested in test_explainer.py and test_run_flow.py."""
from __future__ import annotations

import threading
import time
from typing import ClassVar

import pytest
from app.agents import orchestrator
from app.agents.subagents.base import run_context
from app.api.dispatcher import LocalDispatcher, payload_from_run
from app.api.service import ApiService, MessageIn, NoDispatcher, service_from_env
from app.context.plan_resolver import resolve_plan
from app.core.config import Settings
from app.intake import MockBackend
from app.schemas.models import OrchestratorInput, OrchestratorOutput
from app.schemas.run_result import RunAnswer
from app.store.ledger import InMemoryLedger, JsonlLedger
from app.store.runs import InMemoryRunStore
from app.tools.context import current_as_of, current_profile, default_as_of

# ---------- orchestrator ----------


class FakeOrchestratorAgent:
    """Stands for the Strands agent: records the run context it sees, returns working notes."""
    seen: ClassVar[dict] = {}

    def __call__(self, message):
        FakeOrchestratorAgent.seen = {"as_of": current_as_of.get(), "profile": current_profile.get(),
                                      "ctx": run_context.get(), "message": message}
        return "working notes"


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setattr(orchestrator, "build_orchestrator", lambda *a, **k: FakeOrchestratorAgent())


def payload(make_profile, make_request, task="risk_review", plan_update=None, **req):
    profile, request = make_profile("robert"), make_request("persona-robert", task, **req)
    plan = resolve_plan(profile, request)
    if plan_update:
        plan = plan.model_copy(update=plan_update)
    return OrchestratorInput(run_id="run-1", actor_id=request.actor_id, profile=profile, request=request,
                             execution_plan=plan)


def test_run_returns_the_orchestrator_draft_not_the_answer(build, make_profile, make_request, tmp_path):
    """The answer is the Explainer's job, after the orchestrator (dispatch.attach_answer)."""
    out = orchestrator.run_orchestrator(payload(make_profile, make_request), Settings(runs_dir=tmp_path))
    assert out.status == "DONE" and out.summary == "working notes" and out.as_of == "2026-11-12"


def test_tools_see_the_run_date_and_profile(build, make_profile, make_request, tmp_path):
    orchestrator.run_orchestrator(payload(make_profile, make_request, as_of="2022-06-30"),
                                  Settings(runs_dir=tmp_path))
    seen = FakeOrchestratorAgent.seen
    assert seen["as_of"] == "2022-06-30" and seen["ctx"]["as_of"] == "2022-06-30"
    assert seen["profile"]["risk_band"] == "low_moderate" and "Reference date (as_of): 2022-06-30" in seen["message"]


def test_as_of_comes_from_payload_then_request_then_the_curated_data_never_a_constant(
        build, make_profile, make_request, tmp_path):
    s = Settings(runs_dir=tmp_path)
    inp = payload(make_profile, make_request, as_of=None)
    assert orchestrator.resolve_as_of(inp) == default_as_of() == "2026-11-12"
    assert orchestrator.resolve_as_of(inp.model_copy(update={"as_of": "2023-01-02"})) == "2023-01-02"
    out = orchestrator.run_orchestrator(inp.model_copy(update={"as_of": "2024-05-06"}), s)
    assert out.as_of == "2024-05-06"


# ---------- ledger ----------

@pytest.mark.parametrize("make", [lambda p: JsonlLedger(p), lambda p: InMemoryLedger()])
def test_ledger_is_thread_safe_and_numbers_calls_contiguously(make, tmp_path):
    led = make(tmp_path)

    def work(k):
        for i in range(25):
            led.append("run-t", {"tool": "t", "evidence_id": f"ev-{k:02d}{i:08d}"})
    threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    seqs = [r["seq"] for r in led.records("run-t")]
    assert sorted(seqs) == [f"E-{i:03d}" for i in range(1, 201)] and len(led.evidence_ids("run-t")) == 200


def test_jsonl_ledger_continues_the_numbering_after_a_restart(tmp_path):
    JsonlLedger(tmp_path).append("run-r", {"tool": "a"})
    again = JsonlLedger(tmp_path)
    assert again.append("run-r", {"tool": "b"})["seq"] == "E-002"


# ---------- local dispatcher ----------

FORM = {"risk_band": "low_moderate", "experience": "advanced", "language": "en"}


def done_output(inp: OrchestratorInput) -> OrchestratorOutput:
    return OrchestratorOutput(run_id=inp.run_id, status="DONE", summary="the answer", model="m")


def wait_for(runs, run_id, status, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        run = runs.get(run_id)
        if run.status == status:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run still {runs.get(run_id).status}, expected {status}")


def fake_explain(run, orchestrator, records):
    return RunAnswer(kind="fallback", short_answer=f"answer after {orchestrator.get('status', 'no result')}")


def api(runner):
    runs = InMemoryRunStore()
    dispatcher = LocalDispatcher(runs, runner=runner, explain_fn=fake_explain, records_fn=lambda run_id: [])
    return ApiService(intake_backend=MockBackend(), runs=runs, dispatcher=dispatcher), runs


def test_a_run_goes_queued_running_done_and_stores_the_result():
    release, started = threading.Event(), threading.Event()

    def slow(inp):
        started.set()
        release.wait(5)
        return done_output(inp)
    svc, runs = api(slow)
    status, out = svc.post_message("robert", MessageIn(text="Is my portfolio too risky?", profile_form=FORM))
    assert status == 202 and out.kind == "run"
    assert started.wait(5)
    assert runs.get(out.run_id).status == "RUNNING"
    release.set()
    run = wait_for(runs, out.run_id, "DONE")
    assert run.result["summary"] == "the answer" and run.error is None
    assert run.result["answer"]["short_answer"] == "answer after DONE"  # the Explainer ran after the orchestrator
    assert run.note == LocalDispatcher.note and run.updated_at >= run.created_at


def test_a_failing_orchestrator_output_marks_the_run_failed():
    svc, runs = api(lambda inp: OrchestratorOutput(run_id=inp.run_id, status="FAILED", error="ValidationException: bad"))
    _, out = svc.post_message("robert", MessageIn(text="Is my portfolio too risky?", profile_form=FORM))
    run = wait_for(runs, out.run_id, "FAILED")
    assert run.error == "ValidationException: bad" and run.result["status"] == "FAILED"


def test_a_crashing_runner_is_caught_and_the_run_is_failed():
    def crash(inp):
        raise RuntimeError("boom")
    svc, runs = api(crash)
    _, out = svc.post_message("robert", MessageIn(text="Is my portfolio too risky?", profile_form=FORM))
    run = wait_for(runs, out.run_id, "FAILED")
    assert run.error == "RuntimeError: boom" and run.result["answer"]["short_answer"] == "answer after FAILED"


def test_the_payload_carries_the_stored_snapshot():
    seen = []
    svc, runs = api(lambda inp: seen.append(inp) or done_output(inp))
    _, out = svc.post_message("robert", MessageIn(text="Is my portfolio too risky?", profile_form=FORM))
    wait_for(runs, out.run_id, "DONE")
    inp = seen[0]
    assert inp.run_id == out.run_id and inp.actor_id == "robert" and inp.profile.risk_band == "low_moderate"
    assert inp.execution_plan.enabled_names == ["risk"] and inp.prompt_ref.key == "explainer.advanced.md"
    assert payload_from_run(runs.get(out.run_id)).run_id == out.run_id


def test_service_from_env_picks_the_dispatcher():
    from app.api.dispatch import InlineDispatcher
    assert isinstance(service_from_env({}).dispatcher, InlineDispatcher)             # queue-free default
    assert isinstance(service_from_env({"AF_DISPATCH": "none"}).dispatcher, NoDispatcher)
    with pytest.raises(ValueError):
        service_from_env({"AF_DISPATCH": "carrier-pigeon"})
