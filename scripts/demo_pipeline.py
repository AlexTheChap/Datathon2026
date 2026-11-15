"""End-to-end demo of boxes ③ Intake + ④ Context resolution:
free text → Profile/Request JSON → context Lambda handler → ExecutionPlan + PromptRef.

    python scripts/demo_pipeline.py                  # persona scenarios, mock intake (offline)
    python scripts/demo_pipeline.py --mode bedrock   # real Claude Haiku on Bedrock (AWS required)
    python scripts/demo_pipeline.py --text "..."     # your own text
    python scripts/demo_pipeline.py --json           # also dump the full JSON passed between boxes

Simulation only: nothing here is financial advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.context import handler  # noqa: E402
from app.intake import get_backend, run_intake  # noqa: E402
from app.store.profiles import InMemoryProfileStore  # noqa: E402

from demo_intake import SCENARIOS  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "bedrock"], default="mock")
    ap.add_argument("--text")
    ap.add_argument("--actor", default="demo")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mode == "bedrock":
        from deploy_context_lambda import load_dotenv  # AWS_* from the git-ignored .env, never overrides

        load_dotenv()
        try:  # Windows: let Python see the system certificates
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass

    backend = get_backend(args.mode)
    store = InMemoryProfileStore()
    for actor, text in ([(args.actor, args.text)] if args.text else SCENARIOS):
        print("=" * 100)
        print(f"[{actor}] \"{text}\"")
        res = run_intake(text, actor, backend=backend, store=store)
        print(f"③ intake  -> status={res.status}  backend={res.backend}")
        for w in res.warnings:
            print(f"            warning: {w}")
        if res.profile is None or res.request is None:
            print(f"            question: {res.clarification_question}  (④ not called)")
            continue
        p, r = res.profile, res.request
        print(f"            profile: risk={p.risk_band} horizon={p.horizon_years.model_dump()} "
              f"experience={p.experience} capital={p.capital}")
        print(f"            request: task={r.task} confidence={r.intake_confidence}")

        # Same payload shape the context Lambda receives (L4, L5).
        event = {"profile": p.model_dump(mode="json"), "request": r.model_dump(mode="json")}
        out = handler(json.loads(json.dumps(event)))
        plan, ref = out["execution_plan"], out["prompt_ref"]
        print("④ context -> enabled agents:")
        for a in plan["enabled_agents"]:
            forced = " (forced)" if a["forced"] else ""
            print(f"              + {a['agent']:<16}{forced} {a['reason']}")
        print(f"            disabled: {', '.join(a['agent'] for a in plan['disabled_agents'])}")
        print(f"            governance={plan['run_governance']} explainer={plan['run_explainer']} "
              f"needs_clarification={plan['needs_clarification']}")
        for w in plan["warnings"]:
            print(f"            warning: {w}")
        print(f"            prompt_ref: {ref['key']} v{ref['version']} sha256={ref['sha256'][:12]}…")
        if args.json:
            print(json.dumps({"intake": event, "context": out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
