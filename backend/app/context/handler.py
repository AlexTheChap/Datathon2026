"""Lambda entry point of ④ Context resolution (links L4, L5 → L6, L7 of docs/architecture.mmd).

Input  : {"profile": <Profile JSON>, "request": <Request JSON>}   (outputs of ③ Intake)
Output : {"execution_plan": <ExecutionPlan JSON>, "prompt_ref": <PromptRef JSON>}
         → stored with the QUEUED run, then passed to AgentCore Runtime by the dispatcher (L6, L7).

No LLM call. The only possible external access: reading the template from S3 (AF_PROMPT_SOURCE=s3).
"""
from __future__ import annotations

from typing import Any, Optional

from ..schemas import Profile, Request
from .plan_resolver import resolve_plan
from .prompt_loader import PromptSource, resolve_explainer_prompt


def resolve_context(profile: Profile, request: Request, source: Optional[PromptSource] = None) -> dict[str, Any]:
    plan = resolve_plan(profile, request)
    ref = resolve_explainer_prompt(profile, source)
    return {
        "execution_plan": plan.model_dump(mode="json"),
        "prompt_ref": ref.model_dump(mode="json"),
    }


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Validates both Intake JSONs (Pydantic raises ValidationError if invalid), then resolves."""
    profile = Profile.model_validate(event["profile"])
    request = Request.model_validate(event["request"])
    return resolve_context(profile, request)
