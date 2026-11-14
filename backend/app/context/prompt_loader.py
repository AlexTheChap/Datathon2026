"""Chargeur de gabarits (L5/L7) — code déterministe, sans LLM.

Gabarits versionnés dans app/prompts/{langue}/{role}.md (le dépôt Git est la
source ; en mode aws ils sont publiés vers S3 / Bedrock Prompt Management).
Substitution simple {{variable}} ; une variable absente devient une chaîne vide.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def prompt_path(role: str, language: str = "fr") -> Path:
    """Chemin du gabarit pour un rôle et une langue ; repli sur l'anglais, sinon le français."""
    base = get_settings().abs_path(get_settings().prompts_dir)
    for lang in dict.fromkeys([language, "en", "fr"]):  # ordre sans doublons
        p = base / lang / f"{role}.md"
        if p.exists():
            return p
    raise FileNotFoundError(f"Gabarit introuvable pour le rôle « {role} » dans {base}")


def load_prompt(role: str, language: str = "fr", **variables: str) -> str:
    """Gabarit {role} en {language}, variables {{clé}} substituées."""
    text = prompt_path(role, language).read_text(encoding="utf-8")
    values = {k: str(v) for k, v in variables.items()}

    def sub(m: re.Match) -> str:
        return values.get(m.group(1), "")

    return _VAR.sub(sub, text)
