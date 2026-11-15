"""frontend/assets/universe.json is the web app's copy of the asset universe: it must match the curated CSV."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_frontend_universe", ROOT / "scripts" / "build_frontend_universe.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def test_web_universe_is_up_to_date():
    on_disk = json.loads((ROOT / "frontend" / "assets" / "universe.json").read_text(encoding="utf-8"))
    assert on_disk == gen.build(), "run: .venv/bin/python scripts/build_frontend_universe.py"


def test_every_fund_has_a_plain_description():
    assets = gen.build()["assets"]
    funds = [t for t, a in assets.items() if a["class"].startswith("etf")]
    assert funds and all(assets[t]["plain"] != assets[t]["name"] for t in funds)
