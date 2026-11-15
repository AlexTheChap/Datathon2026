"""Shared test setup: makes `app` importable and builds Profile / Request objects from
core/personas.json (Amira, Mateo, Robert). Run from the repo root: python -m pytest -q tests
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
EXAMPLES = APP / "schemas" / "examples"

if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

# The tests are offline: a developer's .env (AF_MEMORY_ID) must never make them call AgentCore.
os.environ["AF_MEMORY_ID"] = ""
from app.schemas import Profile, Request  # noqa: E402


def _persona_profile(p: dict, **overrides) -> Profile:
    lo, hi = p["horizon_years"]
    data = {
        "actor_id": f"persona-{p['id']}",
        "risk_band": p["risk_band"],
        "horizon_years": {"min": lo, "max": hi},
        "experience": p["experience"],
        "language": p["language"],
        "objective": p["objective"],
        "preferences": p["preferences"],
        "constraints": p["constraints"],
        "exclusions": p["exclusions"],
        "capital": p["simulated_capital"]["value"],
    }
    data.update(overrides)
    return Profile.model_validate(data)


@pytest.fixture(scope="session")
def personas() -> dict[str, dict]:
    raw = json.loads((APP / "core" / "personas.json").read_text(encoding="utf-8"))
    return {p["id"]: p for p in raw["personas"]}


@pytest.fixture
def make_profile(personas):
    def _make(persona_id: str, **overrides) -> Profile:
        return _persona_profile(personas[persona_id], **overrides)

    return _make


@pytest.fixture
def make_request():
    def _make(actor_id: str, task: str, **overrides) -> Request:
        data = {
            "request_id": "req-test",
            "actor_id": actor_id,
            "task": task,
            "portfolio_ref": {"portfolio_id": "pf-test"},
            "user_message": "test",
            "as_of": "2026-11-12",
            "intake_confidence": 0.9,
        }
        data.update(overrides)
        return Request.model_validate(data)

    return _make


@pytest.fixture
def example_profile() -> Profile:
    return Profile.model_validate_json((EXAMPLES / "profile.example.json").read_text(encoding="utf-8"))


@pytest.fixture
def example_request() -> Request:
    return Request.model_validate_json((EXAMPLES / "request.example.json").read_text(encoding="utf-8"))
