"""Inline dispatcher (link L6 → box ⑤, result written back L18): runs the orchestrator for a QUEUED run.

The architecture's target is an asynchronous "dispatcher" Lambda invoking AgentCore Runtime. Until that is
deployed, InlineDispatcher runs the same orchestrator in-process and keeps the run record up to date in the
RunStore (DynamoDB when AF_STORE=dynamodb):

    QUEUED (API, L6) → RUNNING → DONE / FAILED + result (summary, evidence_ids, duration) (L18)
                                         + result["answer"]: the Explainer's checked answer (L7, L16, L18)

The evidence of every tool call goes to the ledger chosen by the settings (DynamoDB: RUN#id / EVID#…, L15),
so GET /v1/runs/{id} and the evidence ledger describe the same run_id.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from app.schemas.models import OrchestratorInput
from app.store.runs import RunRecord, RunStore, _now


def orchestrator_input(run: RunRecord, portfolio_weights: Optional[dict[str, float]] = None) -> OrchestratorInput:
    """RunRecord (profile + request + plan snapshot) → the payload box ⑤ expects."""
    req = run.request
    weights = portfolio_weights
    if weights is None:
        named = {p.ticker: p.weight for p in req.positions if p.weight is not None}
        weights = named or None
    return OrchestratorInput(
        run_id=run.run_id, actor_id=run.actor_id, language=run.profile.language,
        as_of=req.as_of.isoformat() if req.as_of else None,
        profile=run.profile, request=req, execution_plan=run.execution_plan, portfolio_weights=weights,
    )


class InlineDispatcher:
    note = None

    def __init__(self, runs: RunStore, portfolio_weights: Optional[dict[str, float]] = None,
                 run_fn: Optional[Callable[[OrchestratorInput], Any]] = None,
                 explain_fn: Optional[Callable[..., Any]] = None,
                 records_fn: Optional[Callable[[str], list[dict]]] = None):
        self.runs = runs
        self.portfolio_weights = portfolio_weights
        self._run_fn = run_fn  # injectable for tests (no Bedrock)
        self._explain_fn = explain_fn
        self._records_fn = records_fn

    def dispatch(self, run: RunRecord) -> None:
        run.status, run.updated_at = "RUNNING", _now()
        self.runs.put(run)
        try:
            if self._run_fn is None:
                from app.agents.orchestrator import run_orchestrator

                self._run_fn = run_orchestrator
            out = self._run_fn(orchestrator_input(run, self.portfolio_weights))
            run.status = out.status
            run.result = out.model_dump(mode="json")
            run.error = out.error
        except Exception as e:  # visible failure, never a run stuck in RUNNING
            run.status, run.error = "FAILED", f"{type(e).__name__}: {e}"
        # The Explainer always answers: checked explanation, polite decline, or evidence-only fallback.
        try:
            answer = self._explain(run)
            run.result = {**(run.result or {}), "answer": answer.model_dump(mode="json")}
        except Exception as e:  # never lose the run because the explanation failed
            run.result = {**(run.result or {}), "answer": None, "answer_error": f"{type(e).__name__}: {e}"}
        run.updated_at = _now()
        self.runs.put(run)

    def _explain(self, run: RunRecord) -> Any:
        if self._explain_fn is None:
            from app.agents.explainer import explain

            self._explain_fn = explain
        if self._records_fn is None:
            from app.core.config import get_settings
            from app.store.ledger import make_ledger

            self._records_fn = make_ledger(get_settings()).records
        orchestrator = dict(run.result or {})
        if run.status == "FAILED":
            orchestrator.update(status="FAILED", error=run.error)
        return self._explain_fn(run, orchestrator, self._records_fn(run.run_id))


class BackgroundDispatcher:
    """Runs another dispatcher in a thread: POST /v1/runs answers 202 at once, the UI polls GET /v1/runs/{id}.
    Local stand-in for the asynchronous dispatcher Lambda of L6."""

    note = None

    def __init__(self, inner: Any):
        self.inner = inner

    def dispatch(self, run: RunRecord) -> None:
        threading.Thread(target=self.inner.dispatch, args=(run,), name=f"run-{run.run_id}", daemon=True).start()
