"""Prompt template loader tests — no AWS, no LLM."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.context import prompt_loader
from app.core.config import Settings


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "en").mkdir()
    (tmp_path / "en" / "orchestrator.md").write_text(
        "Role: test. Persona:\n{{persona_block}}\nTask: {{task}}", encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "get_settings",
                        lambda: Settings(prompts_dir=tmp_path))
    return tmp_path


def test_substitutes_variables(prompts_dir):
    out = prompt_loader.load_prompt("orchestrator", "en",
                                    persona_block="- Amira, high risk", task="risk_review")
    assert "Persona:\n- Amira, high risk" in out
    assert "Task: risk_review" in out
    assert "{{" not in out


def test_missing_variable_becomes_empty_string(prompts_dir):
    out = prompt_loader.load_prompt("orchestrator", "en")  # {{task}} not provided
    assert "Task: " in out and "{{task}}" not in out


def test_fallback_to_available_language(prompts_dir):
    # no "de" template -> falls back to en
    out = prompt_loader.load_prompt("orchestrator", "de", task="x")
    assert "Role: test" in out


def test_unknown_role_raises(prompts_dir):
    with pytest.raises(FileNotFoundError):
        prompt_loader.load_prompt("explainer", "en")
