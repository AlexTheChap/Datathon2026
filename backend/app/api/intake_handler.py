"""Intake Lambda `alphaforge-intake` (box ③, link L2: API -> Intake).

Two operations (direct invocation, or the JSON body of an HTTP request):

- {"op": "extract", "text": "...", "known_profile": {...}, "feedback": "..."?}  ← what the API calls
  (AF_INTAKE_MODE=lambda → intake.backends.LambdaBackend). Only the model step: Guardrails + Claude Haiku
  structured output. The API keeps the merge with the stored profile, the toggles and the storage, so this
  function needs Bedrock but no database. Output: {"draft": IntakeDraft} | {"refused": reason} |
  {"invalid": message} (the model's output did not validate; the API retries once with it as feedback).
- {"text": "...", "actor_id": "sub-xyz", "profile_form": {...}?, "portfolio_ref": {...}?}  (no "op")
  The whole intake without a profile store (console tests, scripts/deploy_intake_lambda.py --test).
  Output: IntakeResult JSON (status, Profile JSON, Request JSON, optional question, warnings).
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.intake import IntakeError, IntakeRefused, get_backend, run_intake

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
    _backend = _backend or get_backend()
    if body.get("op") == "extract":
        return _extract(body)
    text, actor_id = body.get("text"), body.get("actor_id")
    if not text or not actor_id:
        return _response(400, {"error": "Required fields: text, actor_id."})

    try:
        result = run_intake(text, actor_id, profile_form=body.get("profile_form"),
                            portfolio_ref=body.get("portfolio_ref"), backend=_backend)
    except IntakeError as e:
        return _response(502, {"error": str(e)})
    return _response(200, result.model_dump_json())


def _extract(body: dict) -> dict:
    """The model step only (Guardrails + Haiku); see the module docstring."""
    text = body.get("text")
    if not text:
        return _response(400, {"error": "Required field: text."})
    try:
        draft = _backend.extract(text, body.get("known_profile") or {}, feedback=body.get("feedback"))
    except IntakeRefused as e:
        return _response(200, {"refused": str(e)})
    except (ValidationError, ValueError) as e:
        return _response(200, {"invalid": str(e)})
    return _response(200, {"draft": draft.model_dump(mode="json")})
