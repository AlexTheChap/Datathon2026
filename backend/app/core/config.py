"""Central configuration (AF_* environment variables, no secrets in code).

Maps to the cross-cutting "IAM / config" concern of docs/architecture.mmd:
the code is identical locally and on AgentCore Runtime; only environment
variables change (model, region, AgentCore memory, data paths).

Local development: a .env file at the repo root (never committed — see
.gitignore and .env.example) supplies AWS_* and AF_*. The real environment
always wins (os.environ.setdefault). Local-only convenience: on AgentCore
Runtime, IAM roles replace keys.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dotenv(path: Path) -> None:
    """Inject variables from .env (KEY=value) without overriding the real environment."""
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
    """Every setting is overridable via the environment (AF_ prefix)."""

    model_config = SettingsConfigDict(env_prefix="AF_", env_file=str(REPO_ROOT / ".env"),
                                      env_file_encoding="utf-8", extra="ignore")

    # Bedrock models — node ⑤: Sonnet for the orchestrator (and the Explainer later).
    model_orchestrator: str = "anthropic.claude-sonnet-4-5-v1:0"
    aws_region: str = "us-east-1"

    # AgentCore Memory (L17) — empty = no persistent memory (local development).
    memory_id: str | None = None

    # Data and rules (node ⑥) — repo-root-relative by default.
    curated_dir: Path = REPO_ROOT / "data" / "curated"
    rules_path: Path = REPO_ROOT / "backend" / "app" / "core" / "rules.yaml"
    personas_path: Path = REPO_ROOT / "backend" / "app" / "core" / "personas.json"
    prompts_dir: Path = REPO_ROOT / "backend" / "app" / "prompts"

    # Local evidence ledger (L15) — one JSONL file per run.
    runs_dir: Path = REPO_ROOT / "data" / "runs"

    def abs_path(self, p: Path) -> Path:
        """Resolve a relative path against the repo root."""
        return p if p.is_absolute() else REPO_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
