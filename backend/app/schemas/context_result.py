"""ContextResult: what the ④ Context resolution Lambda returns to the dispatcher (links L6, L7).

- status "planned": `execution_plan` + `prompt_ref` are set; the dispatcher starts the run.
- status "profile_required": the user has no investor profile yet (the Intake returned `profile: null`).
  Nothing can be planned: the dispatcher shows `message` to the user and does NOT start a run.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from .execution_plan import ExecutionPlan
from .prompt_ref import PromptRef

ContextStatus = Literal["planned", "profile_required"]


class ContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    status: ContextStatus
    execution_plan: Optional[ExecutionPlan] = None
    prompt_ref: Optional[PromptRef] = None
    message: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "ContextResult":
        if self.status == "planned" and (self.execution_plan is None or self.prompt_ref is None):
            raise ValueError("status 'planned' requires execution_plan and prompt_ref")
        if self.status == "profile_required" and (self.execution_plan or self.prompt_ref or not self.message):
            raise ValueError("status 'profile_required' requires a message and no plan")
        return self
