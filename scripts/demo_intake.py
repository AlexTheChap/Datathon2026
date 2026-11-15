"""Démo de l'Intake agent (boîte ③) : texte libre → Profile JSON + Request JSON.

    python scripts/demo_intake.py                      # scénarios des personas, mode mock (hors ligne)
    python scripts/demo_intake.py --text "..."         # votre propre texte
    python scripts/demo_intake.py --mode bedrock       # vrai modèle (Claude Haiku sur Bedrock, AWS requis)

Simulation seulement : rien ici n'est un conseil financier.
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
    ("mateo", "Profil équilibré, expérience intermédiaire, horizon de 8 à 12 ans, 150 000 $. Je veux éviter les "
              "entreprises liées à des controverses importantes. Est-ce qu'Enbridge et Exxon passent mon filtre ESG ?"),
    ("robert", "Je suis prudent : je veux préserver mon capital de 400 000 $ sur 3 à 5 ans. "
               "Je suis activement les marchés, expérience avancée. Pas de crypto pour moi."),
    ("robert", "Je pense mettre 20 % dans Nvidia, tout le monde en parle."),
    ("robert", "Achète-moi 100 actions de Tesla maintenant."),
    ("inconnu", "Est-ce que mon portefeuille est trop risqué ?"),
]


def show(actor: str, text: str, result) -> None:
    print("=" * 100)
    print(f"[{actor}] « {text} »")
    print(f"→ statut : {result.status}   (backend : {result.backend})")
    if result.clarification_question:
        print(f"→ question : {result.clarification_question}")
    for w in result.warnings:
        print(f"→ avertissement : {w}")
    if result.profile:
        print("Profile JSON :")
        print(json.dumps(result.profile.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False, indent=2))
    if result.request:
        print("Request JSON :")
        print(json.dumps(result.request.model_dump(mode="json", exclude={"user_message"}), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "bedrock"], default="mock")
    ap.add_argument("--text")
    ap.add_argument("--actor", default="demo")
    args = ap.parse_args()

    backend = get_backend(args.mode)
    store = InMemoryProfileStore()  # garde le profil de Robert entre ses deux messages
    for actor, text in ([(args.actor, args.text)] if args.text else SCENARIOS):
        show(actor, text, run_intake(text, actor, backend=backend, store=store))


if __name__ == "__main__":
    main()
