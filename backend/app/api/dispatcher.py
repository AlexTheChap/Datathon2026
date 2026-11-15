"""Dispatchers of box ② (link L6): hand a QUEUED run to box ⑤.

- `LocalDispatcher` (AF_DISPATCH=local, the default with the in-memory store): runs the orchestrator in a
  background thread of the same process and writes the outcome back to the run store (L18), so that
  `GET /v1/runs/{id}` goes QUEUED -> RUNNING -> DONE / FAILED. For local development and demos.
- `NoDispatcher` (api/service.py): the run stays QUEUED. Used with DynamoDB until the AgentCore dispatcher Lambda
  (InvokeAgentRuntime) is deployed: a thread would not survive the end of a Lambda invocation.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from app.schemas.models import OrchestratorInput, OrchestratorOutput
from app.store.runs import RunRecord, RunStore, _now

log = logging.getLogger(__name__)
Runner = Callable[[OrchestratorInput], OrchestratorOutput]


def payload_from_run(run: RunRecord) -> OrchestratorInput:
    """The AgentCore payload (L6) built from the stored run: profile snapshot, request, plan and prompt reference."""
    return OrchestratorInput(
        run_id=run.run_id, actor_id=run.actor_id, session_id=run.session_id, language=run.profile.language,
        as_of=str(run.request.as_of) if run.request.as_of else None,
        profile=run.profile, request=run.request, execution_plan=run.execution_plan, prompt_ref=run.prompt_ref)


class LocalDispatcher:
    note = "Running locally in a background thread (AF_DISPATCH=local)."

    def __init__(self, runs: RunStore, runner: Runner | None = None, max_workers: int = 2) -> None:
        self.runs = runs
        self._runner = runner
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="af-run")

    @property
    def runner(self) -> Runner:
        if self._runner is None:                        # imported late: strands/bedrock only when a run starts
            from app.agents.orchestrator import run_orchestrator
            self._runner = run_orchestrator
        return self._runner

    def dispatch(self, run: RunRecord) -> None:
        self._pool.submit(self._execute, run.run_id)

    def _save(self, run: RunRecord, **changes) -> RunRecord:
        run = run.model_copy(update={**changes, "updated_at": _now()})
        self.runs.put(run)
        return run

    def _execute(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        run = self._save(run, status="RUNNING")
        try:
            out = self.runner(payload_from_run(run))
        except Exception as e:  # the orchestrator already reports its own failures; this is the last safety net
            log.exception("run %s crashed", run_id)
            self._save(run, status="FAILED", error=f"{type(e).__name__}: {e}")
            return
        self._save(run, status=out.status, result=out.model_dump(mode="json"), error=out.error)
