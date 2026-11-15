"""PromptRef: output of the Prompt loader to the Explainer (links L5 and L7 of architecture.mmd).

Only keys travel in the InvokeAgentRuntime payload (L7: level, version): the Runtime reloads the
template from the repo or from S3 with `context.prompt_loader.load_prompt(ref)`, which checks that
`version` and `sha256` did not change between resolution and execution.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .profile import Experience


class PromptRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    role: Literal["explainer"] = "explainer"
    experience: Experience
    version: str = Field(description="`version` field of the template front-matter.")
    key: str = Field(description="Path relative to the templates root, e.g. explainer.advanced.md")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Hash of the whole file (traceability).")
