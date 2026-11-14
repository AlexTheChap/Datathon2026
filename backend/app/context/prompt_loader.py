"""Prompt template loader (L5/L7) — deterministic code, no LLM.

Templates are versioned in app/prompts/{language}/{role}.md (the Git repo is
the source; in aws mode they are published to S3 / Bedrock Prompt Management).
Simple {{variable}} substitution; a missing variable becomes an empty string.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def prompt_path(role: str, language: str = "en") -> Path:
    """Path of the template for a role and language; falls back to English, then French."""
    base = get_settings().abs_path(get_settings().prompts_dir)
    for lang in dict.fromkeys([language, "en", "fr"]):  # ordered, deduplicated
        p = base / lang / f"{role}.md"
        if p.exists():
            return p
    raise FileNotFoundError(f"No template found for role '{role}' in {base}")


def load_prompt(role: str, language: str = "en", **variables: str) -> str:
    """Template {role} in {language}, {{key}} variables substituted."""
    text = prompt_path(role, language).read_text(encoding="utf-8")
    values = {k: str(v) for k, v in variables.items()}

    def sub(m: re.Match) -> str:
        return values.get(m.group(1), "")

    return _VAR.sub(sub, text)
