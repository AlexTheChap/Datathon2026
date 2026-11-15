"""ESG exclusion flags per ticker (data/curated/esg_flags.json, built by scripts/build_esg_flags.py).

    load_esg_flags()            -> {"generated_at", "sources": {"1": {name, url, as_of}, ...}, "companies": {ticker: ...}}
    flags_for("BA")             -> [{"reason": "Excluded because of production of nuclear weapons", "source": "1", "since": "2006-01-05"}]

Each flag's reason never names its source: show it with a numbered citation, "<reason> [<source>]", and list the
sources from load_esg_flags()["sources"]. Non-commercial use only (datathon prototype).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings

FILE = "esg_flags.json"
EMPTY: dict[str, Any] = {"generated_at": None, "sources": {}, "companies": {}}


@lru_cache(maxsize=4)
def load_esg_flags(path: Optional[str] = None) -> dict[str, Any]:
    """The parsed JSON, read once per process. A missing file gives an empty dataset (no flags), never an error."""
    settings = get_settings()
    p = Path(path) if path else settings.abs_path(settings.curated_dir) / FILE
    if not p.is_file():
        return EMPTY
    return json.loads(p.read_text(encoding="utf-8"))


def flags_for(ticker: str, path: Optional[str] = None) -> list[dict[str, str]]:
    """The flags of one ticker (empty when it has none)."""
    return list((load_esg_flags(path)["companies"].get(ticker) or {}).get("flags", []))
