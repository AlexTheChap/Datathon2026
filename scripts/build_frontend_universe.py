"""Write frontend/assets/universe.json: the asset universe as the web app shows it (box ①).

    .venv/bin/python scripts/build_frontend_universe.py

The browser cannot read data/curated, so the portfolio dashboard gets this small copy: ticker, name, asset class,
currency, sector, and for funds a plain description a beginner understands ("500 large US companies"). Run it again
whenever data/curated/assets_universe.csv or assets_universe_ext.csv changes; tests/unit/test_frontend_universe.py
fails if the copy is stale.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "curated" / "assets_universe.csv"
EXTENSION = ROOT / "data" / "curated" / "assets_universe_ext.csv"   # S&P 500 constituents (build_extended_universe.py)
TARGET = ROOT / "frontend" / "assets" / "universe.json"

# What a fund holds, in everyday words. Companies and crypto are described by their own name.
PLAIN = {
    "XIU.TO": "60 large Canadian companies",
    "SPY": "500 large US companies",
    "QQQ": "100 large US companies, mostly technology",
    "ESGV": "US companies that pass an ESG screen",
    "XBB.TO": "Canadian government and company bonds",
    "ZST.TO": "Very short-term bonds, close to cash",
    "AGG": "US government and company bonds",
    "TLT": "Long-term US government bonds",
    "GLD": "Gold",
}


def build() -> dict:
    rows = []
    for src in (SOURCE, EXTENSION):   # curated assets first, then the S&P 500 extension when built
        if src.exists():
            with src.open(encoding="utf-8") as f:
                rows += [r for r in csv.DictReader(f) if r["ticker"] not in {x["ticker"] for x in rows}]
    return {"source": "data/curated/assets_universe.csv (+ assets_universe_ext.csv)",
            "assets": {r["ticker"]: {"name": r["name"], "class": r["asset_class"], "currency": r["currency"],
                                     "sector": r["sector"], "plain": PLAIN.get(r["ticker"], r["name"])}
                       for r in rows}}


def main() -> None:
    TARGET.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"• {TARGET.relative_to(ROOT)}: {len(build()['assets'])} assets")


if __name__ == "__main__":
    main()
