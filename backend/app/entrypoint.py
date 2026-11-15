"""Node ⑤/L6 — Amazon Bedrock AgentCore Runtime entrypoint.

The API ("dispatcher" Lambda) calls InvokeAgentRuntime with the
OrchestratorInput payload; the Runtime executes this handler. Locally:
`python -m app.entrypoint` serves the same contract on http://127.0.0.1:8080
(equivalent to `agentcore start`).
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
        # run_orchestrator is synchronous (blocking Bedrock calls) -> dedicated thread
        out = await asyncio.to_thread(orchestrator.run_orchestrator, inp)
        yield {"type": "run.completed", "result": out.model_dump()}
    except Exception as e:  # invalid payload, etc.: explicit failure
        yield {"type": "run.failed", "run_id": request.get("run_id"),
               "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    app.run()
