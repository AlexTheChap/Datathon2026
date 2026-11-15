"""Template renderer tests (internal agents' prompts) — no AWS, no LLM."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.context import template_loader
from app.core.config import Settings


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "orchestrator.md").write_text(
        "Role: test. Persona:\n{{persona_block}}\nTask: {{task}}", encoding="utf-8")
    monkeypatch.setattr(template_loader, "get_settings", lambda: Settings(prompts_dir=tmp_path))
    return tmp_path


def test_substitutes_variables(prompts_dir):
    out = template_loader.render_prompt("orchestrator", persona_block="- Amira, high risk", task="risk_review")
    assert "Persona:\n- Amira, high risk" in out
    assert "Task: risk_review" in out
    assert "{{" not in out


def test_missing_variable_becomes_empty_string(prompts_dir):
    out = template_loader.render_prompt("orchestrator")  # {{task}} not provided
    assert "Task: " in out and "{{task}}" not in out


def test_unknown_role_raises(prompts_dir):
    with pytest.raises(FileNotFoundError):
        template_loader.render_prompt("explainer")


def test_crlf_template_renders_like_lf(prompts_dir):
    (prompts_dir / "orchestrator.md").write_bytes(b"\xef\xbb\xbfLine 1\r\nTask: {{task}}\r\n")
    assert template_loader.render_prompt("orchestrator", task="x") == "Line 1\nTask: x\n"


def test_repo_orchestrator_template_renders():
    """The real app/prompts/orchestrator.md (flat layout) is found and fully substituted."""
    out = template_loader.render_prompt("orchestrator", persona_block="P", task="risk_review", as_of="2026-11-12",
                                        reply_language="en", tools_block="T")
    assert "{{" not in out and "risk_review" in out
