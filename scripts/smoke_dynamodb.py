"""End-to-end run with DynamoDB as the store (profiles · runs · evidence ledger), LIVE on AWS.

Same path as POST /v1/runs (box ② ApiService), with AF_STORE=dynamodb:
  PUT profile  → DynamoDB ACTOR#<id> / PROFILE#current                     (L3)
  POST message → Intake (Bedrock) → plan (box ④) → RUN#<id> / META QUEUED  (L6)
  InlineDispatcher → orchestrator + sub-agents → tools via the AgentCore Gateway (S3 curated Parquet) and the
  Knowledge Base; every tool call → RUN#<id> / EVID#<seq>#<evidence_id>   (L12-L15)
  run written back DONE + result                                           (L18)
Then everything is read back from DynamoDB.

Needs: scripts/deploy_data_layer.py (table), deploy_quant_gateway.py, deploy_news_kb.py, AWS credentials.
Usage: python scripts/smoke_dynamodb.py [--persona robert|amira|mateo]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["AF_STORE"] = "dynamodb"          # before app.core.config is imported (cached settings)
os.environ.setdefault("AF_INTAKE_MODE", "bedrock")

try:  # Windows: system certificates for botocore
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from smoke_pipeline import MESSAGES, persona_form  # noqa: E402

from app.api.dispatch import InlineDispatcher  # noqa: E402
from app.api.service import ApiService, MessageIn  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.store.ledger import make_ledger  # noqa: E402
from app.store.profiles import DynamoDBProfileStore  # noqa: E402
from app.store.runs import DynamoDBRunStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", choices=list(MESSAGES), default="robert")
    ap.add_argument("--text", help="question to ask (default: the persona's demo message)")
    args = ap.parse_args()

    import boto3

    s = get_settings()
    table = boto3.resource("dynamodb", region_name=s.aws_region).Table(s.table)
    personas = json.loads(s.abs_path(s.personas_path).read_text(encoding="utf-8"))["personas"]
    persona = next(p for p in personas if p["id"] == args.persona)
    pf = (persona.get("demo_request") or {}).get("simulated_current_portfolio")
    weights = {k: float(v) for k, v in pf.items()} if pf else None

    runs = DynamoDBRunStore(table)
    svc = ApiService(profiles=DynamoDBProfileStore(table), runs=runs,
                     dispatcher=InlineDispatcher(runs, portfolio_weights=weights))
    actor = f"demo-{persona['id']}"
    print(f"• store={s.store} table={s.table} gateway={'on' if s.gateway_url else 'off'} kb={s.kb_id or 'off'}")

    profile = svc.put_profile(actor, persona_form(persona))
    print(f"[L3 profile]  PUT  ACTOR#{actor} / PROFILE#current  risk={profile.risk_band}")

    status, out = svc.post_message(actor, MessageIn(text=args.text or MESSAGES[args.persona]))
    print(f"[L6 run]      POST -> HTTP {status} kind={out.kind} run_id={out.run_id} agents={out.enabled_agents}")
    if out.kind != "run":
        print(out.model_dump_json(indent=2))
        return 1

    # ---- read everything back from DynamoDB ----
    print("\n=== Read back from DynamoDB ===")
    item = table.get_item(Key={"PK": f"ACTOR#{actor}", "SK": "PROFILE#current"})["Item"]
    print(f"ACTOR#{actor} / PROFILE#current : {len(item['data'])} bytes of profile JSON")
    run = svc.get_run(actor, out.run_id)
    res = run.result or {}
    print(f"RUN#{run.run_id} / META        : status={run.status}  updated={run.updated_at}  "
          f"duration={res.get('duration_ms')} ms  evidence={len(res.get('evidence_ids', []))}")
    if run.error:
        print(f"  error: {run.error}")
    led = make_ledger(s)
    records = led.records(run.run_id)
    print(f"RUN#{run.run_id} / EVID#…      : {len(records)} tool calls")
    for r in records:
        print(f"  {r['seq']}  {r.get('agent') or 'orchestrator':<20} {r['tool']:<28} {r.get('evidence_id') or '-'}")
    print("\n--- answer (first lines) ---")
    print("\n".join((res.get("summary") or "").splitlines()[:12]))
    return 0 if run.status == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
