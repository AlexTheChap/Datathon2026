"""Intake Bedrock path, without AWS: the real `anthropic` SDK talks to a fake HTTP server.

Checks that the request sent carries the JSON schema (structured outputs) and the right model, and that the
response is parsed into an `IntakeDraft`, then goes through `run_intake`. Skipped if `anthropic` is not installed.
"""
import json

import pytest

anthropic = pytest.importorskip("anthropic")
try:  # the anthropic 2.x SDK uses httpx2; older versions use httpx
    import httpx2 as httpx
except ImportError:
    httpx = pytest.importorskip("httpx")

from app.intake import BedrockBackend, run_intake  # noqa: E402

MODEL_JSON = {
    "profile": {
        "risk_band": "low_moderate", "horizon_min_years": 3, "horizon_max_years": 5, "experience": "advanced",
        "language": "en", "objective": "Preserve capital", "preferences": ["established companies"],
        "constraints": [], "exclusions": [{"type": "asset_class", "value": "crypto", "min_severity": None}],
        "loss_capacity": None, "capital": 400000, "learning": "unsaid",
    },
    "request": {
        "task": "allocation_proposal", "positions": [{"ticker": "NVIDIA", "weight": 0.2}], "capital": None, "benchmark": None, "include_rag": True, "as_of": None,
        "intake_confidence": 0.93,
    },
    "clarification_question": None,
}


def fake_client(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "us.anthropic.claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps(MODEL_JSON)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    return anthropic.Anthropic(api_key="test", base_url="http://fake.local",
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_bedrock_backend_end_to_end():
    captured: list = []
    backend = BedrockBackend(client=fake_client(captured))
    res = run_intake("I'm thinking of putting 20% in Nvidia, everyone is talking about it.", "robert", backend=backend)

    body = captured[0]
    assert body["model"] == "us.anthropic.claude-haiku-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "NVDA" in body["system"]  # the asset universe is injected into the prompt
    assert "<user_message>" in body["messages"][0]["content"]

    assert res.status == "ready" and res.backend == "bedrock"
    assert res.profile.risk_band == "low_moderate"
    assert res.request.positions[0].ticker == "NVDA"  # "NVIDIA" normalised by the code
