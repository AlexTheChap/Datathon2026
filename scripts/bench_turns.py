"""Before/after benchmark of model turns and wall time for one full pipeline run (live Bedrock).

Fixed scenario (the same as data/runs/pipe-*.jsonl traces): persona mateo asks for a
growth/stability balance avoiding major controversies — the plan enables risk + esg + allocation.
The script wraps strands BedrockModel.stream to COUNT and TIME every model invocation
(behavior-neutral: identical instrumentation for the "before" and "after" variants), then runs
③ intake -> ④ context -> ⑤ orchestrator (sub-agents + synthesis) -> explainer and dumps a JSON
summary per run to data/bench/.

Usage:
    .venv/bin/python scripts/bench_turns.py --tag before --runs 2
    .venv/bin/python scripts/bench_turns.py --tag after  --runs 2

AF_GATEWAY_URL is forced empty so both variants use the same in-process tools.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

os.environ["AF_GATEWAY_URL"] = ""   # same tool path for both variants (local functions, deterministic)

from strands.models import BedrockModel  # noqa: E402

# ---- behavior-neutral turn counter over every Bedrock model call -------------------------
MODEL = {"calls": 0, "ms": [], "by_prefix": {}}
_orig_stream = BedrockModel.stream


def _counted_stream(self, *args, **kwargs):
    model_id = "unknown"
    try:
        model_id = (getattr(self, "config", None) or {}).get("model_id") or "unknown"
    except Exception:
        pass

    async def gen():
        t0 = time.perf_counter()
        MODEL["calls"] += 1
        try:
            async for ev in _orig_stream(self, *args, **kwargs):
                yield ev
        finally:
            try:   # instrumentation must never break a run
                ms = int((time.perf_counter() - t0) * 1000)
                MODEL["ms"].append(ms)
                MODEL["by_prefix"][model_id] = MODEL["by_prefix"].get(model_id, 0) + 1
            except Exception:
                pass
    return gen()


BedrockModel.stream = _counted_stream  # type: ignore[method-assign]

from app.agents.explainer import explain  # noqa: E402
from app.agents.orchestrator import run_orchestrator  # noqa: E402
from app.context.handler import resolve_context  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.intake.agent import run_intake  # noqa: E402
from app.intake.backends import BedrockBackend  # noqa: E402
from app.schemas.execution_plan import ExecutionPlan  # noqa: E402
from app.schemas.models import OrchestratorInput  # noqa: E402
from app.schemas.prompt_ref import PromptRef  # noqa: E402
from app.store.ledger import make_ledger  # noqa: E402
from app.tools.context import default_as_of  # noqa: E402

MESSAGE = "Propose a growth/stability balance avoiding companies with major controversies."
WEIGHTS = {"FTS.TO": 0.2, "XBB.TO": 0.5, "RY.TO": 0.3}   # the portfolio of the pipe-* traces
HAIKU_PREFIX, SONNET_PREFIX = "us.anthropic.claude-haiku", "us.anthropic.claude-sonnet"


def persona_form(persona: dict) -> dict:
    h = persona.get("horizon_years", [None, None])
    form = {k: persona.get(k) for k in ("risk_band", "experience", "language", "objective", "loss_capacity")}
    form["horizon_years"] = {"min": h[0], "max": h[1]}
    form.update({k: persona.get(k) for k in ("preferences", "constraints", "exclusions")})
    cap = persona.get("simulated_capital") or {}
    form["capital"] = cap.get("value")
    return {k: v for k, v in form.items() if v is not None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="bench", help="label stored in the summary file (before/after)")
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()

    s = get_settings()
    personas = json.loads(s.abs_path(s.personas_path).read_text(encoding="utf-8"))["personas"]
    persona = next(p for p in personas if p["id"] == "mateo")
    actor_id = "persona-mateo"
    out_dir = REPO / "data" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    for i in range(1, args.runs + 1):
        MODEL.update(calls=0, ms=[], by_prefix={})
        run_id = f"bench-{args.tag}-{i}-{uuid.uuid4().hex[:6]}"
        t_all = time.perf_counter()

        t0 = time.perf_counter()
        intake = run_intake(MESSAGE, actor_id, profile_form=persona_form(persona), backend=BedrockBackend())
        intake_ms = int((time.perf_counter() - t0) * 1000)
        if intake.status != "ready":
            print(f"[{run_id}] intake not ready: {intake.status} — {intake.clarification_question}")
            return 1

        ctx = resolve_context(intake.profile, intake.request)
        plan = ExecutionPlan.model_validate(ctx["execution_plan"])
        enabled = [a.agent for a in plan.enabled_agents]

        inp = OrchestratorInput(
            run_id=run_id, actor_id=actor_id, language=intake.profile.language,
            as_of=intake.request.as_of.isoformat() if intake.request.as_of else default_as_of(),
            profile=intake.profile, request=intake.request, execution_plan=plan, portfolio_weights=WEIGHTS,
        )
        t0 = time.perf_counter()
        out = run_orchestrator(inp)
        orch_ms = int((time.perf_counter() - t0) * 1000)
        if out.error:
            print(f"[{run_id}] orchestrator ERROR: {out.error}")
            return 1

        run = type("R", (), {"profile": intake.profile, "request": intake.request, "execution_plan": plan,
                             "prompt_ref": PromptRef(**ctx["prompt_ref"])})()
        t0 = time.perf_counter()
        answer = explain(run, out.model_dump(mode="json"), make_ledger(s).records(run_id))
        explain_ms = int((time.perf_counter() - t0) * 1000)

        # per-agent tool calls from the run ledger
        recs = make_ledger(s).records(run_id)
        agents: dict[str, dict] = {}
        for r in recs:
            a = r.get("agent")
            if not a or r.get("duration_ms") is None:
                continue
            d = agents.setdefault(a, {"tool_calls": 0, "tool_ms": 0})
            d["tool_calls"] += 1
            d["tool_ms"] += r.get("duration_ms") or 0

        total_ms = int((time.perf_counter() - t_all) * 1000)
        haiku_calls = sum(v for k, v in MODEL["by_prefix"].items() if k.startswith(HAIKU_PREFIX))
        sonnet_calls = sum(v for k, v in MODEL["by_prefix"].items() if k.startswith(SONNET_PREFIX))
        summary = {
            "tag": args.tag, "run": i, "run_id": run_id, "enabled_agents": enabled,
            "model_calls_total": MODEL["calls"], "haiku_calls": haiku_calls, "sonnet_calls": sonnet_calls,
            "model_ms_total": sum(MODEL["ms"]), "model_ms_max": max(MODEL["ms"] or [0]),
            "wall_ms": {"intake": intake_ms, "orchestrator": orch_ms, "explainer": explain_ms, "total": total_ms},
            "agents_tool_calls": agents,
            "answer_kind": answer.kind,
        }
        summaries.append(summary)
        print(json.dumps(summary, indent=1))
        print(f"[{run_id}] done: {haiku_calls} Haiku + {sonnet_calls} Sonnet calls, "
              f"{sum(MODEL['ms'])/1000:.1f}s model time, {total_ms/1000:.1f}s wall\n")

    out = out_dir / f"bench_{args.tag}.json"
    out.write_text(json.dumps(summaries, indent=1), encoding="utf-8")
    print(f"summary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
