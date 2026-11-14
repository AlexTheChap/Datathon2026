"""Tests du chargeur de gabarits — sans AWS, sans LLM."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.context import prompt_loader
from app.core.config import Settings


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "fr").mkdir()
    (tmp_path / "en").mkdir()
    (tmp_path / "fr" / "orchestrator.md").write_text(
        "Rôle: test. Persona:\n{{persona_block}}\nTâche: {{task}}", encoding="utf-8")
    (tmp_path / "en" / "orchestrator.md").write_text("Role: test. Task: {{task}}", encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "get_settings",
                        lambda: Settings(prompts_dir=tmp_path))
    return tmp_path


def test_substitue_les_variables(prompts_dir):
    out = prompt_loader.load_prompt("orchestrator", "fr",
                                    persona_block="- Amira, risque élevé", task="risk_review")
    assert "Persona:\n- Amira, risque élevé" in out
    assert "Tâche: risk_review" in out
    assert "{{" not in out


def test_variable_absente_devient_chaine_vide(prompts_dir):
    out = prompt_loader.load_prompt("orchestrator", "en")  # {{task}} non fourni
    assert "Task: " in out and "{{task}}" not in out


def test_repli_sur_langue_disponible(prompts_dir):
    # aucun gabarit « de » -> repli sur en
    out = prompt_loader.load_prompt("orchestrator", "de", task="x")
    assert "Role: test" in out


def test_role_inconnu_leve(prompts_dir):
    with pytest.raises(FileNotFoundError):
        prompt_loader.load_prompt("explainer", "fr")
