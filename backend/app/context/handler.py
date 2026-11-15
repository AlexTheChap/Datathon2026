"""Lambda entry point of ④ Context resolution (links L4, L5 → L6, L7 of docs/architecture.mmd).

Input  : {"profile": <Profile JSON or null>, "request": <Request JSON or null>}   (outputs of ③ Intake)
Output : ContextResult JSON (schemas/context_result.py)
  - {"status": "planned", "execution_plan": {...}, "prompt_ref": {...}}
        → stored with the QUEUED run, then passed to AgentCore Runtime by the dispatcher (L6, L7);
  - {"status": "profile_required", "message": "..."}
        → the user has no investor profile yet: the dispatcher shows `message`, no run is started.

Rules for the dispatcher (box ②): call this Lambda when the Intake status is "ready" or "out_of_scope".
On "needs_clarification", show the Intake's question instead (the profile is usually incomplete).

No LLM call. The only possible external access: reading the template from S3 (AF_PROMPT_SOURCE=s3).
"""
from __future__ import annotations

from typing import Any, Optional

from ..schemas import Profile, Request
from ..schemas.context_result import ContextResult
from .plan_resolver import resolve_plan
from .prompt_loader import PromptSource, resolve_explainer_prompt

PROFILE_REQUIRED_MESSAGE = (
    "Please create your investor profile first: fill in the profile form (risk tolerance, investment "
    "horizon and experience level). AlphaForge needs it before it can analyse any request."
)
OUT_OF_SCOPE_NOTE = (
    " Note that AlphaForge is a simulation: it never places real trades and does not give "
    "personalized financial advice."
)


def _dump(result: ContextResult) -> dict[str, Any]:
    """JSON dict without the top-level keys that do not apply (nested nulls, e.g. plan.as_of, are kept)."""
    return {k: v for k, v in result.model_dump(mode="json").items() if v is not None}


def resolve_context(profile: Profile, request: Request, source: Optional[PromptSource] = None) -> dict[str, Any]:
    return _dump(ContextResult(
        status="planned",
        execution_plan=resolve_plan(profile, request),
        prompt_ref=resolve_explainer_prompt(profile, source),
    ))


def profile_required(request: Optional[Request]) -> dict[str, Any]:
    """No profile: nothing can be planned. The message is fixed text (no LLM, no user input echoed)."""
    message = PROFILE_REQUIRED_MESSAGE
    if request is not None and request.task == "out_of_scope":
        message += OUT_OF_SCOPE_NOTE
    return _dump(ContextResult(status="profile_required", message=message))


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Validates the Intake JSON (Pydantic raises ValidationError if invalid), then resolves."""
    raw_request = event.get("request")
    if event.get("profile") is None:
        # The request, if any, is still validated: garbage is rejected, not silently ignored.
        request = Request.model_validate(raw_request) if raw_request is not None else None
        return profile_required(request)
    profile = Profile.model_validate(event["profile"])
    request = Request.model_validate(raw_request)
    return resolve_context(profile, request)
