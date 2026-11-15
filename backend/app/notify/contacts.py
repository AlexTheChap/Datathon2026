"""SMS contact preferences + the log of news already texted, kept APART from the profile.

Why not in Profile: the profile is sent to the agents (Intake, Explainer prompts). A phone number is personal
data that no model needs, so it lives in its own item and never enters a prompt:

    PK "ACTOR#<actor_id>"  SK "CONTACT#sms"            {opt_in, phone, consent_at, updated_at}
    PK "ACTOR#<actor_id>"  SK "SMSSENT#<event_id>"     one item per news item texted (no double send)

Opting out deletes the phone number (it is not kept "just in case").
Three backends with the same interface, like app.store.profiles: memory (tests), JSON files (AF_STORE=file),
DynamoDB (same single table "alphaforge").
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# North American numbers only (+1 and 10 digits): the SMS sandbox and the datathon target Canada / the U.S.
E164_NANP = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")
CONTACT_SK = "CONTACT#sms"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_phone(raw: str) -> str:
    """'(514) 555-0123', '514-555-0123', '+1 514 555 0123' -> '+15145550123'; ValueError otherwise."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "1" + digits
    phone = "+" + digits
    if not E164_NANP.match(phone):
        raise ValueError("Enter a Canadian or U.S. mobile number, like 514 555 0123.")
    return phone


def mask(phone: Optional[str]) -> Optional[str]:
    """'+15145550123' -> '+1 ••• ••• 0123' (logs, API echoes)."""
    return f"+1 ••• ••• {phone[-4:]}" if phone else None


class SmsContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str
    opt_in: bool = False
    phone: Optional[str] = None
    consent_at: Optional[str] = Field(default=None, description="When the person agreed to receive texts.")
    updated_at: str = Field(default_factory=now_iso)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        return normalize_phone(v) if v else None

    @model_validator(mode="after")
    def _consistent(self) -> "SmsContact":
        if self.opt_in and not self.phone:
            raise ValueError("A phone number is required to receive text alerts.")
        if not self.opt_in:          # opting out forgets the number
            self.phone, self.consent_at = None, None
        return self


class SmsPrefsIn(BaseModel):
    """PUT /v1/notifications/sms body."""

    model_config = ConfigDict(extra="forbid")

    opt_in: bool
    phone: Optional[str] = Field(default=None, max_length=30)


class ContactStore(Protocol):
    def get(self, actor_id: str) -> Optional[SmsContact]: ...
    def put(self, contact: SmsContact) -> None: ...
    def opted_in(self) -> list[SmsContact]: ...
    def was_sent(self, actor_id: str, event_id: str) -> bool: ...
    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None: ...


def save_prefs(store: ContactStore, actor_id: str, body: SmsPrefsIn) -> SmsContact:
    """Apply the person's choice; consent time is set when they opt in (kept while they stay opted in)."""
    old = store.get(actor_id)
    consent = old.consent_at if old and old.opt_in and body.opt_in else (now_iso() if body.opt_in else None)
    contact = SmsContact(actor_id=actor_id, opt_in=body.opt_in, phone=body.phone if body.opt_in else None,
                         consent_at=consent)
    store.put(contact)
    return contact


class InMemoryContactStore:
    def __init__(self) -> None:
        self._contacts: dict[str, SmsContact] = {}
        self._sent: set[tuple[str, str]] = set()

    def get(self, actor_id: str) -> Optional[SmsContact]:
        return self._contacts.get(actor_id)

    def put(self, contact: SmsContact) -> None:
        self._contacts[contact.actor_id] = contact

    def opted_in(self) -> list[SmsContact]:
        return [c for c in self._contacts.values() if c.opt_in and c.phone]

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        return (actor_id, event_id) in self._sent

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        self._sent.add((actor_id, event_id))


class JsonFileContactStore:
    """AF_STORE=file: one JSON file per actor next to the profiles (git-ignored .af_store/)."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, actor_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in actor_id)
        return self.dir / f"{safe}.json"

    def _read(self, actor_id: str) -> dict:
        p = self._path(actor_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def get(self, actor_id: str) -> Optional[SmsContact]:
        item = self._read(actor_id).get("contact")
        return SmsContact.model_validate(item) if item else None

    def put(self, contact: SmsContact) -> None:
        item = self._read(contact.actor_id)
        item["contact"] = json.loads(contact.model_dump_json())
        self._path(contact.actor_id).write_text(json.dumps(item, indent=2), encoding="utf-8")

    def opted_in(self) -> list[SmsContact]:
        out = []
        for p in self.dir.glob("*.json"):
            item = json.loads(p.read_text(encoding="utf-8")).get("contact")
            if item and item.get("opt_in") and item.get("phone"):
                out.append(SmsContact.model_validate(item))
        return out

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        return event_id in self._read(actor_id).get("sent", {})

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        item = self._read(actor_id)
        item.setdefault("sent", {})[event_id] = sent_at
        self._path(actor_id).write_text(json.dumps(item, indent=2), encoding="utf-8")


class DynamoDBContactStore:
    """Same single table as profiles and runs. `opted_in` scans for CONTACT#sms items (a few hundred users in
    this prototype; a GSI on opt_in would replace the scan at bank scale)."""

    def __init__(self, table) -> None:
        self.table = table

    def get(self, actor_id: str) -> Optional[SmsContact]:
        item = self.table.get_item(Key={"PK": f"ACTOR#{actor_id}", "SK": CONTACT_SK}).get("Item")
        return SmsContact.model_validate_json(item["data"]) if item else None

    def put(self, contact: SmsContact) -> None:
        self.table.put_item(Item={"PK": f"ACTOR#{contact.actor_id}", "SK": CONTACT_SK,
                                  "opt_in": contact.opt_in, "data": contact.model_dump_json()})

    def opted_in(self) -> list[SmsContact]:
        from boto3.dynamodb.conditions import Attr

        out, kwargs = [], {"FilterExpression": Attr("SK").eq(CONTACT_SK) & Attr("opt_in").eq(True)}
        while True:
            page = self.table.scan(**kwargs)
            out += [SmsContact.model_validate_json(i["data"]) for i in page.get("Items", [])]
            if "LastEvaluatedKey" not in page:
                return [c for c in out if c.opt_in and c.phone]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        key = {"PK": f"ACTOR#{actor_id}", "SK": f"SMSSENT#{event_id}"}
        return "Item" in self.table.get_item(Key=key)

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        self.table.put_item(Item={"PK": f"ACTOR#{actor_id}", "SK": f"SMSSENT#{event_id}", "sent_at": sent_at})
