"""Test de bout en bout de l'orchestrateur : le scénario « Robert » de l'énoncé.

« Je pense mettre 20 % dans Nvidia, tout le monde en parle. » — Robert, profil
prudent, horizon 3-5 ans. L'orchestrateur doit appeler les outils (gouvernance,
stress), identifier le conflit avec le profil et remettre la demande en question
au lieu d'acquiescer. Nécessite des identifiants AWS (Bedrock).

Usage : .venv/bin/python scripts/smoke_orchestrator.py [--personna amira|mateo|robert]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agents.orchestrator import run_orchestrator  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.schemas.models import AnalysisRequest, OrchestratorInput, Profile  # noqa: E402
from app.store.ledger import JsonlLedger  # noqa: E402

# Portefeuille fictif actuel de Robert (personas.json, champ demande_demo)
ROBERT_PORTFOLIO = {"XIU.TO": 0.30, "XBB.TO": 0.30, "ZST.TO": 0.10, "RY.TO": 0.10,
                    "FTS.TO": 0.10, "JNJ": 0.10}
MESSAGES = {
    "robert": ("Je pense mettre 20 % dans Nvidia, tout le monde en parle.",
               "risk_review", ROBERT_PORTFOLIO),
    "amira": ("Should I go all-in on tech and crypto? I'm 26 and can take risk.",
              "risk_review", None),
    "mateo": ("Propose-moi un équilibre croissance/stabilité en évitant les entreprises controversées.",
              "risk_review", {"XIU.TO": 0.5, "XBB.TO": 0.3, "ENB.TO": 0.2}),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", choices=list(MESSAGES), default="robert")
    args = ap.parse_args()

    s = get_settings()
    personas = json.loads(s.abs_path(s.personas_path).read_text(encoding="utf-8"))["personas"]
    persona = next(p for p in personas if p["id"] == args.persona)
    user_message, task, portfolio = MESSAGES[args.persona]

    inp = OrchestratorInput(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        actor_id=f"persona-{persona['id']}",
        language=persona["language"],
        as_of="2026-11-12",
        profile=Profile.model_validate(persona),
        request=AnalysisRequest(
            request_id=f"req-{uuid.uuid4().hex[:6]}", actor_id=f"persona-{persona['id']}",
            task=task, user_message=user_message,
        ),
        portfolio_weights=portfolio,
    )

    out = run_orchestrator(inp)
    print(f"=== {out.run_id} · {out.status} · {out.model} · {out.duration_ms} ms ===")
    print(out.summary)
    print(f"\nPreuves ({len(out.evidence_ids)}) : {', '.join(out.evidence_ids)}")
    if out.error:
        print(f"ERREUR : {out.error}")
    for r in JsonlLedger(s.abs_path(s.runs_dir)).records(out.run_id):
        print(f"  {r['seq']} {r.get('evidence_id', '-')} {r['tool']}")
    return 0 if out.status == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
