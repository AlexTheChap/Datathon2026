"""Inline dispatcher (link L6 → box ⑤, result written back L18): runs the orchestrator for a QUEUED run.

The architecture's target is an asynchronous "dispatcher" Lambda invoking AgentCore Runtime. Until that is
deployed, InlineDispatcher runs the same orchestrator in-process and keeps the run record up to date in the
RunStore (DynamoDB when AF_STORE=dynamodb):

    QUEUED (API, L6) → RUNNING → DONE / FAILED + result (summary, evidence_ids, duration) (L18)

The evidence of every tool call goes to the ledger chosen by the settings (DynamoDB: RUN#id / EVID#…, L15),
so GET /v1/runs/{id} and the evidence ledger describe the same run_id.
"""
from __future__ import annotations

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
                 run_fn: Optional[Callable[[OrchestratorInput], Any]] = None):
        self.runs = runs
        self.portfolio_weights = portfolio_weights
        self._run_fn = run_fn  # injectable for tests (no Bedrock)

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
        run.updated_at = _now()
        self.runs.put(run)
