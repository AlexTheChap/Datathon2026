"""Configuration centrale (AF_* variables d'environnement, aucune clé en dur).

Correspond au traversant « IAM / config » du schéma docs/architecture.mmd :
le code est identique en local et sur AgentCore Runtime ; seuls les variables
d'environnement changent (modèle, région, mémoire AgentCore, chemins de données).

Développement local : un fichier .env à la racine du dépôt (jamais committé,
voir .gitignore et .env.example) fournit AWS_* et AF_*. L'environnement réel
prime toujours (os.environ.setdefault). Utile seulement en local : sur AgentCore
Runtime, les rôles IAM remplacent les clés.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> racine du dépôt
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dotenv(path: Path) -> None:
    """Injecte les variables de .env (KEY=value) sans écraser l'environnement réel."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")


class Settings(BaseSettings):
    """Tous les réglages sont surchargeables par l'environnement (préfixe AF_)."""

    model_config = SettingsConfigDict(env_prefix="AF_", env_file=str(REPO_ROOT / ".env"),
                                      env_file_encoding="utf-8", extra="ignore")

    # Modèles Bedrock — nœud ⑤ : Sonnet pour l'orchestrateur (et l'Explainer plus tard).
    model_orchestrator: str = "anthropic.claude-sonnet-4-5-v1:0"
    aws_region: str = "us-east-1"

    # AgentCore Memory (L17) — vide = pas de mémoire persistante (développement local).
    memory_id: str | None = None

    # Données et règles (nœud ⑥) — relatifs à la racine du dépôt par défaut.
    curated_dir: Path = REPO_ROOT / "data" / "curated"
    rules_path: Path = REPO_ROOT / "backend" / "app" / "core" / "rules.yaml"
    personas_path: Path = REPO_ROOT / "backend" / "app" / "core" / "personas.json"
    prompts_dir: Path = REPO_ROOT / "backend" / "app" / "prompts"

    # Registre de preuves local (L15) — un fichier JSONL par exécution.
    runs_dir: Path = REPO_ROOT / "data" / "runs"

    def abs_path(self, p: Path) -> Path:
        """Résout un chemin relatif contre la racine du dépôt."""
        return p if p.is_absolute() else REPO_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
