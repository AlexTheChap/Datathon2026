"""Stockage des profils (lien L3 : « le profil est enregistré dans DynamoDB (ACTOR#… / PROFILE#…) »).

Analogue local de DynamoDB : en mémoire (tests) ou un fichier JSON par acteur. La clé suit le format
DynamoDB prévu, pour que le passage à boto3 ne change que cette classe.
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
