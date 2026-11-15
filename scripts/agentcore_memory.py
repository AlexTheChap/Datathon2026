"""AgentCore Memory (link L17): create, inspect and delete the memory resource of Fitfolio.

  .venv/bin/python scripts/agentcore_memory.py create [--write-env]   # idempotent: reuses an existing one
  .venv/bin/python scripts/agentcore_memory.py status
  .venv/bin/python scripts/agentcore_memory.py events --actor persona-robert --session sess-abc   # short term
  .venv/bin/python scripts/agentcore_memory.py preferences --actor persona-robert [--query "..."]   # long term
  .venv/bin/python scripts/agentcore_memory.py delete --yes

The resource has the 90-day event store (short term, per session) and one long-term strategy,
`userPreferenceMemoryStrategy`, that extracts the investor's SOFT PREFERENCES into `/preferences/{actorId}/`
(see backend/app/agents/memory.py). The orchestrator uses it when AF_MEMORY_ID is set (--write-env puts it in .env).
Long-term records appear a minute or more after the conversation: extraction is asynchronous.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agents.memory import MEMORY_NAME, STRATEGIES, preferences_namespace, safe_id
from app.core.config import REPO_ROOT, get_settings
from bedrock_agentcore.memory import MemoryClient


def find_memory(client: MemoryClient) -> dict | None:
    """The Fitfolio memory of this account (ids look like `FitfolioMemory-xxxxxxxxxx`)."""
    return next((m for m in client.list_memories() if str(m.get("id", "")).startswith(f"{MEMORY_NAME}-")), None)


def write_env(memory_id: str) -> Path:
    path = REPO_ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"AF_MEMORY_ID={memory_id}"
    if re.search(r"^AF_MEMORY_ID=.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^AF_MEMORY_ID=.*$", line, text, flags=re.MULTILINE)
    else:
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def memory_id_arg(args, settings) -> str:
    mid = args.memory_id or settings.memory_id
    if not mid:
        raise SystemExit("No memory id: run `create --write-env`, set AF_MEMORY_ID or pass --memory-id.")
    return mid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["create", "status", "events", "preferences", "delete"])
    ap.add_argument("--memory-id")
    ap.add_argument("--actor")
    ap.add_argument("--session")
    ap.add_argument("--query", default="investment preferences")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--write-env", action="store_true", help="create: write AF_MEMORY_ID into .env")
    ap.add_argument("--yes", action="store_true", help="delete: confirm")
    args = ap.parse_args()

    s = get_settings()
    client = MemoryClient(region_name=s.aws_region)

    if args.command == "create":
        existing = find_memory(client)
        if existing:
            mid = existing["id"]
            print(f"Memory already exists: {mid} ({existing.get('status')})")
        else:
            mem = client.create_memory_and_wait(
                name=MEMORY_NAME, strategies=STRATEGIES, event_expiry_days=90,
                description="Fitfolio: session history (short term) and soft investment preferences (long term).")
            mid = mem["id"]
            print(f"Memory created: {mid} ({mem.get('status')})")
        if args.write_env:
            print(f"AF_MEMORY_ID written to {write_env(mid)}")
        else:
            print(f"Use it with: export AF_MEMORY_ID={mid}   (or re-run with --write-env)")
        return 0

    mid = memory_id_arg(args, s)
    if args.command == "status":
        mem = client.gmcp_client.get_memory(memoryId=mid)["memory"]
        print(json.dumps({k: mem.get(k) for k in ("id", "name", "status", "eventExpiryDuration")}, default=str))
        for st in client.get_memory_strategies(mid):
            print(f"  strategy {st.get('name')} ({st.get('type') or st.get('memoryStrategyType')}) "
                  f"{st.get('status')} namespaces={st.get('namespaces')}")
        return 0
    if args.command == "events":
        if not (args.actor and args.session):
            raise SystemExit("events needs --actor and --session")
        events = client.list_events(mid, safe_id(args.actor), safe_id(args.session), include_payload=True)
        print(f"{len(events)} event(s) in session {args.session} of actor {safe_id(args.actor)}")
        for e in events:
            for p in e.get("payload", []):
                conv = p.get("conversational")
                if conv:
                    print(f"  [{conv.get('role')}] {str(conv.get('content', {}).get('text', ''))[:140]!r}")
        return 0
    if args.command == "preferences":
        if not args.actor:
            raise SystemExit("preferences needs --actor")
        ns = preferences_namespace(args.actor)
        records = client.retrieve_memories(mid, namespace=ns, query=args.query, top_k=args.top_k)
        print(f"{len(records)} soft preference record(s) in {ns}")
        for r in records:
            print(f"  score={r.get('score')} {str(r.get('content', {}).get('text', r.get('content')))[:200]}")
        return 0
    if args.command == "delete":
        if not args.yes:
            raise SystemExit("Refusing to delete without --yes (this removes every stored session and preference).")
        client.delete_memory_and_wait(mid)
        print(f"Deleted {mid}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
