"""Local evidence ledger (L15) — one JSONL file per run.

Every tool call = one line (seq E-001, E-002…). The stable `evidence_id`
produced by risk_tools lets a single piece of evidence be cited even when
called several times. DynamoDBLedger (below) is the AWS version (RUN#id / EVID#…),
same interface; make_ledger() picks one from the settings (AF_STORE).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

# Beyond this, the payload is truncated (e.g. replay: one value per day) with a
# marker kept: the full evidence stays recomputable via its evidence_id.
_MAX_CHARS = 200_000


class EvidenceLedger(Protocol):
    def append(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
    def records(self, run_id: str) -> list[dict[str, Any]]: ...
    def evidence_ids(self, run_id: str) -> list[str]: ...


def _truncate(value: Any) -> Any:
    s = json.dumps(value, ensure_ascii=False, default=str)
    if len(s) <= _MAX_CHARS:
        return value
    return {"truncated": True, "preview": s[:2000],
            "note": "Full payload recomputable via evidence_id (deterministic functions)."}


class JsonlLedger:
    """Local ledger: data/runs/{run_id}.jsonl (created on first evidence)."""

    def __init__(self, runs_dir: str | Path):
        self.dir = Path(runs_dir)

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.jsonl"

    def append(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        existing = self.records(run_id)
        entry = {
            "seq": f"E-{len(existing) + 1:03d}",
            "run_id": run_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **record,
        }
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def records(self, run_id: str) -> list[dict[str, Any]]:
        p = self._path(run_id)
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def evidence_ids(self, run_id: str) -> list[str]:
        """Unique ids, in order of first appearance."""
        seen: list[str] = []
        for r in self.records(run_id):
            eid = r.get("evidence_id")
            if eid and eid not in seen:
                seen.append(eid)
        return seen


class DynamoDBLedger:
    """Evidence ledger in DynamoDB (L15), same single table as profiles and runs.

    Keys:  PK "RUN#<run_id>"  SK "EVID#<seq>#<evidence_id>"   (one item per tool call, sorted by seq)
           PK "RUN#<run_id>"  SK "EVIDSEQ"                     (atomic counter: parallel sub-agents)
    The record is kept as one JSON string (`data`, like profiles/runs: no float conversion) plus a few
    plain attributes (tool, agent, evidence_id, say) so the items are readable in the AWS console.
    `table` is a boto3 DynamoDB Table resource (injectable for tests).
    """

    def __init__(self, table: Any):
        self.table = table

    def _next_seq(self, run_id: str) -> int:
        out = self.table.update_item(
            Key={"PK": f"RUN#{run_id}", "SK": "EVIDSEQ"}, UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW")
        return int(out["Attributes"]["n"])

    def append(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        seq = f"E-{self._next_seq(run_id):03d}"
        entry = {"seq": seq, "run_id": run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        data = json.dumps(_truncate(entry), ensure_ascii=False, default=str)
        item = {"PK": f"RUN#{run_id}", "SK": f"EVID#{seq}#{record.get('evidence_id') or '-'}",
                "seq": seq, "tool": str(record.get("tool", "")), "data": data}
        for key in ("agent", "evidence_id", "say", "error"):
            if record.get(key):
                item[key] = str(record[key])[:1000]
        self.table.put_item(Item=item)
        return entry

    def records(self, run_id: str) -> list[dict[str, Any]]:
        items, kwargs = [], {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":pk": f"RUN#{run_id}", ":sk": "EVID#"}}
        while True:
            page = self.table.query(**kwargs)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        items.sort(key=lambda i: i["seq"])
        return [json.loads(i["data"]) for i in items]

    def evidence_ids(self, run_id: str) -> list[str]:
        seen: list[str] = []
        for r in self.records(run_id):
            eid = r.get("evidence_id")
            if eid and eid not in seen:
                seen.append(eid)
        return seen


def make_ledger(settings: Any) -> EvidenceLedger:
    """AF_STORE=dynamodb -> DynamoDBLedger on AF_TABLE; otherwise the local JSONL ledger (data/runs)."""
    if str(getattr(settings, "store", "memory")).lower() == "dynamodb":
        import boto3

        table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.table)
        return DynamoDBLedger(table)
    return JsonlLedger(settings.abs_path(settings.runs_dir))
