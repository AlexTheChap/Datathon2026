"""Lambda entry point of ④ Context resolution (links L4, L5 → L6, L7 of docs/architecture.mmd).

Input  : {"profile": <Profile JSON or null>, "request": <Request JSON or null>}   (outputs of ③ Intake)
Output : ContextResult JSON (schemas/context_result.py)
  - {"status": "planned", "execution_plan": {...}, "prompt_ref": {...}}
        → stored with the QUEUED run, then passed to the orchestrator by the dispatcher (L6, L7);
  - {"status": "profile_required", "message": "..."}
        → the user has no investor profile yet: the dispatcher shows `message`, no run is started.

Rules for the dispatcher (box ②): call this Lambda when the Intake status is "ready" or "out_of_scope".
On "needs_clarification", show the Intake's question instead (the profile is usually incomplete).

No LLM call. The only possible external access: reading the template from S3 (AF_PROMPT_SOURCE=s3).

Called in-process by default. With AF_CONTEXT_MODE=lambda, a caller that is not the Lambda runtime (the API:
it passes no `context`) invokes the deployed function instead (AF_CONTEXT_FUNCTION, default
"alphaforge-context"), so the API needs no access to the template bucket. The Lambda runtime always passes a
`context`, so the deployed function resolves locally and never calls itself.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..schemas import Profile, Request
from ..schemas.context_result import ContextResult
from .plan_resolver import resolve_plan
from .prompt_loader import PromptSource, resolve_explainer_prompt

PROFILE_REQUIRED_MESSAGE = (
    "Please create your investor profile first: fill in the profile form (risk tolerance, investment "
    "horizon and experience level). Fitfolio needs it before it can analyse any request."
)
OUT_OF_SCOPE_NOTE = (
    " Note that Fitfolio is a simulation: it never places real trades and does not give "
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


DEFAULT_CONTEXT_FUNCTION = "alphaforge-context"


class ContextUnavailable(RuntimeError):
    """The context Lambda could not be reached or failed."""


def invoke_remote(event: dict[str, Any], client: Any = None, function: Optional[str] = None) -> dict[str, Any]:
    """One synchronous invocation of the deployed context Lambda; same input and output as `handler`."""
    function = function or os.environ.get("AF_CONTEXT_FUNCTION", DEFAULT_CONTEXT_FUNCTION)
    if client is None:
        import boto3

        client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        out = client.invoke(FunctionName=function, Payload=json.dumps(event, default=str).encode())
    except Exception as e:  # network, permissions, throttling
        raise ContextUnavailable(f"Context Lambda {function} unreachable: {type(e).__name__}: {e}") from e
    result = json.loads(out["Payload"].read() or b"{}")
    if out.get("FunctionError"):
        raise ContextUnavailable(f"Context Lambda {function} failed: {result.get('errorMessage', result)}")
    return result


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Validates the Intake JSON (Pydantic raises ValidationError if invalid), then resolves."""
    if context is None and os.environ.get("AF_CONTEXT_MODE", "local").lower() == "lambda":
        return invoke_remote(event)
    raw_request = event.get("request")
    if event.get("profile") is None:
        # The request, if any, is still validated: garbage is rejected, not silently ignored.
        request = Request.model_validate(raw_request) if raw_request is not None else None
        return profile_required(request)
    profile = Profile.model_validate(event["profile"])
    request = Request.model_validate(raw_request)
    return resolve_context(profile, request)
