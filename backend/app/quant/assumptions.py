"""Projection assumptions (backend/app/core/projection_assumptions.yaml) and the ticker -> category mapping.

Expected returns come only from the FP Canada / Institute of Financial Planning 2026 Projection Assumption
Guidelines (PAG) rates in the YAML, or from the labelled team assumptions (gold, crypto: no PAG category).
They are never estimated from past prices. AF_PROJECTION_ASSUMPTIONS overrides the file path (the SageMaker
job ships its own copy next to the code).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "core" / "projection_assumptions.yaml"
EQUITY_CLASSES = ("equity", "etf_equity")


class UnmappedTickerError(KeyError):
    """A ticker has no PAG category (nor a labelled team assumption): no expected return can be given."""


@dataclass(frozen=True)
class Category:
    name: str
    rate: float                  # nominal compound annual, before fees
    team_assumption: bool        # True = not from the PAG


def assumptions_path() -> Path:
    return Path(os.environ.get("AF_PROJECTION_ASSUMPTIONS") or DEFAULT_PATH)


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_assumptions(path: str | Path | None = None) -> dict:
    return _load(str(path or assumptions_path()))


def category_of(ticker: str, asset_class: str | None = None, market: str | None = None,
                cfg: dict | None = None) -> Category:
    """Explicit ticker list first, then equity_<market> for equities and equity ETFs. Raises UnmappedTickerError."""
    cfg = cfg or load_assumptions()
    mapping = cfg["mapping"]
    name = mapping.get("by_ticker", {}).get(ticker)
    if name is None and asset_class in EQUITY_CLASSES and market:
        name = mapping.get("by_asset_class_and_market", {}).get(f"equity_{market}")
    if name is None:
        raise UnmappedTickerError(f"{ticker}: no projection category (asset_class={asset_class!r}, market={market!r})")
    if name in cfg["rates"]:
        return Category(name, float(cfg["rates"][name]), False)
    if name in cfg.get("team_assumptions", {}):
        return Category(name, float(cfg["team_assumptions"][name]), True)
    raise UnmappedTickerError(f"{ticker}: category {name!r} has no rate in the assumptions file")
