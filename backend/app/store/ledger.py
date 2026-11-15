"""Local evidence ledger (L15) — one JSONL file per run.

Every tool call = one line (seq E-001, E-002…). The stable `evidence_id`
produced by risk_tools lets a single piece of evidence be cited even when
called several times. DynamoDB equivalent (RUN#id / EVID#…) planned later:
same interface.
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
