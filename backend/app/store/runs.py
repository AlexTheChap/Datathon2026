"""Run storage (box ②, links L6 and L19): one record per analysis, read back by GET /v1/runs/{id}.

DynamoDB single-table layout shared with the profiles: PK "RUN#<run_id>", SK "META". The whole record is
stored as one JSON string attribute (`data`), so floats and nested objects need no DynamoDB conversion.
In-memory stand-in for tests and local runs.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import Profile, Request
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef

RunStatus = Literal["QUEUED", "RUNNING", "DONE", "FAILED"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:16]}"


class RunRecord(BaseModel):
    """Everything box ⑤ needs to run the analysis (L6), plus its outcome once written back (L18)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    run_id: str
    actor_id: str
    session_id: Optional[str] = None    # conversation (AgentCore Memory short term): the client sends it back on the next turn
    status: RunStatus = "QUEUED"
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    profile: Profile                   # snapshot at submission time (the stored profile may change later)
    request: Request
    execution_plan: ExecutionPlan      # box ④
    prompt_ref: PromptRef              # box ④, for the Explainer
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    note: Optional[str] = None


def run_key(run_id: str) -> dict[str, str]:
    return {"PK": f"RUN#{run_id}", "SK": "META"}


class RunStore(Protocol):
    def put(self, run: RunRecord) -> None: ...

    def get(self, run_id: str) -> Optional[RunRecord]: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._items: dict[str, str] = {}
        self._lock = threading.Lock()          # a background run writes while the API reads

    def put(self, run: RunRecord) -> None:
        with self._lock:
            self._items[run.run_id] = run.model_dump_json()

    def get(self, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            raw = self._items.get(run_id)
        return RunRecord.model_validate_json(raw) if raw else None


class DynamoDBRunStore:
    """`table` is a boto3 DynamoDB Table resource (injectable for tests)."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def put(self, run: RunRecord) -> None:
        self.table.put_item(Item={**run_key(run.run_id), "actor_id": run.actor_id, "status": run.status,
                                  "updated_at": run.updated_at, "data": run.model_dump_json()})

    def get(self, run_id: str) -> Optional[RunRecord]:
        item = self.table.get_item(Key=run_key(run_id)).get("Item")
        return RunRecord.model_validate(json.loads(item["data"])) if item else None
