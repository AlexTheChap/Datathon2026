"""Chargeur .env local — injecte sans écraser l'environnement réel."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.config import _load_dotenv


def test_injecte_et_ignorer_commentaires(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AF_TEST_A", raising=False)
    f = tmp_path / "e"
    f.write_text('# commentaire\nAF_TEST_A="valeur"\nLIGNE_INVALIDE\n', encoding="utf-8")
    _load_dotenv(f)
    assert os.environ["AF_TEST_A"] == "valeur"


def test_environnement_reel_prime(tmp_path: Path):
    os.environ["AF_TEST_B"] = "reel"
    f = tmp_path / "e"
    f.write_text("AF_TEST_B=fichier\n", encoding="utf-8")
    _load_dotenv(f)
    assert os.environ["AF_TEST_B"] == "reel"


def test_fichier_absent_silencieux(tmp_path: Path):
    _load_dotenv(tmp_path / "inexistant")  # ne doit rien lever
