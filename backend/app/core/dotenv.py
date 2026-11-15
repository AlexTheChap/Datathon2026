"""Minimal .env reader shared by the backend (app.core.config) and the scripts (no extra dependency).

Injects KEY=value lines without ever overriding a variable already set: the real environment always wins.
No side effect on import: callers decide when (and whether) to load.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_dotenv(path: Path | str = REPO_ROOT / ".env") -> None:
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").removeprefix("$env:").strip()  # pasted bash / PowerShell lines
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)
