"""Nœud ⑤/L6 — point d'entrée Amazon Bedrock AgentCore Runtime.

L'API (Lambda « dispatcher ») appelle InvokeAgentRuntime avec le payload
OrchestratorInput ; le Runtime exécute ce handler. Local : `python -m app.entrypoint`
sert le même contrat sur http://127.0.0.1:8080 (équivalent `agentcore start`).
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from bedrock_agentcore import BedrockAgentCoreApp

from app.agents import orchestrator
from app.schemas.models import OrchestratorInput

app = BedrockAgentCoreApp()


@app.entrypoint
async def handler(request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "run.started", "run_id": request.get("run_id")}
    try:
        inp = OrchestratorInput.model_validate(request)
        # run_orchestrator est synchrone (appels Bedrock bloquants) -> thread dédié
        out = await asyncio.to_thread(orchestrator.run_orchestrator, inp)
        yield {"type": "run.completed", "result": out.model_dump()}
    except Exception as e:  # payload invalide, etc. : défaillance explicite
        yield {"type": "run.failed", "run_id": request.get("run_id"),
               "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    app.run()
