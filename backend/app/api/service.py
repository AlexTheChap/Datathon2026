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

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.context import handler as context_handler
from app.intake import IntakeError, get_backend, run_intake
from app.intake.backends import IntakeBackend
from app.intake.schemas import AgentToggleChange
from app.schemas import PortfolioRef, Profile
from app.schemas.models import profile_from_persona
from app.schemas.profile import AgentToggles
from app.store.profiles import InMemoryProfileStore, ProfileStore
from app.store.runs import InMemoryRunStore, RunRecord, RunStore, new_run_id, new_session_id

MAX_TEXT_CHARS = 4000
PERSONAS_PATH = Path(__file__).resolve().parents[1] / "core" / "personas.json"


class Dispatcher(Protocol):
    """Hands a QUEUED run to box ⑤ (L6). The AgentCore dispatcher comes when box ⑤ is deployed."""

    def dispatch(self, run: RunRecord) -> None: ...


class NoDispatcher:
    """Box ⑤ not connected yet: the run stays QUEUED, with its plan, ready to be picked up."""

    note = "Waiting: the analysis agents are not connected to the app yet, so this run stops at its plan."

    def dispatch(self, run: RunRecord) -> None:
        return None


# ---------- HTTP-facing models ----------

class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    profile_form: Optional[dict[str, Any]] = None
    portfolio_ref: Optional[PortfolioRef] = None
    # Conversation id returned by the previous answer: keeps the session (short-term memory) across turns.
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


class RunSummary(BaseModel):
    """One line of GET /v1/runs."""

    run_id: str
    status: str
    task: str
    created_at: str
    updated_at: str
    question: str
    enabled_agents: list[str]
    n_evidence: int = 0

    @classmethod
    def from_run(cls, run: RunRecord) -> "RunSummary":
        return cls(run_id=run.run_id, status=run.status, task=run.request.task, created_at=run.created_at,
                   updated_at=run.updated_at, question=run.request.user_message[:200],
                   enabled_agents=run.execution_plan.enabled_names,
                   n_evidence=len((run.result or {}).get("evidence_ids") or []))


class MessageOut(BaseModel):
    """One shape for every answer to POST /v1/runs; `kind` tells the frontend what to show."""

    kind: Literal["question", "message", "run"]
    intake_status: Optional[str] = None
    question: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    message: Optional[str] = None
    run_id: Optional[str] = None
    session_id: Optional[str] = None
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
    ledger: Optional[Any] = None   # EvidenceLedger; None -> chosen from the settings (JSONL or DynamoDB)

    def _ledger(self):
        if self.ledger is None:
            from app.core.config import get_settings
            from app.store.ledger import make_ledger

            self.ledger = make_ledger(get_settings())
        return self.ledger

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

        session_id = body.session_id or new_session_id()
        run = RunRecord(run_id=new_run_id(), actor_id=actor_id, session_id=session_id,
                        profile=res.profile, request=res.request,
                        execution_plan=ctx["execution_plan"], prompt_ref=ctx["prompt_ref"],
                        note=getattr(self.dispatcher, "note", None))
        self.runs.put(run)
        self.dispatcher.dispatch(run)
        return 202, MessageOut(kind="run", run_id=run.run_id, session_id=session_id, run_status=run.status,
                               enabled_agents=run.execution_plan.enabled_names, **common)

    # ----- Demo accounts -----
    def ensure_demo_profile(self, actor_id: str, persona: Optional[str]) -> None:
        """A demo account (amira/mateo/robert) starts with its persona's profile from core/personas.json.
        Only when it has none yet: changes the user makes afterwards are kept."""
        if not persona or self.profiles.get(actor_id) is not None:
            return
        form = next((p["profile_form"] for p in example_personas() if p["id"] == persona), None)
        if form is not None:
            self.profiles.put(Profile(actor_id=actor_id, **form))

    # ----- GET /v1/runs/{run_id} -----
    def get_run(self, actor_id: str, run_id: str) -> RunRecord:
        run = self.runs.get(run_id)
        if run is None or run.actor_id != actor_id:  # never reveal another user's run
            raise ApiError(404, "Run not found.")
        return run

    # ----- GET /v1/runs (history for the dashboard) -----
    def list_runs(self, actor_id: str, limit: int = 50) -> list[RunSummary]:
        return [RunSummary.from_run(r) for r in self.runs.list_for_actor(actor_id, limit)]

    # ----- GET /v1/runs/{run_id}/replay (dashboard "if 2022 happened again" chart) -----
    def get_replay(self, actor_id: str, run_id: str) -> dict[str, Any]:
        run = self.get_run(actor_id, run_id)
        from app.api.replay import replay
        from app.tools.quant_gateway import tools

        capital = run.request.capital or run.profile.capital
        return replay(self._ledger().records(run_id), capital, tools(),
                      start="2022-01-03", end="2022-12-30")

    # ----- GET /v1/runs/{run_id}/evidence (ledger L15/L16: what the dashboard charts) -----
    def get_evidence(self, actor_id: str, run_id: str) -> list[dict[str, Any]]:
        self.get_run(actor_id, run_id)  # same ownership check: 404 for someone else's run
        return self._ledger().records(run_id)

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


def example_personas(path: Path = PERSONAS_PATH) -> list[dict[str, Any]]:
    """The brief's three personas as ready-to-save profile forms (the frontend's "start from an example").
    Fictional profiles; the form never carries an actor_id (the login decides who saves it)."""
    out = []
    for p in json.loads(path.read_text(encoding="utf-8"))["personas"]:
        form = profile_from_persona(p).model_dump(mode="json", exclude={"actor_id", "schema_version",
                                                                         "agent_toggle_reasons"})
        out.append({
            "id": p["id"],
            "name": p["id"].capitalize(),
            "summary": f"{p['profile_type']} investor, {p['age']}, {p['occupation'].lower()}.",
            "tests": p.get("test_case"),
            "example_question": (p.get("demo_request") or {}).get("text"),
            "profile_form": form,
        })
    return out


def _dispatcher(runs: RunStore, dispatch: str) -> Dispatcher:
    if dispatch == "local":
        from .dispatcher import LocalDispatcher

        return LocalDispatcher(runs)
    if dispatch == "inline":
        from .dispatch import InlineDispatcher

        return InlineDispatcher(runs)
    return NoDispatcher()


def service_from_env(env: Optional[dict[str, str]] = None) -> ApiService:
    """AF_STORE=memory (default, local) | dynamodb (AF_TABLE, default "alphaforge").
    AF_DISPATCH=local (default with the memory store: the orchestrator runs in a background thread) | none
    (default with DynamoDB: the run stays QUEUED until the AgentCore dispatcher is deployed) | inline (the
    orchestrator runs before POST /v1/runs returns: app.api.dispatch.InlineDispatcher, used by the Streamlit
    dashboard so the run is DONE when the page shows it)."""
    env = os.environ if env is None else env
    kind = env.get("AF_STORE", "memory").lower()
    dispatch = env.get("AF_DISPATCH", "local" if kind == "memory" else "none").lower()
    if dispatch not in ("local", "none", "inline"):
        raise ValueError(f"Unknown AF_DISPATCH: {dispatch!r} (expected: local, none or inline)")
    if kind == "memory":
        runs = InMemoryRunStore()
        return ApiService(runs=runs, dispatcher=_dispatcher(runs, dispatch))
    if kind == "dynamodb":
        import boto3

        from app.store.profiles import DynamoDBProfileStore
        from app.store.runs import DynamoDBRunStore

        table = boto3.resource("dynamodb", region_name=env.get("AWS_REGION", "us-east-1")).Table(
            env.get("AF_TABLE", "alphaforge"))
        runs = DynamoDBRunStore(table)
        return ApiService(profiles=DynamoDBProfileStore(table), runs=runs, dispatcher=_dispatcher(runs, dispatch))
    raise ValueError(f"Unknown AF_STORE: {kind!r} (expected: memory or dynamodb)")
