"""Full pipeline smoke test, boxes ③→④→⑤→⑥ of docs/architecture.mmd, LIVE on Bedrock:

  ③ Intake      run_intake(text)              — Bedrock Haiku: free text → Profile + Request JSON
  ④ Context     resolve_context(p, r)         — deterministic: ExecutionPlan + prompt_ref
  ⑤ Orchestrator run_orchestrator(inp)         — plan-driven sub-agents (Claude Sonnet + Haiku)
  ⑥ Tools       the sub-agents' toolsets      — deterministic pandas over data/curated
  ⑤ Explainer   explain(run, out, ledger)     — Claude Sonnet: the checked answer for this reader

Persona test: Robert (conservative) says "I'm thinking of putting 20% in Nvidia…".
The profile comes from the persona file (as if created via the profile form).

Usage: .venv/bin/python scripts/smoke_pipeline.py [--persona robert|amira|mateo]
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.context.handler import resolve_context  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.intake.agent import run_intake  # noqa: E402
from app.intake.backends import BedrockBackend  # noqa: E402
from app.schemas.execution_plan import ExecutionPlan  # noqa: E402
from app.schemas.models import OrchestratorInput  # noqa: E402
from app.agents.orchestrator import run_orchestrator  # noqa: E402
from app.store.ledger import make_ledger  # noqa: E402
from app.tools.context import default_as_of  # noqa: E402

MESSAGES = {
    "robert": "I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
    "amira": "Should I go all-in on tech and crypto? I'm 26 and can take risk.",
    "mateo": "Propose a growth/stability balance avoiding companies with major controversies.",
}


def persona_form(persona: dict) -> dict:
    """The persona file as the profile-form payload (the explicit source that wins in the merge)."""
    h = persona.get("horizon_years", [None, None])
    form = {k: persona.get(k) for k in
            ("risk_band", "experience", "language", "objective", "loss_capacity")}
    form["horizon_years"] = {"min": h[0], "max": h[1]}
    form.update({k: persona.get(k) for k in ("preferences", "constraints", "exclusions")})
    cap = persona.get("simulated_capital") or {}
    form["capital"] = cap.get("value")
    return {k: v for k, v in form.items() if v is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", choices=list(MESSAGES), default="robert")
    ap.add_argument("--text", help="your own question (uses --persona's profile); "
                                   "e.g. --text \"Should I put 30%% in bitcoin?\"")
    args = ap.parse_args()
    message = args.text or MESSAGES[args.persona]

    s = get_settings()
    personas = json.loads(s.abs_path(s.personas_path).read_text(encoding="utf-8"))["personas"]
    persona = next(p for p in personas if p["id"] == args.persona)
    actor_id = f"persona-{persona['id']}"

    # ---- ③ Intake: free text -> validated Profile + Request (Bedrock Haiku) ---------------
    intake = run_intake(message, actor_id, profile_form=persona_form(persona),
                        backend=BedrockBackend())
    print(f"[③ Intake] status={intake.status} backend={intake.backend}")
    for w in intake.warnings:
        print(f"  warning: {w}")
    if intake.status != "ready":
        print(f"  clarification: {intake.clarification_question} missing={intake.missing_fields}")
        return 1
    print(f"  request: task={intake.request.task} as_of={intake.request.as_of}")

    # ---- ④ Context: ExecutionPlan + prompt_ref (deterministic, no LLM) ---------------------
    ctx = resolve_context(intake.profile, intake.request)
    plan = ExecutionPlan.model_validate(ctx["execution_plan"])
    print(f"[④ Context] status={ctx['status']} · prompt_ref={ctx['prompt_ref']['key']}")
    for a in plan.enabled_agents:
        print(f"  enabled: {a.agent} — {a.reason}")
    if plan.needs_clarification:
        print("  plan flag: needs_clarification (the orchestrator will state its assumptions)")

    # ---- ⑤⑥ Orchestrator + sub-agents + tools (Sonnet + Haiku, live) ----------------------
    demo = persona.get("demo_request") or {}
    pf = demo.get("simulated_current_portfolio")
    weights = {k: float(v) for k, v in pf.items()} if pf else None

    inp = OrchestratorInput(
        run_id=f"pipe-{uuid.uuid4().hex[:8]}",
        actor_id=actor_id,
        language=intake.profile.language,
        as_of=intake.request.as_of.isoformat() if intake.request.as_of else default_as_of(),
        profile=intake.profile,
        request=intake.request,
        execution_plan=plan,
        portfolio_weights=weights,
    )
    out = run_orchestrator(inp)
    print(f"[⑤⑥ Orchestrator] {out.run_id} · {out.status} · {out.model} · {out.duration_ms} ms")
    if out.error:
        print(f"  ERROR: {out.error}")
        return 1
    # ---- ⑤ Explainer: the answer the user reads, every claim checked (Sonnet, live) ---------
    from types import SimpleNamespace

    from app.agents.explainer import explain
    from app.schemas.prompt_ref import PromptRef

    run = SimpleNamespace(profile=intake.profile, request=intake.request, execution_plan=plan,
                          prompt_ref=PromptRef(**ctx["prompt_ref"]))
    answer = explain(run, out.model_dump(mode="json"), make_ledger(get_settings()).records(out.run_id))
    v = answer.verification
    print(f"\n[⑤ Explainer] {answer.kind} · template {answer.prompt.key if answer.prompt else '-'} · "
          f"{answer.duration_ms} ms")
    print("\n--- answer ---")
    print(answer.short_answer)
    for c in answer.checks:
        print(f"  [{c.verdict}] {c.allocation}: {c.text}  {c.evidence}")
    for pt in answer.key_points:
        print(f"  - {pt.text}  {pt.evidence}")
    for g in answer.glossary:
        print(f"  * {g.term}: {g.meaning}")
    if v:
        print(f"\n[claim check] {v.claims_checked} claims · retried={v.retried} · removed={len(v.removed)}")
        for r in v.removed:
            print(f"  removed ({r.section}): {r.reason}")
    if answer.note:
        print(f"  note: {answer.note}")
    print(f"\nEvidence ({len(out.evidence_ids)}): {', '.join(out.evidence_ids)}")
    recs = make_ledger(s).records(out.run_id)
    print(f"\nTrace: .venv/bin/python scripts/show_run.py {out.run_id}")
    for r in recs:
        caller = r.get("agent") or "orchestrator"
        ms = r.get("duration_ms")
        print(f"  {r['seq']} [{caller}] {r['tool']}" + (f" ({ms} ms)" if ms is not None else ""))
    by_caller: dict[str, list[int]] = {}
    for r in recs:
        if r.get("duration_ms") is not None:
            by_caller.setdefault(r.get("agent") or "orchestrator", []).append(r["duration_ms"])
    if by_caller:
        print("\nWall time per caller (ms; fork callers run in parallel — the slowest sets the pace):")
        for caller, mss in sorted(by_caller.items(), key=lambda kv: -sum(kv[1])):
            print(f"  {caller:22} calls={len(mss):2}  total={sum(mss):6}  max={max(mss):6}")
    return 0 if getattr(v, "ok", True) else 1     # an answer with an unknown citation is a failed smoke test


if __name__ == "__main__":
    raise SystemExit(main())
