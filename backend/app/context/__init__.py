"""④ Context resolution — deterministic code, no LLM (see docs/architecture.mmd)."""
from .handler import handler, resolve_context
from .plan_resolver import PlanRegistry, load_registry, resolve_plan
from .prompt_loader import (
    LocalPromptSource,
    PromptError,
    PromptFormatError,
    PromptNotFound,
    PromptTemplate,
    PromptVersionMismatch,
    S3PromptSource,
    load_prompt,
    resolve_explainer_prompt,
    source_from_env,
)

__all__ = [
    "handler",
    "resolve_context",
    "PlanRegistry",
    "load_registry",
    "resolve_plan",
    "LocalPromptSource",
    "PromptError",
    "PromptFormatError",
    "PromptNotFound",
    "PromptTemplate",
    "PromptVersionMismatch",
    "S3PromptSource",
    "load_prompt",
    "resolve_explainer_prompt",
    "source_from_env",
]
