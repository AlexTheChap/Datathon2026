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


# A profile with one of these exclusions ("no controversial companies", ESG activities) excludes EVERY flagged ticker,
# under Exclusion and Observation alike. The optimizer and governance both use this rule, so they never disagree.
ESG_EXCLUSION_TYPES = ("controversy", "esg_activity")


def esg_exclusion_applies(profile: dict) -> bool:
    return any((ex or {}).get("type") in ESG_EXCLUSION_TYPES for ex in (profile or {}).get("exclusions") or [])


def flags_path(data_dir: str | Path | None) -> Optional[str]:
    """data_dir/curated/esg_flags.json when a data folder is given (governance, optimizer), else the default file."""
    return None if data_dir is None else str(Path(data_dir) / "curated" / FILE)


def flags_available(data_dir: str | Path | None = None) -> bool:
    """Is there an exclusion list to check against? Without one, a check is never reported as met."""
    settings = get_settings()
    p = Path(flags_path(data_dir) or settings.abs_path(settings.curated_dir) / FILE)
    return p.is_file()


def excluded_by_flags(tickers, data_dir: str | Path | None = None) -> dict[str, list[dict[str, str]]]:
    """The flagged tickers among `tickers`, each with its flags (reason, source number, source name, since)."""
    data = load_esg_flags(flags_path(data_dir))
    names = {k: v.get("name", "") for k, v in data["sources"].items()}
    out = {}
    for t in tickers:
        flags = (data["companies"].get(t) or {}).get("flags") or []
        if flags:
            out[t] = [{**f, "source_name": names.get(f["source"], "")} for f in flags]
    return out


def flag_text(flagged: dict[str, list[dict[str, str]]]) -> str:
    """"XOM: Excluded because of ... [2]; TD.TO: Under observation because of ... [1]"."""
    parts = []
    for t, fl in flagged.items():
        parts.append(t + ": " + ", ".join(x["reason"] + " [" + x["source"] + "]" for x in fl))
    return "; ".join(parts)
