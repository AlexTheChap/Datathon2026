"""Profile set-up from a conversation or a document (box ③, link L3): text, PDF or Word → profile fields.

The web app's onboarding chat sends either the person's free text or a file (POST /v1/profile/draft and
/v1/profile/draft/file). This module reads the file's text, asks the intake model for the profile part only
(OnboardingDraft: no analysis request, no run is created), and returns:
  - `fields`: the profile form fields it found (the same shape as PUT /v1/profile);
  - `missing`: the fields still to ask, in the order the chat asks them;
  - `irrelevant`: what in the document says nothing about an investing profile (shown to the person);
  - `summary`: what the document is.
Nothing is saved here: the person reviews the answers and saves the profile themselves (PUT /v1/profile). The
document's text is sent to the model and then dropped; it is never stored. Toggle changes keep the intake rule:
a change whose quoted words are not in the text is discarded.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from .agent import IntakeError, _verified_toggle_changes
from .backends import BedrockBackend, IntakeRefused
from .schemas import ProfileDraft

MAX_CHARS = 40_000          # about 25 pages: enough for any profile document, bounded for the model
MAX_BYTES = 5 * 1024 * 1024
PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "onboarding.md"
# The order the chat asks for what is missing (required first).
ASKED = ("risk_band", "experience", "horizon_years", "capital", "loss_capacity", "objective", "wants_to_learn",
         "exclusions", "agent_toggles")


class OnboardingDraft(BaseModel):
    """Structured model output: the profile part of the intake, plus what was ignored."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileDraft
    summary: str = Field(description="One plain sentence: what the document is.")
    irrelevant: list[str] = Field(description="Short phrases naming the parts that say nothing about the investing profile.")


class OnboardingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    found: list[str]
    missing: list[str]
    irrelevant: list[str] = Field(default_factory=list)
    summary: str = ""
    refused: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    backend: str


# ---------- Documents → text ----------

def document_text(filename: str, data: bytes) -> str:
    """The text of a PDF, Word (.docx), plain-text or Markdown file. ValueError when it can't be read."""
    if len(data) > MAX_BYTES:
        raise ValueError("The file is larger than 5 MB.")
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        text = _pdf_text(data)
    elif name.endswith(".docx") or data[:2] == b"PK":
        text = _docx_text(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError("Use a PDF, a Word (.docx) or a text file.")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 20:
        raise ValueError("No readable text was found in the file (a scanned image can't be read).")
    return text[:MAX_CHARS]


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)
    except ImportError as e:  # pragma: no cover - pypdf is in the api extra
        raise ValueError("PDF reading is not installed on this server (pypdf).") from e
    except Exception as e:
        raise ValueError("The PDF could not be read.") from e


def _docx_text(data: bytes) -> str:
    """A .docx is a zip of XML: the paragraphs of word/document.xml, no extra library."""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        raise ValueError("The Word file could not be read (only .docx is supported).") from e
    return "\n".join("".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p"))


# ---------- Text → profile fields ----------

def to_fields(p: ProfileDraft, toggles) -> dict[str, Any]:
    """ProfileDraft → the profile form fields (PUT /v1/profile), only those the text supports."""
    out: dict[str, Any] = {}
    for k in ("risk_band", "experience", "capital", "loss_capacity", "objective"):
        v = getattr(p, k)
        if v not in (None, ""):
            out[k] = v
    if p.horizon_min_years is not None or p.horizon_max_years is not None:
        out["horizon_years"] = {"min": p.horizon_min_years, "max": p.horizon_max_years}
    for k in ("preferences", "constraints"):
        if getattr(p, k):
            out[k] = list(getattr(p, k))
    if p.exclusions:
        out["exclusions"] = [e.model_dump(mode="json") for e in p.exclusions]
    if p.wants_to_learn is not None:
        out["wants_to_learn"] = p.wants_to_learn
    if toggles:
        out["agent_toggles"] = {t.agent: t.enabled for t in toggles}
    return out


def draft_profile(text: str, backend: Any = None) -> OnboardingResult:
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise ValueError("Nothing to read.")
    reader = backend if hasattr(backend, "extract_profile") else BedrockBackend()  # the Lambda backend reads in-process
    try:
        draft: OnboardingDraft = reader.extract_profile(text)
    except IntakeRefused as refused:
        return OnboardingResult(fields={}, found=[], missing=list(ASKED), refused=str(refused), backend=reader.name)
    except IntakeError:
        raise
    except Exception as e:  # unreachable model, invalid output: a visible error, never a half-filled profile
        raise IntakeError(f"The document could not be read by the language model: {type(e).__name__}: {str(e)[:300]}") from e
    warnings: list[str] = []
    toggles = _verified_toggle_changes(text, draft.profile.agent_toggle_changes, warnings)
    fields = to_fields(draft.profile, toggles)
    return OnboardingResult(fields={**fields, "language": "en"}, found=[k for k in ASKED if k in fields],
                            missing=[k for k in ASKED if k not in fields], irrelevant=draft.irrelevant[:12],
                            summary=draft.summary, warnings=warnings, backend=reader.name)


def onboarding_prompt(base: str) -> str:
    return base + "\n" + PROMPT.read_text(encoding="utf-8")
