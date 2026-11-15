"""Intake Lambda (link L2: API -> Intake).

Input (direct invocation, or the JSON body of an HTTP request):
    {"text": "...", "actor_id": "sub-xyz", "profile_form": {...}?, "portfolio_ref": {...}?}
Output: IntakeResult as JSON (status, Profile JSON, Request JSON, optional question, warnings).

No profile storage here yet: the DynamoDB table comes with the API (box 2), so store=None.
"""
from __future__ import annotations

import json
from typing import Any

from app.intake import IntakeError, get_backend, run_intake

_backend = None  # reused across invocations of the same Lambda container


def _parse(event: dict) -> dict:
    body = event.get("body", event)
    if isinstance(body, str):
        body = json.loads(body or "{}")
    return body


def _response(status: int, payload: Any) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": body}


def handler(event: dict, context: Any = None) -> dict:
    global _backend
    try:
        body = _parse(event)
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body."})
    text, actor_id = body.get("text"), body.get("actor_id")
    if not text or not actor_id:
        return _response(400, {"error": "Required fields: text, actor_id."})

    _backend = _backend or get_backend()
    try:
        result = run_intake(text, actor_id, profile_form=body.get("profile_form"),
                            portfolio_ref=body.get("portfolio_ref"), backend=_backend)
    except IntakeError as e:
        return _response(502, {"error": str(e)})
    return _response(200, result.model_dump_json())
