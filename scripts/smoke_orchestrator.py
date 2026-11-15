"""End-to-end orchestrator smoke test: the "Robert" scenario from the brief.

"I'm thinking of putting 20% in Nvidia, everyone is talking about it." —
Robert, conservative profile (low_moderate), 3-5 year horizon. The orchestrator
must call the tools (governance, stress), identify the conflict with the profile
and challenge the request instead of agreeing. Requires AWS credentials (Bedrock).

Usage: .venv/bin/python scripts/smoke_orchestrator.py [--persona amira|mateo|robert]
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
from app.schemas.models import AnalysisRequest, OrchestratorInput, profile_from_persona  # noqa: E402
from app.store.ledger import JsonlLedger  # noqa: E402

MESSAGES = {
    "robert": ("I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
               "risk_review"),
    "amira": ("Should I go all-in on tech and crypto? I'm 26 and can take risk.",
              "risk_review"),
    "mateo": ("Propose a growth/stability balance avoiding companies with major controversies.",
              "allocation_proposal"),
}


def _portfolio(persona: dict) -> dict[str, float] | None:
    demo = persona.get("demo_request") or {}
    pf = demo.get("simulated_current_portfolio")
    return {k: float(v) for k, v in pf.items()} if pf else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", choices=list(MESSAGES), default="robert")
    args = ap.parse_args()

    s = get_settings()
    personas = json.loads(s.abs_path(s.personas_path).read_text(encoding="utf-8"))["personas"]
    persona = next(p for p in personas if p["id"] == args.persona)
    user_message, task = MESSAGES[args.persona]
    profile = profile_from_persona(persona)

    inp = OrchestratorInput(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        actor_id=profile.actor_id,
        language=profile.language,
        as_of="2026-11-12",
        profile=profile,
        request=AnalysisRequest(
            request_id=f"req-{uuid.uuid4().hex[:6]}", actor_id=profile.actor_id,
            task=task, user_message=user_message, intake_confidence=0.95,
        ),
        portfolio_weights=_portfolio(persona),
    )

    out = run_orchestrator(inp)
    print(f"=== {out.run_id} · {out.status} · {out.model} · {out.duration_ms} ms ===")
    print(out.summary)
    print(f"\nEvidence ({len(out.evidence_ids)}): {', '.join(out.evidence_ids)}")
    if out.error:
        print(f"ERROR: {out.error}")
    for r in JsonlLedger(s.abs_path(s.runs_dir)).records(out.run_id):
        print(f"  {r['seq']} {r.get('evidence_id', '-')} {r['tool']}")
    return 0 if out.status == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
