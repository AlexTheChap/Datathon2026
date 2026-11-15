"""One-time creation of the AgentCore Memory resource (L17); prints the memory_id.

Strategies: session summary (short-term memory) + per-user preferences
(long-term memory of "soft preferences"). Then export:
    export AF_MEMORY_ID=<printed id>
"""
from __future__ import annotations

import sys

from app.core.config import get_settings

STRATEGIES = [
    {"summaryMemoryStrategy": {
        "name": "SessionSummarizer",
        "namespaces": ["/summaries/{actorId}/{sessionId}/"],
    }},
    {"userPreferenceMemoryStrategy": {
        "name": "PreferenceLearner",
        "namespaces": ["/preferences/{actorId}/"],
    }},
]


def main() -> int:
    from bedrock_agentcore.memory import MemoryClient

    s = get_settings()
    client = MemoryClient(region_name=s.aws_region)
    memory = client.create_memory_and_wait(name="alphaforge", strategies=STRATEGIES)
    memory_id = getattr(memory, "id", None) or getattr(memory, "memory_id", None)
    if not memory_id:
        print(f"Unexpected response from create_memory_and_wait: {memory!r}", file=sys.stderr)
        return 1
    print(memory_id)
    print(f"# export AF_MEMORY_ID={memory_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
