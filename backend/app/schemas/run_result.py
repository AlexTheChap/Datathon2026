"""The Explainer's answer (box ⑤ EXP → L18 RunResult → L19 result view).

- ExplainerDraft: what Claude returns (Bedrock structured outputs, validated by Pydantic). Every text item
  carries the evidence ids that back it.
- RunAnswer: what is stored with the run and shown to the user, after the deterministic claim check
  (agents/claim_check.py) has removed anything it could not tie to the evidence.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .prompt_ref import PromptRef

Verdict = Literal["PASS", "WARN", "BLOCK", "NON_VERIFIE"]


class AnswerPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    evidence: list[str] = Field(description="Ids of the evidence items that back every fact and number in text.")


class AnswerCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation: str = Field(description='What was judged, in plain words, e.g. "Your idea: Nvidia at 20%".')
    verdict: Verdict
    text: str
    evidence: list[str]


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    meaning: str


class ExplainerDraft(BaseModel):
    """Structured output requested from the model."""

    model_config = ConfigDict(extra="forbid")

    short_answer: str
    short_answer_evidence: list[str]
    key_points: list[AnswerPoint]
    checks: list[AnswerCheck]
    limitations: list[AnswerPoint]
    glossary: list[GlossaryEntry]
    clarifying_question: Optional[str]


class RemovedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str
    text: str
    reason: str


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims_checked: int
    removed: list[RemovedClaim] = Field(default_factory=list)
    unknown_ids_dropped: int = 0
    retried: bool = False


DISCLAIMER = "Simulation only. This is not investment advice, and no real trade is placed. Past returns do not predict future returns."


class RunAnswer(BaseModel):
    """Final answer of a run, in the reader's style, every remaining claim backed by evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    kind: Literal["explained", "declined", "fallback"]
    short_answer: str
    short_answer_evidence: list[str] = Field(default_factory=list)
    key_points: list[AnswerPoint] = Field(default_factory=list)
    checks: list[AnswerCheck] = Field(default_factory=list)
    limitations: list[AnswerPoint] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    clarifying_question: Optional[str] = None
    not_analysed: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER
    verification: Optional[Verification] = None
    note: Optional[str] = Field(default=None, description="Why a declined or fallback answer was used.")
    model: Optional[str] = None
    prompt: Optional[PromptRef] = None
    duration_ms: Optional[int] = None
