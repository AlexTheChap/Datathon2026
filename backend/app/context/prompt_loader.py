"""Prompt loader (④, links L5 and L7 of docs/architecture.mmd): Explainer template by experience level.

Deterministic code, no LLM. Two steps, as in the diagram:

- `resolve_explainer_prompt(profile)` (context Lambda, L5) → PromptRef: keys only
  (level, style, version, sha256) that travel in the InvokeAgentRuntime payload (L7);
- `load_prompt(ref)` (dispatcher, before the Explainer) → PromptTemplate: reloads the
  template and refuses to use it if its version or content changed since resolution.

Templates: backend/app/prompts/explainer.<experience>.md, plus explainer.<experience>.learning.md for a reader who
wants to learn (beginner and intermediate; an advanced reader already gets the in-depth one), each with a YAML
front-matter (role, experience, style, version). Source chosen by the AF_PROMPT_SOURCE variable:

- `local` (default): AF_PROMPT_DIR, otherwise backend/app/prompts/;
- `s3`: s3://AF_PROMPT_BUCKET/AF_PROMPT_PREFIX<key> (prefix defaults to "prompts/"), requires boto3.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml

from ..schemas import Profile
from ..schemas.prompt_ref import PromptRef

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
ROLE = "explainer"


class PromptError(Exception):
    """Base error of the Prompt loader."""


class PromptNotFound(PromptError):
    pass


class PromptFormatError(PromptError):
    pass


class PromptVersionMismatch(PromptError):
    """The template changed between resolution (PromptRef) and execution."""


class PromptSource(Protocol):
    def read(self, key: str) -> str: ...


class LocalPromptSource:
    def __init__(self, root: Path | str = PROMPTS_DIR):
        self.root = Path(root).resolve()

    def read(self, key: str) -> str:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise PromptNotFound(f"Key outside the templates root: {key}")
        if not path.is_file():
            raise PromptNotFound(f"Template not found: {path}")
        return path.read_text(encoding="utf-8")


class S3PromptSource:
    """Reads from S3. `client` can be injected (tests); otherwise boto3.client("s3")."""

    def __init__(self, bucket: str, prefix: str = "prompts/", client: Any = None):
        self.bucket = bucket
        self.prefix = prefix
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.client = client

    def read(self, key: str) -> str:
        s3_key = f"{self.prefix}{key}"
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=s3_key)
        except Exception as exc:  # botocore.exceptions.ClientError, without importing botocore
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise PromptNotFound(f"Template not found: s3://{self.bucket}/{s3_key}") from exc
            raise
        return obj["Body"].read().decode("utf-8")


def source_from_env(env: Optional[dict[str, str]] = None) -> PromptSource:
    env = os.environ if env is None else env
    kind = env.get("AF_PROMPT_SOURCE", "local").lower()
    if kind == "local":
        return LocalPromptSource(env.get("AF_PROMPT_DIR") or PROMPTS_DIR)
    if kind == "s3":
        bucket = env.get("AF_PROMPT_BUCKET")
        if not bucket:
            raise PromptError("AF_PROMPT_SOURCE=s3 requires AF_PROMPT_BUCKET")
        return S3PromptSource(bucket, env.get("AF_PROMPT_PREFIX", "prompts/"))
    raise PromptError(f"Unknown AF_PROMPT_SOURCE: {kind} (expected: local | s3)")


@dataclass(frozen=True)
class PromptTemplate:
    role: str
    experience: str
    style: str
    version: str
    key: str
    sha256: str
    body: str


LEARNING_LEVELS = ("beginner", "intermediate")  # advanced readers already get the in-depth template


def explainer_style(experience: str, wants_to_learn: bool) -> str:
    return "learning" if wants_to_learn and experience in LEARNING_LEVELS else "standard"


def explainer_key(experience: str, style: str = "standard") -> str:
    return f"{ROLE}.{experience}.md" if style == "standard" else f"{ROLE}.{experience}.{style}.md"


def _parse(key: str, text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise PromptFormatError(f"{key}: missing YAML front-matter (must start with ---)")
    try:
        _, front, body = text.split("---\n", 2)
    except ValueError as exc:
        raise PromptFormatError(f"{key}: front-matter not closed") from exc
    meta = yaml.safe_load(front) or {}
    for field in ("role", "experience", "version"):
        if not meta.get(field):
            raise PromptFormatError(f"{key}: field '{field}' missing from the front-matter")
    body = body.strip()
    if not body:
        raise PromptFormatError(f"{key}: empty template body")
    return meta, body


def _template(key: str, text: str, experience: str, style: str = "standard") -> PromptTemplate:
    # Normalize line endings (and a stray BOM) before parsing AND hashing: a Windows checkout
    # (core.autocrlf=true) yields CRLF files, which would otherwise fail the "---\n" front-matter
    # check and give a different sha256 than the same template published from Linux.
    text = text.lstrip("﻿").replace("\r\n", "\n")
    meta, body = _parse(key, text)
    meta.setdefault("style", "standard")  # templates written before styles existed
    for field, value in {"role": ROLE, "experience": experience, "style": style}.items():
        if str(meta[field]) != value:
            raise PromptFormatError(f"{key}: {field} = {meta[field]!r} in the front-matter, expected {value!r}")
    return PromptTemplate(
        role=ROLE,
        experience=experience,
        style=style,
        version=str(meta["version"]),
        key=key,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        body=body,
    )


def resolve_explainer_prompt(profile: Profile, source: Optional[PromptSource] = None) -> PromptRef:
    """L5: Profile → PromptRef (keys only) for the Explainer template."""
    src = source or source_from_env()
    style = explainer_style(profile.experience, profile.wants_to_learn)
    key = explainer_key(profile.experience, style)
    tpl = _template(key, src.read(key), profile.experience, style)
    return PromptRef(experience=tpl.experience, style=style, version=tpl.version, key=tpl.key, sha256=tpl.sha256)


def load_prompt(ref: PromptRef, source: Optional[PromptSource] = None) -> PromptTemplate:
    """L7: PromptRef → template, on the Runtime side. Raises PromptVersionMismatch if the file changed."""
    src = source or source_from_env()
    tpl = _template(ref.key, src.read(ref.key), ref.experience, ref.style)
    if tpl.version != ref.version or tpl.sha256 != ref.sha256:
        raise PromptVersionMismatch(
            f"{ref.key}: expected version {ref.version} / sha256 {ref.sha256[:12]}…, "
            f"found version {tpl.version} / sha256 {tpl.sha256[:12]}…"
        )
    return tpl
