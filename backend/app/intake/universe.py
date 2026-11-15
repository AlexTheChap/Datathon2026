"""Asset universe read from `data/curated/assets_universe.csv` (+ `assets_universe_ext.csv`, the S&P 500
constituents added by scripts/build_extended_universe.py, when present next to it): used to validate and
normalise tickers."""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_UNIVERSE = REPO_ROOT / "data" / "curated" / "assets_universe.csv"

# Common names -> ticker. The full names from the universe are recognised too (see `aliases`).
EXTRA_ALIASES = {
    "nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "tesla": "TSLA", "shopify": "SHOP.TO", "enbridge": "ENB.TO", "fortis": "FTS.TO",
    "exxon": "XOM", "exxonmobil": "XOM", "jpmorgan": "JPM", "johnson & johnson": "JNJ", "procter": "PG",
    "manulife": "MFC.TO", "couche-tard": "ATD.TO", "constellation software": "CSU.TO",
    "royal bank": "RY.TO", "rbc": "RY.TO", "td bank": "TD.TO",
    "canadian national": "CNR.TO", "cn rail": "CNR.TO", "canadian natural": "CNQ.TO",
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "ether": "ETH-USD",
    "costco": "COST", "berkshire": "BRK-B", "berkshire hathaway": "BRK-B", "coca-cola": "KO", "coke": "KO",
}

# Extension tickers that are also everyday words or acronyms written in capitals ("ALL in", "IT sector", "ON
# my own", "LOW risk"): a free-text scan must not read them as a holding. An explicit ticker field still resolves.
COMMON_WORDS = frozenset({
    "ALL", "ARE", "BE", "ON", "IT", "KEY", "KEYS", "LOW", "NOW", "HAS", "SO", "MO", "PM", "GE", "DE", "EL", "MA",
    "MS", "ES", "ED", "EG", "IP", "IR", "PH", "RF", "CB", "DD", "HD", "GS", "CAT", "FAST", "FIX", "FLEX", "GEN",
    "HOOD", "TECH", "ICE", "BALL", "USB", "ECHO", "COST", "CASY", "LIN", "MET", "PEG", "UPS", "IEX", "EXE", "TT",
})
# Legal-form words stripped from the end of an extension name to get the name people use ("Visa Inc Class A").
_SUFFIX = re.compile(r"(?:\s+(?:&|inc|corp|corporation|co|company|ltd|plc|nv|sa|(?:class|cl) [a-c]|cl|shares?|holdings?|"
                     r"group|the|[a-c])\.?)+$")
# Short names that are also everyday words ("any news?", "my target", "a block of shares"): no alias from them.
AMBIGUOUS_NAMES = frozenset({"ball", "block", "booking", "carnival", "everest", "flex", "news", "progressive",
                             "target", "waters"})


@dataclass(frozen=True)
class Asset:
    ticker: str
    name: str
    asset_class: str
    extended: bool = False      # S&P 500 extension: price history only (no fundamentals, ESG detail, 10-K)


def _read(p: Path, extended: bool) -> dict[str, Asset]:
    with p.open(encoding="utf-8") as f:
        return {r["ticker"]: Asset(r["ticker"], r["name"], r["asset_class"], extended) for r in csv.DictReader(f)}


@lru_cache(maxsize=4)
def load_universe(path: Optional[str] = None) -> dict[str, Asset]:
    p = Path(path or os.environ.get("AF_UNIVERSE_CSV", DEFAULT_UNIVERSE))
    ext = p.with_name(f"{p.stem}_ext{p.suffix}")
    return {**(_read(ext, True) if ext.is_file() else {}), **_read(p, False)}   # curated rows win


def short_name(name: str) -> str:
    """'Visa Inc Class A Shares' -> 'visa'; 'Ford Motor Co' -> 'ford motor'."""
    return _SUFFIX.sub("", name.lower().replace("+", "&").replace(",", "").replace(".", "")).strip()


def display_name(name: str) -> str:
    """The name to show a reader: 'Archer Daniels Midland Co' -> 'Archer Daniels Midland', 'Eli Lilly + Co' ->
    'Eli Lilly'. Curated names ('Royal Bank of Canada') come out unchanged."""
    n = re.sub(r"/the\b", "", name.replace(" + ", " & ").replace("+", "&"), flags=re.I).strip()
    short = re.sub(_SUFFIX.pattern, "", n, flags=re.I).strip(" ,.&")
    if len(short) < 2:
        return n
    return short.upper() if len(short) <= 3 and " " not in short else short   # 'Cme' -> 'CME' (acronym names)


def aliases(universe: dict[str, Asset]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t, a in universe.items():
        if a.extended and len(short := short_name(a.name)) >= 4 and short not in AMBIGUOUS_NAMES:
            out.setdefault(short, t)
    out.update({a.name.lower(): t for t, a in universe.items()})
    out.update({k: v for k, v in EXTRA_ALIASES.items() if v in universe})
    return out


def match_symbol(symbol: str, universe: dict[str, Asset]) -> Optional[str]:
    """Exact ticker, or the symbol with its TSX (.TO) / crypto (-USD) suffix: 'XIU' -> 'XIU.TO'.
    Share classes written with a dot ('BRK.B') map to Yahoo's dash form ('BRK-B')."""
    if not symbol.endswith(".TO"):
        symbol = symbol.replace(".", "-")
    return next((t for t in (symbol, symbol + ".TO", symbol + "-USD") if t in universe), None)


def normalize_ticker(raw: str, universe: dict[str, Asset]) -> Optional[str]:
    """'nvda', 'NVIDIA', 'XIU', 'xiu.to' -> universe ticker; None if unknown."""
    s = raw.strip()
    return match_symbol(s.upper(), universe) or aliases(universe).get(s.lower())


def universe_for_prompt(universe: dict[str, Asset]) -> str:
    """The curated assets one per line; the S&P 500 extension in one compact line (tickers only)."""
    lines = [f"- {t}: {a.name} ({a.asset_class})" for t, a in universe.items() if not a.extended]
    ext = sorted(t for t, a in universe.items() if a.extended)
    if ext:
        lines.append(f"- Also accepted, by ticker: these {len(ext)} other S&P 500 constituents (US equities, "
                     f"price history only): {' '.join(ext)}")
    return "\n".join(lines)
