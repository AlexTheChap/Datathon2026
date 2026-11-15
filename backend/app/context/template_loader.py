"""Template renderer for the internal agents' system prompts (orchestrator, sub-agents) — no LLM.

Different from `prompt_loader.py`, which serves the user-facing Explainer templates through a
versioned PromptRef (L5/L7, sha256-checked). Here: plain `{{variable}}` substitution of
app/prompts/<role>.md, read from the repo at run time (box 5 owns these templates).
A missing variable becomes an empty string.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def template_path(role: str) -> Path:
    """Path of the template for a role: <prompts_dir>/<role>.md (English only, flat folder)."""
    p = get_settings().abs_path(get_settings().prompts_dir) / f"{role}.md"
    if not p.is_file():
        raise FileNotFoundError(f"No template found for role '{role}': {p}")
    return p


def render_prompt(role: str, **variables: object) -> str:
    """Template <role>.md with its {{key}} variables substituted."""
    text = template_path(role).read_text(encoding="utf-8").lstrip("﻿").replace("\r\n", "\n")
    values = {k: str(v) for k, v in variables.items()}
    return _VAR.sub(lambda m: values.get(m.group(1), ""), text)
