"""Chemin Bedrock de l'Intake, sans AWS : le vrai SDK `anthropic` parle à un faux serveur HTTP.

Vérifie que la requête envoyée contient le schéma JSON (structured outputs) et le bon modèle, et que la
réponse est parsée en `IntakeDraft` puis passe par `run_intake`. Ignoré si `anthropic` n'est pas installé.
"""
import json

import pytest

anthropic = pytest.importorskip("anthropic")
try:  # le SDK anthropic 2.x utilise httpx2 ; les versions plus anciennes, httpx
    import httpx2 as httpx
except ImportError:
    httpx = pytest.importorskip("httpx")

from app.intake import BedrockBackend, run_intake  # noqa: E402

MODEL_JSON = {
    "profile": {
        "risk_band": "low_moderate", "horizon_min_years": 3, "horizon_max_years": 5, "experience": "advanced",
        "language": "en", "objective": "Préserver son capital", "preferences": ["entreprises établies"],
        "constraints": [], "exclusions": [{"type": "asset_class", "value": "crypto", "min_severity": None}],
        "loss_capacity": None, "capital": 400000,
    },
    "request": {
        "task": "allocation_proposal", "positions": [{"ticker": "NVIDIA", "weight": 0.2}], "capital": None,
        "scenarios": [], "benchmark": None, "stress_tests": False, "include_rag": True, "as_of": None,
        "intake_confidence": 0.93,
    },
    "clarification_question": None,
}


def fake_client(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": "anthropic.claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps(MODEL_JSON)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    return anthropic.Anthropic(api_key="test", base_url="http://fake.local",
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_bedrock_backend_end_to_end():
    captured: list = []
    backend = BedrockBackend(client=fake_client(captured))
    res = run_intake("Je pense mettre 20 % dans Nvidia, tout le monde en parle.", "robert", backend=backend)

    body = captured[0]
    assert body["model"] == "us.anthropic.claude-haiku-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "NVDA" in body["system"]  # l'univers d'actifs est injecté dans le prompt
    assert "<user_message>" in body["messages"][0]["content"]

    assert res.status == "ready" and res.backend == "bedrock"
    assert res.profile.risk_band == "low_moderate"
    assert res.request.positions[0].ticker == "NVDA"  # « NVIDIA » normalisé par le code
