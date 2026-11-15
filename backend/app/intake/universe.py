"""Univers d'actifs lu depuis `data/curated/assets_universe.csv` : sert à valider et normaliser les tickers."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "curated" / "assets_universe.csv"

# Noms courants → ticker. Les noms complets de l'univers sont aussi reconnus (voir `aliases`).
EXTRA_ALIASES = {
    "nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "tesla": "TSLA", "shopify": "SHOP.TO", "enbridge": "ENB.TO", "fortis": "FTS.TO",
    "exxon": "XOM", "exxonmobil": "XOM", "jpmorgan": "JPM", "johnson & johnson": "JNJ", "procter": "PG",
    "manulife": "MFC.TO", "couche-tard": "ATD.TO", "constellation software": "CSU.TO",
    "banque royale": "RY.TO", "royal bank": "RY.TO", "rbc": "RY.TO", "banque td": "TD.TO",
    "canadien national": "CNR.TO", "canadian national": "CNR.TO", "cn rail": "CNR.TO",
    "canadian natural": "CNQ.TO", "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "ether": "ETH-USD",
}


@dataclass(frozen=True)
class Asset:
    ticker: str
    name: str
    asset_class: str


@lru_cache(maxsize=4)
def load_universe(path: Optional[str] = None) -> dict[str, Asset]:
    p = Path(path or os.environ.get("AF_UNIVERSE_CSV", DEFAULT_UNIVERSE))
    with p.open(encoding="utf-8") as f:
        return {row["ticker"]: Asset(row["ticker"], row["name"], row["asset_class"]) for row in csv.DictReader(f)}


def aliases(universe: dict[str, Asset]) -> dict[str, str]:
    out = {a.name.lower(): t for t, a in universe.items()}
    out.update({k: v for k, v in EXTRA_ALIASES.items() if v in universe})
    return out


def normalize_ticker(raw: str, universe: dict[str, Asset]) -> Optional[str]:
    """'nvda', 'NVIDIA', 'XIU', 'xiu.to' → ticker de l'univers ; None si inconnu."""
    s = raw.strip()
    up = s.upper()
    if up in universe:
        return up
    for suffix in (".TO", "-USD"):
        if up + suffix in universe:
            return up + suffix
    return aliases(universe).get(s.lower())


def universe_for_prompt(universe: dict[str, Asset]) -> str:
    return "\n".join(f"- {t}: {a.name} ({a.asset_class})" for t, a in universe.items())
