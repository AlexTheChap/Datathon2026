"""Intake agent demo (box 3): free text -> Profile JSON + Request JSON.

    python scripts/demo_intake.py                      # persona scenarios, mock mode (offline)
    python scripts/demo_intake.py --text "..."         # your own text
    python scripts/demo_intake.py --mode bedrock       # real model (Claude Haiku on Bedrock, AWS required)

Simulation only: nothing here is financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.intake import get_backend, run_intake  # noqa: E402
from app.store.profiles import InMemoryProfileStore  # noqa: E402

SCENARIOS = [
    ("amira", "I'm 26, intermediate investor, high risk tolerance, horizon of 15 years or more, $50,000. "
              "I love AI and crypto but want some diversification. Is my portfolio of NVDA, QQQ and Bitcoin too risky?"),
    ("mateo", "Balanced profile, intermediate experience, horizon of 8 to 12 years, $150,000. I want to avoid "
              "companies linked to major controversies. Do Enbridge and Exxon pass my ESG screen?"),
    ("robert", "I'm cautious: I want to preserve my capital of $400,000 over 3 to 5 years. "
               "I actively follow the markets, advanced experience. No crypto for me."),
    ("robert", "I'm thinking of putting 20% in Nvidia, everyone is talking about it."),
    ("robert", "Buy 100 Tesla shares for me right now."),
    ("unknown", "Is my portfolio too risky?"),
]


def show(actor: str, text: str, result) -> None:
    print("=" * 100)
    print(f"[{actor}] \"{text}\"")
    print(f"-> status: {result.status}   (backend: {result.backend})")
    if result.clarification_question:
        print(f"-> question: {result.clarification_question}")
    for w in result.warnings:
        print(f"-> warning: {w}")
    if result.profile:
        print("Profile JSON:")
        print(json.dumps(result.profile.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False, indent=2))
    if result.request:
        print("Request JSON:")
        print(json.dumps(result.request.model_dump(mode="json", exclude={"user_message"}), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "bedrock"], default="mock")
    ap.add_argument("--text")
    ap.add_argument("--actor", default="demo")
    args = ap.parse_args()

    backend = get_backend(args.mode)
    store = InMemoryProfileStore()  # keeps Robert's profile between his messages
    for actor, text in ([(args.actor, args.text)] if args.text else SCENARIOS):
        show(actor, text, run_intake(text, actor, backend=backend, store=store))


if __name__ == "__main__":
    main()
