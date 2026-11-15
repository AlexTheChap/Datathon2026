"""Lambda de l'Intake (lien L2 : API → Intake).

Entrée (invocation directe ou corps JSON d'une requête HTTP) :
    {"text": "...", "actor_id": "sub-xyz", "profile_form": {...}?, "portfolio_ref": {...}?}
Sortie : IntakeResult en JSON (statut, Profile JSON, Request JSON, question éventuelle, avertissements).

Pas encore de stockage du profil ici : la table DynamoDB viendra avec l'API ② (store=None).
"""
from __future__ import annotations

import json
from typing import Any

from app.intake import IntakeError, get_backend, run_intake

_backend = None  # réutilisé entre invocations d'un même conteneur Lambda


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
