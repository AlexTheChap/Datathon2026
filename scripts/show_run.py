"""Pretty-print one run's pipeline from the evidence ledger: order, caller, tool,
inputs, key outputs — the verifiable trace behind an agent answer.

Usage:
    .venv/bin/python scripts/show_run.py                 # latest run
    .venv/bin/python scripts/show_run.py smoke-b8c85dc1  # a specific run_id
    .venv/bin/python scripts/show_run.py --full          # untruncated outputs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.store.ledger import make_ledger  # noqa: E402


def _finding(payload) -> dict | None:
    """The sub-agent finding inside a ToolResult-shaped payload."""
    if isinstance(payload, list):
        return next((b for b in payload if isinstance(b, dict) and "headline" in b), None)
    return payload if isinstance(payload, dict) and "headline" in payload else None


def _summarize(r: dict, full: bool) -> str:
    """One-line summary of a record's output, per tool type."""
    p = r.get("payload")
    inner = _finding(p)
    if inner:  # sub-agent finding
        return f"verdict={inner.get('severity')} | {inner.get('headline', '')}"
    if isinstance(p, list):  # ToolResult shape: [block...] -> first dict block
        p = next((b for b in p if isinstance(b, dict)), {})
    if not isinstance(p, dict):
        return ""
    if p.get("error"):
        return f"ERROR: {p['error']}"
    out = p.get("output") or p  # quant envelopes nest under "output"; governance doesn't
    if "verdict" in out and "checks" in out:
        flags = [f"{c['id']}:{c['status']}" for c in out.get("checks", []) if c.get("status") != "PASS"]
        return f"verdict={out['verdict']}" + (f" | flags: {', '.join(flags)}" if flags else " | all PASS")
    if "portfolio" in out and "max_drawdown" in out["portfolio"]:
        pf = out["portfolio"]
        return f"return={pf.get('total_return'):.1%} worst={pf.get('max_drawdown'):.1%}" + \
               (f" loss_on_capital={out.get('loss_at_trough'):,.0f}$" if out.get("loss_at_trough") else "")
    if out and all(isinstance(v, dict) and "ann_vol" in v for v in out.values()):
        t, m = next(iter(out.items()))
        return f"{t}: vol={m['ann_vol']:.0%} worst={m['max_drawdown']:.1%}"
    if "hhi" in out:
        return f"top={next(iter(out.get('by_position', {})), '?')} eff_positions={out.get('effective_positions')}"
    if "matrix" in out:
        return "correlation matrix"
    if "risk_contribution" in out:
        return f"diversification_ratio={out.get('diversification_ratio')}"
    if "max_weight" in out:
        return f"max_weight({out.get('asset')})={out.get('max_weight'):.0%} (limit {out.get('limit'):.0%})"
    if "membership" in out:
        return f"unverified={out.get('unverified_only')}"
    if "date" in out:
        return f"macro@{out['date']}: " + json.dumps({k: v for k, v in out.items() if k != "date"})[:90]
    if out and all(isinstance(v, dict) and "empirical_duration" in v for v in out.values()):
        return json.dumps(out)[:90]
    return json.dumps(out)[:90] if out else json.dumps(p.get("say", ""))[:90]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", help="run_id; default = most recent run")
    ap.add_argument("--full", action="store_true", help="print full JSON outputs")
    ap.add_argument("--json", action="store_true", help="dump raw records as JSON")
    args = ap.parse_args()

    s = get_settings()
    led = make_ledger(s)
    run_id = args.run_id
    if run_id is None:
        files = sorted(s.abs_path(s.runs_dir).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            print("No runs recorded in data/runs/.")
            return 1
        run_id = files[-1].stem
    recs = led.records(run_id)
    if not recs:
        print(f"No records for run '{run_id}'.")
        return 1

    print(f"Run {run_id} — {len(recs)} calls, started {recs[0]['ts']}\n")
    print(f"{'seq':6} {'caller':18} {'tool':20} output")
    print("-" * 100)
    for r in recs:
        caller = r.get("agent") or "orchestrator"
        line = f"{r['seq']:6} {caller:18} {r['tool']:20} {_summarize(r, args.full)}"
        print(line)
        if args.full:
            print("      input:", json.dumps(r.get("tool_input"), default=str)[:200])
            print("      output:", json.dumps(r.get("payload"), default=str)[:600])
    ids = led.evidence_ids(run_id)
    print(f"\nUnique evidence ({len(ids)}): {', '.join(ids)}")
    print("Verification rule: every number in the agent's summary must cite one of these ids.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
