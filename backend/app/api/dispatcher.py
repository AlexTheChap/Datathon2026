"""Dispatchers of box ② (link L6): hand a QUEUED run to box ⑤.

- `InlineDispatcher` (api/dispatch.py): runs the orchestrator, then the Explainer, synchronously in the request
  (scripts/smoke_dynamodb.py, the Streamlit dashboard).
- `LocalDispatcher` (AF_DISPATCH=local, the default with the in-memory store): runs the orchestrator, then the
  Explainer (dispatch.attach_answer), in a background thread of the same process and writes the outcome back to the run store (L18), so that
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

from .dispatch import attach_answer, orchestrator_input

log = logging.getLogger(__name__)
Runner = Callable[[OrchestratorInput], OrchestratorOutput]


def payload_from_run(run: RunRecord) -> OrchestratorInput:
    """The AgentCore payload (L6) built from the stored run: same function as the inline dispatcher
    (profile snapshot, request with its named positions as portfolio weights, plan, prompt reference, session)."""
    return orchestrator_input(run)


class LocalDispatcher:
    note = "Running locally in a background thread (AF_DISPATCH=local)."

    def __init__(self, runs: RunStore, runner: Runner | None = None, max_workers: int = 2,
                 explain_fn: Callable | None = None, records_fn: Callable[[str], list[dict]] | None = None) -> None:
        self.runs = runs
        self._runner = runner
        self._explain_fn, self._records_fn = explain_fn, records_fn  # injectable for tests (no Bedrock)
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
            changes = {"status": out.status, "result": out.model_dump(mode="json"), "error": out.error}
        except Exception as e:  # the orchestrator already reports its own failures; this is the last safety net
            log.exception("run %s crashed", run_id)
            changes = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
        run = run.model_copy(update=changes)
        self._save(attach_answer(run, self._explain_fn, self._records_fn))
