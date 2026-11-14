"""Création unique de la ressource AgentCore Memory (L17) et impression du memory_id.

Stratégies : résumé de session (mémoire court terme) + préférences par utilisateur
(mémoire longue durée des « soft preferences »). Exporter ensuite :
    export AF_MEMORY_ID=<id imprimé>
"""
from __future__ import annotations

import sys

from app.core.config import get_settings

STRATEGIES = [
    {"summaryMemoryStrategy": {
        "name": "ResumeSession",
        "namespaces": ["/summaries/{actorId}/{sessionId}/"],
    }},
    {"userPreferenceMemoryStrategy": {
        "name": "PreferencesUsager",
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
        print(f"Réponse inattendue de create_memory_and_wait : {memory!r}", file=sys.stderr)
        return 1
    print(memory_id)
    print(f"# export AF_MEMORY_ID={memory_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
