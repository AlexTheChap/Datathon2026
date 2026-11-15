"""Notification preferences (channel, address, alert types) + the log of news already texted, kept APART from the
profile. One record per person: on/off; channel "sms" (mobile number) or "email" (address, the account's by
default); which alerts: "up" / "down" (a stock they hold moves) and "news" (news on a stock they hold). Alerts are
only ever about stocks the person holds. Records saved before channels existed read as {channel: "sms", alerts:
["news"]}.

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
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# North American numbers only (+1 and 10 digits): the SMS sandbox and the datathon target Canada / the U.S.
E164_NANP = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")
CONTACT_SK = "CONTACT#sms"     # historical name: the item now holds every channel
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
Channel = Literal["sms", "email"]
AlertType = Literal["up", "down", "news"]
ALERT_TYPES: tuple[str, ...] = ("up", "down", "news")


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
    """A person's notification choice (the name is historical: email is a channel too)."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str
    opt_in: bool = False
    channel: Channel | None = None
    phone: Optional[str] = None
    email: str | None = None
    alerts: list[AlertType] = Field(default_factory=lambda: ["news"])
    consent_at: Optional[str] = Field(default=None, description="When the person agreed to receive alerts.")
    updated_at: str = Field(default_factory=now_iso)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        return normalize_phone(v) if v else None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip()
        if not EMAIL.match(v) or len(v) > 254:
            raise ValueError("Enter a valid email address, like name@example.com.")
        return v

    @field_validator("alerts")
    @classmethod
    def _alerts(cls, v: list[str]) -> list[str]:
        return [a for a in ALERT_TYPES if a in v]          # known order, no duplicates

    @model_validator(mode="after")
    def _consistent(self) -> "SmsContact":
        if self.opt_in:
            if self.channel is None:                     # saved before channels existed: texts
                self.channel = "sms"
            if not self.alerts:
                raise ValueError("Pick at least one alert, or turn notifications off.")
            if self.channel == "sms" and not self.phone:
                raise ValueError("A phone number is required to receive text alerts.")
            if self.channel == "email" and not self.email:
                raise ValueError("An email address is required to receive email alerts.")
            if self.channel == "sms":                    # keep only the address the channel uses
                self.email = None
            else:
                self.phone = None
        else:                                            # opting out forgets the addresses
            self.channel, self.phone, self.email, self.consent_at = None, None, None, None
        return self

    @property
    def texts_news(self) -> bool:
        """Gets the SMS news alerts (app.notify.alerts)."""
        return bool(self.opt_in and self.channel == "sms" and self.phone and "news" in self.alerts)


class SmsPrefsIn(BaseModel):
    """PUT /v1/notifications/sms body (text alerts only; kept for older pages)."""

    model_config = ConfigDict(extra="forbid")

    opt_in: bool
    phone: Optional[str] = Field(default=None, max_length=30)


class NotifyPrefsIn(BaseModel):
    """PUT /v1/notifications body. `email` empty = the address of the signed-in account."""

    model_config = ConfigDict(extra="forbid")

    opt_in: bool
    channel: Channel | None = None
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=254)
    alerts: list[AlertType] = Field(default_factory=lambda: list(ALERT_TYPES))


class ContactStore(Protocol):
    def get(self, actor_id: str) -> Optional[SmsContact]: ...
    def put(self, contact: SmsContact) -> None: ...
    def opted_in(self) -> list[SmsContact]: ...
    def was_sent(self, actor_id: str, event_id: str) -> bool: ...
    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None: ...

    def delete(self, actor_id: str) -> None: ...


def save_notify(store: ContactStore, actor_id: str, body: NotifyPrefsIn, account_email: str | None = None
                ) -> SmsContact:
    """Apply the person's notification choice. The consent time is kept while they stay opted in."""
    old = store.get(actor_id)
    consent = old.consent_at if old and old.opt_in and body.opt_in else (now_iso() if body.opt_in else None)
    channel = (body.channel or "sms") if body.opt_in else None
    contact = SmsContact(actor_id=actor_id, opt_in=body.opt_in, channel=channel,
                         phone=body.phone if channel == "sms" else None,
                         email=(body.email or account_email) if channel == "email" else None,
                         alerts=body.alerts, consent_at=consent)
    store.put(contact)
    return contact


def save_prefs(store: ContactStore, actor_id: str, body: SmsPrefsIn) -> SmsContact:
    """Apply the person's choice; consent time is set when they opt in (kept while they stay opted in)."""
    old = store.get(actor_id)
    consent = old.consent_at if old and old.opt_in and body.opt_in else (now_iso() if body.opt_in else None)
    alerts = old.alerts if old and old.opt_in and old.channel == "sms" else ["news"]
    contact = SmsContact(actor_id=actor_id, opt_in=body.opt_in, channel="sms" if body.opt_in else None,
                         phone=body.phone if body.opt_in else None, alerts=alerts, consent_at=consent)
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
        return [c for c in self._contacts.values() if c.opt_in]

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        return (actor_id, event_id) in self._sent

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        self._sent.add((actor_id, event_id))

    def delete(self, actor_id: str) -> None:
        self._contacts.pop(actor_id, None)
        self._sent = {k for k in self._sent if k[0] != actor_id}


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
            if item and item.get("opt_in"):
                out.append(SmsContact.model_validate(item))
        return out

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        return event_id in self._read(actor_id).get("sent", {})

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        item = self._read(actor_id)
        item.setdefault("sent", {})[event_id] = sent_at
        self._path(actor_id).write_text(json.dumps(item, indent=2), encoding="utf-8")

    def delete(self, actor_id: str) -> None:
        self._path(actor_id).unlink(missing_ok=True)


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
                return [c for c in out if c.opt_in]
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def was_sent(self, actor_id: str, event_id: str) -> bool:
        key = {"PK": f"ACTOR#{actor_id}", "SK": f"SMSSENT#{event_id}"}
        return "Item" in self.table.get_item(Key=key)

    def mark_sent(self, actor_id: str, event_id: str, sent_at: str) -> None:
        self.table.put_item(Item={"PK": f"ACTOR#{actor_id}", "SK": f"SMSSENT#{event_id}", "sent_at": sent_at})

    def delete(self, actor_id: str) -> None:
        """The choice and the log of texts sent."""
        self.table.delete_item(Key={"PK": f"ACTOR#{actor_id}", "SK": CONTACT_SK})
        kwargs = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                  "ExpressionAttributeValues": {":pk": f"ACTOR#{actor_id}", ":sk": "SMSSENT#"}}
        while True:
            page = self.table.query(**kwargs)
            for i in page.get("Items", []):
                self.table.delete_item(Key={"PK": i["PK"], "SK": i["SK"]})
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
