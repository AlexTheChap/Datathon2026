"""Registre de preuves local (L15) — un fichier JSONL par exécution.

Chaque appel d'outil = une ligne (seq E-001, E-002…). L'`evidence_id` stable
produit par risk_tools permet de citer une preuve même appelée plusieurs fois.
Équivalent DynamoDB (RUN#id / EVID#…) prévu plus tard : même interface.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

# Au-delà, on tronque la charge utile (ex. replay : une valeur par jour) en
# gardant un marqueur : la preuve complète reste recalculable via son evidence_id.
_MAX_CHARS = 200_000


class EvidenceLedger(Protocol):
    def append(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]: ...
    def records(self, run_id: str) -> list[dict[str, Any]]: ...
    def evidence_ids(self, run_id: str) -> list[str]: ...


def _truncate(value: Any) -> Any:
    s = json.dumps(value, ensure_ascii=False, default=str)
    if len(s) <= _MAX_CHARS:
        return value
    return {"tronque": True, "apercu": s[:2000],
            "note": "Charge utile complète recalculable via evidence_id (fonctions déterministes)."}


class JsonlLedger:
    """Registre local : data/runs/{run_id}.jsonl (créé à la première preuve)."""

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
        """Identifiants uniques, ordre de première apparition."""
        seen: list[str] = []
        for r in self.records(run_id):
            eid = r.get("evidence_id")
            if eid and eid not in seen:
                seen.append(eid)
        return seen
