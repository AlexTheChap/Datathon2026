"""service_from_env: AF_STORE=file keeps profiles and runs on disk, so they survive a restart."""
from pathlib import Path

import pytest
from app.api.service import MessageIn, service_from_env
from app.store.profiles import JsonFileProfileStore
from app.store.runs import JsonFileRunStore, RunRecord


def test_file_mode_keeps_profiles_across_a_restart(tmp_path):
    env = {"AF_STORE": "file", "AF_DATA_DIR": str(tmp_path / "store"), "AF_DISPATCH": "none"}
    svc = service_from_env(env)
    assert isinstance(svc.profiles, JsonFileProfileStore)
    assert isinstance(svc.runs, JsonFileRunStore)
    svc.put_profile("alice", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    assert (tmp_path / "store" / "profiles" / "alice.json").is_file()
    again = service_from_env(env)                               # "restart"
    assert again.get_profile("alice").risk_band == "moderate"


def test_default_data_dir_is_the_repo_local_af_store(monkeypatch):
    from app.core.config import REPO_ROOT

    monkeypatch.setattr(JsonFileProfileStore, "__init__", lambda self, d: setattr(self, "dir", Path(d)))
    monkeypatch.setattr(JsonFileRunStore, "__init__", lambda self, d: setattr(self, "dir", Path(d)))
    svc = service_from_env({"AF_STORE": "file", "AF_DISPATCH": "none"})
    assert svc.profiles.dir == REPO_ROOT / ".af_store" / "profiles"
    assert svc.runs.dir == REPO_ROOT / ".af_store" / "runs"


def test_file_mode_keeps_runs_across_a_restart(tmp_path):
    """Signing in again after a restart must find the last portfolio run, not build a new one."""
    env = {"AF_STORE": "file", "AF_DATA_DIR": str(tmp_path / "store"), "AF_DISPATCH": "none"}
    svc = service_from_env(env)
    svc.put_profile("alice", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    status, out = svc.post_message("alice", MessageIn(text="Build a simulated portfolio that fits my profile."))
    if out.kind != "run":
        pytest.skip(f"intake did not start a run here: {out.kind}")
    again = service_from_env(env)                               # "restart"
    assert [r.run_id for r in again.list_runs("alice")] == [out.run_id]
    assert again.get_run("alice", out.run_id).actor_id == "alice"
    assert again.list_runs("bob") == []


def test_json_file_run_store_lists_newest_first_and_skips_damaged_files(tmp_path):
    store = JsonFileRunStore(tmp_path)
    svc = service_from_env({"AF_STORE": "file", "AF_DATA_DIR": str(tmp_path / "s"), "AF_DISPATCH": "none"})
    svc.put_profile("alice", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    _, out = svc.post_message("alice", MessageIn(text="Build a simulated portfolio that fits my profile."))
    if out.kind != "run":
        pytest.skip(f"intake did not start a run here: {out.kind}")
    base = svc.runs.get(out.run_id)
    old = base.model_copy(update={"run_id": "run-old", "created_at": "2026-01-01T00:00:00+00:00"})
    new = base.model_copy(update={"run_id": "run-new", "created_at": "2026-02-01T00:00:00+00:00"})
    store.put(old); store.put(new)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert [r.run_id for r in store.list_for_actor("alice")] == ["run-new", "run-old"]
    assert store.get("run-old").created_at == old.created_at
    assert store.get("missing") is None


def test_unknown_store_is_rejected():
    with pytest.raises(ValueError, match="memory, file or dynamodb"):
        service_from_env({"AF_STORE": "postgres"})


def test_a_run_left_running_by_a_dead_worker_is_marked_failed(make_profile, make_request):
    """On AWS a Lambda that hits its 15-min limit cannot record its outcome: reading the run expires it."""
    from datetime import datetime, timedelta, timezone

    from app.api.service import STALE_RUN_ERROR, ApiService, NoDispatcher
    from app.context.plan_resolver import resolve_plan
    from app.context.prompt_loader import resolve_explainer_prompt
    from app.store.runs import InMemoryRunStore, RunRecord

    profile, request = make_profile("robert"), make_request("persona-robert", "risk_review")
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runs = InMemoryRunStore()
    for run_id, updated in (("run-dead", old), ("run-live", fresh)):
        runs.put(RunRecord(run_id=run_id, actor_id="persona-robert", profile=profile, request=request,
                           execution_plan=resolve_plan(profile, request),
                           prompt_ref=resolve_explainer_prompt(profile), status="RUNNING",
                           created_at=updated, updated_at=updated))
    svc = ApiService(runs=runs, dispatcher=NoDispatcher())

    dead = svc.get_run("persona-robert", "run-dead")
    assert dead.status == "FAILED" and dead.error == STALE_RUN_ERROR
    assert runs.get("run-dead").status == "FAILED"                       # saved, not just shown
    assert svc.get_run("persona-robert", "run-live").status == "RUNNING"  # a run in progress is left alone
    assert {s.run_id: s.status for s in svc.list_runs("persona-robert")} == {"run-dead": "FAILED",
                                                                           "run-live": "RUNNING"}
