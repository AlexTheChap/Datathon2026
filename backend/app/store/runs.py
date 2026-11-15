"""Run storage (box ②, links L6 and L19): one record per analysis, read back by GET /v1/runs/{id}.

DynamoDB single-table layout shared with the profiles: PK "RUN#<run_id>", SK "META". The whole record is
stored as one JSON string attribute (`data`), so floats and nested objects need no DynamoDB conversion.
In-memory stand-in for tests and local runs.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import Profile, Request
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef

RunStatus = Literal["QUEUED", "RUNNING", "DRAFTED", "DONE", "FAILED"]
# DRAFTED: the orchestrator's working draft is written back and visible while the Explainer writes the
# checked answer over it (perceived latency: the reader reads the draft ~15-20s sooner). Never final.


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

    def list_for_actor(self, actor_id: str, limit: int = 50) -> list[RunRecord]: ...

    def delete_for_actor(self, actor_id: str) -> list[str]: ...


def actor_run_key(actor_id: str, run: RunRecord) -> dict[str, str]:
    """Pointer item for GET /v1/runs: PK "ACTOR#<id>", SK "RUN#<created_at>#<run_id>" (newest last)."""
    return {"PK": f"ACTOR#{actor_id}", "SK": f"RUN#{run.created_at}#{run.run_id}"}


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

    def list_for_actor(self, actor_id: str, limit: int = 50) -> list[RunRecord]:
        runs = [RunRecord.model_validate_json(r) for r in self._items.values()]
        runs = sorted((r for r in runs if r.actor_id == actor_id), key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    def delete_for_actor(self, actor_id: str) -> list[str]:
        """Every run of the actor; returns their ids (their evidence goes too, see ApiService.delete_account)."""
        with self._lock:
            ids = [rid for rid, raw in self._items.items() if RunRecord.model_validate_json(raw).actor_id == actor_id]
            for rid in ids:
                del self._items[rid]
        return ids


class JsonFileRunStore:
    """AF_STORE=file: one JSON file per run (<dir>/<run_id>.json), so the run history (and the portfolio the
    dashboard reads from it) survives a restart, like JsonFileProfileStore does for profiles."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()          # a background run writes while the API reads

    def _path(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run_id)
        return self.dir / f"{safe}.json"

    def put(self, run: RunRecord) -> None:
        path = self._path(run.run_id)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:                        # write then rename: a reader never sees half a file
            tmp.write_text(run.model_dump_json(), encoding="utf-8")
            os.replace(tmp, path)

    def get(self, run_id: str) -> Optional[RunRecord]:
        path = self._path(run_id)
        with self._lock:
            raw = path.read_text(encoding="utf-8") if path.exists() else None
        return RunRecord.model_validate_json(raw) if raw else None

    def list_for_actor(self, actor_id: str, limit: int = 50) -> list[RunRecord]:
        runs: list[RunRecord] = []
        with self._lock:
            raws = [p.read_text(encoding="utf-8") for p in self.dir.glob("*.json")]
        for raw in raws:
            try:
                run = RunRecord.model_validate_json(raw)
            except ValueError:                  # an old or damaged file must not hide the rest of the history
                continue
            if run.actor_id == actor_id:
                runs.append(run)
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]


class DynamoDBRunStore:
    """`table` is a boto3 DynamoDB Table resource (injectable for tests)."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def put(self, run: RunRecord) -> None:
        self.table.put_item(Item={**run_key(run.run_id), "actor_id": run.actor_id, "status": run.status,
                                  "updated_at": run.updated_at, "data": run.model_dump_json()})
        # pointer for GET /v1/runs (one Query on the actor, no table scan)
        self.table.put_item(Item={**actor_run_key(run.actor_id, run), "run_id": run.run_id, "status": run.status,
                                  "task": run.request.task, "updated_at": run.updated_at})

    def get(self, run_id: str) -> Optional[RunRecord]:
        item = self.table.get_item(Key=run_key(run_id)).get("Item")
        return RunRecord.model_validate(json.loads(item["data"])) if item else None

    def list_for_actor(self, actor_id: str, limit: int = 50) -> list[RunRecord]:
        page = self.table.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                ExpressionAttributeValues={":pk": f"ACTOR#{actor_id}", ":sk": "RUN#"},
                                ScanIndexForward=False, Limit=limit)
        runs = (self.get(i["run_id"]) for i in page.get("Items", []))
        return [r for r in runs if r is not None and r.actor_id == actor_id]

    def delete_for_actor(self, actor_id: str) -> list[str]:
        """Every run of the actor (record + pointer); returns their ids."""
        ids: list[str] = []
        kwargs = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                  "ExpressionAttributeValues": {":pk": f"ACTOR#{actor_id}", ":sk": "RUN#"}}
        while True:
            page = self.table.query(**kwargs)
            for i in page.get("Items", []):
                run = self.get(i["run_id"])
                if run is not None and run.actor_id == actor_id:
                    self.table.delete_item(Key=run_key(run.run_id))
                    ids.append(run.run_id)
                self.table.delete_item(Key={"PK": i["PK"], "SK": i["SK"]})
            if "LastEvaluatedKey" not in page:
                return ids
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
