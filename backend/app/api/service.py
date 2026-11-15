"""Box ② core: what POST /v1/runs, GET /v1/runs/{id} and /v1/profile do, without any web framework.

POST /v1/runs (links L2 → L6), synchronous part (a few seconds, well under API Gateway's 30 s):
  1. ③ Intake with the stored profile (the profile is saved, toggles said in chat included);
  2. Intake "needs_clarification"           → 200 {"kind": "question"}  (no run)
  3. ④ Context on "ready" / "out_of_scope":
       "profile_required"                    → 200 {"kind": "message"}   (no run)
       "planned"                             → run saved as QUEUED, handed to the dispatcher → 202 {"kind": "run"}
The analysis itself (box ⑤) runs asynchronously; the frontend polls GET /v1/runs/{id} (L19).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.context import handler as context_handler
from app.intake import IntakeError, get_backend, run_intake
from app.intake.backends import IntakeBackend
from app.intake.schemas import AgentToggleChange
from app.schemas import PortfolioRef, Profile
from app.schemas.profile import AgentToggles
from app.store.profiles import InMemoryProfileStore, ProfileStore
from app.store.runs import InMemoryRunStore, RunRecord, RunStore, new_run_id

MAX_TEXT_CHARS = 4000


class Dispatcher(Protocol):
    """Hands a QUEUED run to box ⑤ (L6). The AgentCore dispatcher comes when box ⑤ is deployed."""

    def dispatch(self, run: RunRecord) -> None: ...


class NoDispatcher:
    """Box ⑤ not connected yet: the run stays QUEUED, with its plan, ready to be picked up."""

    note = "Queued: the analysis agents (box 5) are not connected to the API yet."

    def dispatch(self, run: RunRecord) -> None:
        return None


# ---------- HTTP-facing models ----------

class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    profile_form: Optional[dict[str, Any]] = None
    portfolio_ref: Optional[PortfolioRef] = None


class MessageOut(BaseModel):
    """One shape for every answer to POST /v1/runs; `kind` tells the frontend what to show."""

    kind: Literal["question", "message", "run"]
    intake_status: Optional[str] = None
    question: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    message: Optional[str] = None
    run_id: Optional[str] = None
    run_status: Optional[str] = None
    enabled_agents: list[str] = Field(default_factory=list)
    toggle_changes: list[AgentToggleChange] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


@dataclass
class ApiService:
    profiles: ProfileStore = field(default_factory=InMemoryProfileStore)
    runs: RunStore = field(default_factory=InMemoryRunStore)
    dispatcher: Dispatcher = field(default_factory=NoDispatcher)
    intake_backend: Optional[IntakeBackend] = None

    def _backend(self) -> IntakeBackend:
        self.intake_backend = self.intake_backend or get_backend()
        return self.intake_backend

    # ----- POST /v1/runs -----
    def post_message(self, actor_id: str, body: MessageIn) -> tuple[int, MessageOut]:
        try:
            res = run_intake(body.text, actor_id, profile_form=body.profile_form,
                             portfolio_ref=body.portfolio_ref.model_dump() if body.portfolio_ref else None,
                             backend=self._backend(), store=self.profiles)
        except IntakeError as e:
            raise ApiError(502, f"Intake failed: {e}") from e
        common = dict(intake_status=res.status, toggle_changes=res.toggle_changes, warnings=res.warnings)

        if res.status == "needs_clarification":
            return 200, MessageOut(kind="question", question=res.clarification_question,
                                   missing_fields=res.missing_fields, **common)
        if res.request is None:  # input refused (e.g. guardrail): nothing to plan
            return 200, MessageOut(kind="message", message="This request cannot be processed. AlphaForge is a "
                                   "simulation tool for investment analysis.", **common)

        ctx = context_handler({"profile": res.profile.model_dump(mode="json") if res.profile else None,
                               "request": res.request.model_dump(mode="json")})
        if ctx["status"] == "profile_required":
            return 200, MessageOut(kind="message", message=ctx["message"], **common)

        run = RunRecord(run_id=new_run_id(), actor_id=actor_id, profile=res.profile, request=res.request,
                        execution_plan=ctx["execution_plan"], prompt_ref=ctx["prompt_ref"],
                        note=getattr(self.dispatcher, "note", None))
        self.runs.put(run)
        self.dispatcher.dispatch(run)
        return 202, MessageOut(kind="run", run_id=run.run_id, run_status=run.status,
                               enabled_agents=run.execution_plan.enabled_names, **common)

    # ----- GET /v1/runs/{run_id} -----
    def get_run(self, actor_id: str, run_id: str) -> RunRecord:
        run = self.runs.get(run_id)
        if run is None or run.actor_id != actor_id:  # never reveal another user's run
            raise ApiError(404, "Run not found.")
        return run

    # ----- GET / PUT /v1/profile -----
    def get_profile(self, actor_id: str) -> Profile:
        profile = self.profiles.get(actor_id)
        if profile is None:
            raise ApiError(404, "No profile yet: fill in the profile form.")
        return profile

    def put_profile(self, actor_id: str, form: dict[str, Any]) -> Profile:
        """Profile form: fields given replace the stored ones; agent_toggles apply per agent and clear the
        chat quote for those agents. actor_id always comes from the login, never from the body."""
        stored = self.profiles.get(actor_id)
        data = stored.model_dump(mode="json", exclude={"actor_id"}) if stored else {}
        form = {k: v for k, v in form.items() if k not in ("actor_id", "agent_toggle_reasons")}
        if isinstance(form.get("agent_toggles"), dict):
            form_toggles = form.pop("agent_toggles")
            toggles = {**data.get("agent_toggles", AgentToggles().model_dump()), **form_toggles}
            # Same rule as the Intake: an agent set in the form keeps no chat quote.
            reasons = {a: q for a, q in data.get("agent_toggle_reasons", {}).items() if a not in form_toggles}
            data.update(agent_toggles=toggles, agent_toggle_reasons=reasons)
        data.update({k: v for k, v in form.items() if v is not None})
        try:
            profile = Profile(actor_id=actor_id, **data)
        except (ValidationError, TypeError) as e:
            detail = e.errors(include_url=False) if isinstance(e, ValidationError) else str(e)
            raise ApiError(422, detail) from e
        self.profiles.put(profile)
        return profile


def service_from_env(env: Optional[dict[str, str]] = None) -> ApiService:
    """AF_STORE=memory (default, local) | dynamodb (AF_TABLE, default "alphaforge")."""
    env = os.environ if env is None else env
    kind = env.get("AF_STORE", "memory").lower()
    if kind == "memory":
        return ApiService()
    if kind == "dynamodb":
        import boto3

        from app.store.profiles import DynamoDBProfileStore
        from app.store.runs import DynamoDBRunStore

        table = boto3.resource("dynamodb").Table(env.get("AF_TABLE", "alphaforge"))
        return ApiService(profiles=DynamoDBProfileStore(table), runs=DynamoDBRunStore(table))
    raise ValueError(f"Unknown AF_STORE: {kind!r} (expected: memory or dynamodb)")
