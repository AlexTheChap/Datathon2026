"""Profile storage (link L3: "the profile is stored in DynamoDB (ACTOR#… / PROFILE#…)").

Local DynamoDB stand-in: in memory (tests) or one JSON file per actor. The key follows the planned
DynamoDB format, so moving to boto3 only changes this module.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Protocol

from app.schemas import Profile


def profile_key(actor_id: str) -> tuple[str, str]:
    return f"ACTOR#{actor_id}", "PROFILE#current"


class ProfileStore(Protocol):
    def get(self, actor_id: str) -> Optional[Profile]: ...

    def put(self, profile: Profile) -> None: ...


class InMemoryProfileStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], str] = {}

    def get(self, actor_id: str) -> Optional[Profile]:
        raw = self._items.get(profile_key(actor_id))
        return Profile.model_validate_json(raw) if raw else None

    def put(self, profile: Profile) -> None:
        self._items[profile_key(profile.actor_id)] = profile.model_dump_json()


class JsonFileProfileStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, actor_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in actor_id)
        return self.dir / f"{safe}.json"

    def get(self, actor_id: str) -> Optional[Profile]:
        p = self._path(actor_id)
        if not p.exists():
            return None
        item = json.loads(p.read_text(encoding="utf-8"))
        item.pop("PK", None)
        item.pop("SK", None)
        return Profile.model_validate(item)

    def put(self, profile: Profile) -> None:
        pk, sk = profile_key(profile.actor_id)
        item = {"PK": pk, "SK": sk, **json.loads(profile.model_dump_json())}
        self._path(profile.actor_id).write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


class DynamoDBProfileStore:
    """DynamoDB version (box ②): same keys as above, profile kept as one JSON string attribute (`data`) so
    floats and nested objects need no DynamoDB type conversion. `table` is a boto3 Table resource."""

    def __init__(self, table) -> None:
        self.table = table

    def get(self, actor_id: str) -> Optional[Profile]:
        pk, sk = profile_key(actor_id)
        item = self.table.get_item(Key={"PK": pk, "SK": sk}).get("Item")
        return Profile.model_validate_json(item["data"]) if item else None

    def put(self, profile: Profile) -> None:
        pk, sk = profile_key(profile.actor_id)
        self.table.put_item(Item={"PK": pk, "SK": sk, "data": profile.model_dump_json()})
