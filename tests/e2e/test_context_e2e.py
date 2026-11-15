"""End-to-end test of ④ Context resolution, as it will run on AWS, without an AWS account:

  ③ Intake (raw JSON, as it comes out of Bedrock)
    → context Lambda: handler(event) with AF_PROMPT_SOURCE=s3                       (L4, L5)
    → dispatcher: InvokeAgentRuntime JSON payload {profile, request, plan, prompt_ref}   (L6, L7)
    → AgentCore Runtime: json.loads(payload) → load_prompt(prompt_ref) from S3

S3 is simulated by a fake client filled with the repo templates (like the Git → S3 publication).
Run: python -m pytest -q tests/e2e
"""
import io
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.context import PromptVersionMismatch, handler, load_prompt
from app.context.prompt_loader import PROMPTS_DIR, S3PromptSource
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef

BUCKET, PREFIX = "alphaforge-prompts", "prompts/"


class FakeS3:
    class NoSuchKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def publish_repo_prompts(self, root: Path = PROMPTS_DIR):
        """Equivalent of `aws s3 sync backend/app/prompts s3://bucket/prompts/`."""
        for f in root.rglob("*.md"):
            self.objects[PREFIX + f.relative_to(root).as_posix()] = f.read_bytes()

    def get_object(self, Bucket, Key):
        assert Bucket == BUCKET
        if Key not in self.objects:
            raise self.NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    fake.publish_repo_prompts()

    class FakeBoto3:
        @staticmethod
        def client(name):
            assert name == "s3"
            return fake

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
    monkeypatch.setenv("AF_PROMPT_SOURCE", "s3")
    monkeypatch.setenv("AF_PROMPT_BUCKET", BUCKET)
    monkeypatch.setenv("AF_PROMPT_PREFIX", PREFIX)
    return fake


# JSON as the Intake would produce it for the three personas of the brief.
INTAKE = {
    "amira": (
        {"actor_id": "u-amira", "risk_band": "high", "horizon_years": {"min": 15, "max": None},
         "experience": "intermediate", "language": "en",
         "preferences": ["artificial intelligence", "crypto assets"], "capital": 50000},
        {"request_id": "req-a1", "actor_id": "u-amira", "task": "allocation_proposal",
         "portfolio_ref": {"portfolio_id": "pf-amira"},
         "user_message": "Build me an aggressive AI + crypto portfolio.",
         "toggles": {"stress_tests": True, "include_rag": True}, "as_of": "2026-11-12",
         "intake_confidence": 0.88},
    ),
    "mateo": (
        {"actor_id": "u-mateo", "risk_band": "moderate", "horizon_years": {"min": 8, "max": 12},
         "experience": "intermediate", "language": "en",
         "preferences": ["important ESG factors"],
         "exclusions": [{"type": "controversy", "min_severity": "major"}],
         "agent_toggles": {"esg": False, "news": False}, "capital": 150000},
        {"request_id": "req-m1", "actor_id": "u-mateo", "task": "allocation_proposal",
         "portfolio_ref": {"portfolio_id": "pf-mateo"},
         "user_message": "Suggest a balanced allocation.",
         "toggles": {"include_rag": True}, "as_of": "2026-11-12", "intake_confidence": 0.81},
    ),
    "robert": (
        {"actor_id": "u-robert", "risk_band": "low_moderate", "horizon_years": {"min": 3, "max": 5},
         "experience": "advanced", "language": "en",
         "exclusions": [{"type": "asset_class", "value": "crypto"}], "capital": 400000},
        {"request_id": "req-r1", "actor_id": "u-robert", "task": "risk_review",
         "portfolio_ref": {"portfolio_id": "pf-robert"},
         "user_message": "I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
         "parameters": {"benchmark": "XIU.TO"}, "toggles": {"stress_tests": True},
         "as_of": "2026-11-12", "intake_confidence": 0.93},
    ),
}

EXPECTED = {
    # persona: (enabled agents, forced agents, template key)
    "amira": ({"risk", "news", "allocation"}, set(), "explainer.intermediate.md"),
    "mateo": ({"risk", "esg", "allocation"}, {"esg"}, "explainer.intermediate.md"),
    "robert": ({"risk"}, set(), "explainer.advanced.md"),
}


def _run_chain(profile_json: dict, request_json: dict) -> tuple[dict, dict]:
    # Context Lambda: the event arrives serialized
    event = json.loads(json.dumps({"profile": profile_json, "request": request_json}))
    out = handler(event, context=None)
    # Dispatcher → InvokeAgentRuntime: the whole payload must be plain JSON
    payload = json.dumps({"profile": profile_json, "request": request_json, **out})
    return out, json.loads(payload)


@pytest.mark.parametrize("persona", list(INTAKE))
def test_persona_full_chain(s3, persona):
    profile_json, request_json = INTAKE[persona]
    out, runtime_payload = _run_chain(profile_json, request_json)

    plan = ExecutionPlan.model_validate(runtime_payload["execution_plan"])
    ref = PromptRef.model_validate(runtime_payload["prompt_ref"])
    enabled, forced, key = EXPECTED[persona]

    assert set(plan.enabled_names) == enabled
    assert {a.agent for a in plan.enabled_agents if a.forced} == forced
    assert plan.request_id == request_json["request_id"] and plan.actor_id == profile_json["actor_id"]
    assert plan.run_governance and plan.run_explainer and not plan.needs_clarification

    # Runtime side: the template is reloaded from S3 and must be exactly the resolved one
    tpl = load_prompt(ref, S3PromptSource(BUCKET, PREFIX, client=s3))
    assert tpl.key == key
    assert tpl.body.strip() and tpl.experience == profile_json["experience"]


def test_mateo_news_disabled_by_his_toggle_is_explained(s3):
    _, payload = _run_chain(*INTAKE["mateo"])
    plan = ExecutionPlan.model_validate(payload["execution_plan"])
    news = next(d for d in plan.disabled_agents if d.agent == "news")
    assert "agent_toggles.news = false" in news.reason
    assert any(w.startswith("esg enabled although") for w in plan.warnings)


def test_robert_parameters_reach_the_orchestrator(s3):
    _, payload = _run_chain(*INTAKE["robert"])
    params = {a["agent"]: a["parameters"] for a in payload["execution_plan"]["enabled_agents"]}
    assert params == {"risk": {"benchmark": "XIU.TO"}}


def test_out_of_scope_chain(s3):
    profile_json, request_json = INTAKE["robert"]
    request_json = request_json | {"task": "out_of_scope", "user_message": "Buy 100 NVDA shares for me."}
    _, payload = _run_chain(profile_json, request_json)
    plan = ExecutionPlan.model_validate(payload["execution_plan"])
    assert plan.enabled_agents == [] and not plan.run_governance and plan.run_explainer
    PromptRef.model_validate(payload["prompt_ref"])   # the Explainer still gets its template to decline


def test_new_user_trade_request_gets_create_profile_message(s3):
    """Real Intake output (mock backend, no stored profile): out_of_scope + profile null → ④ answers
    with the fixed 'create your profile first' message instead of failing."""
    from app.intake import MockBackend, run_intake

    res = run_intake("Buy 100 Tesla shares for me right now.", "u-brand-new", backend=MockBackend())
    assert res.status == "out_of_scope" and res.profile is None  # what the Intake really emits
    event = json.loads(json.dumps({"profile": None, "request": json.loads(res.request.model_dump_json())}))
    out = handler(event)
    assert out["status"] == "profile_required"
    assert "create your investor profile" in out["message"] and "never places real trades" in out["message"]


def test_prompt_republished_between_resolution_and_run_is_refused(s3):
    """A template changed in S3 between the Lambda and the Runtime must never be used silently."""
    _, payload = _run_chain(*INTAKE["robert"])
    ref = PromptRef.model_validate(payload["prompt_ref"])
    s3.objects[PREFIX + ref.key] += b"\nNew instruction.\n"
    with pytest.raises(PromptVersionMismatch):
        load_prompt(ref, S3PromptSource(BUCKET, PREFIX, client=s3))


@pytest.mark.parametrize("mutate", [
    lambda p, r: (p | {"risk_band": "yolo"}, r),                     # value outside the enum
    lambda p, r: (p | {"language": "fr"}, r),                         # French no longer supported
    lambda p, r: (p, r | {"task": "buy_now"}),                        # unknown task
    lambda p, r: (p | {"unexpected": 1}, r),                          # extra field (extra=forbid)
    lambda p, r: (p, {k: v for k, v in r.items() if k != "intake_confidence"}),  # required field missing
])
def test_invalid_intake_json_is_rejected(s3, mutate):
    p, r = mutate(*INTAKE["amira"])
    with pytest.raises(ValidationError):
        handler({"profile": p, "request": r})


def test_mismatched_actor_is_rejected(s3):
    p, r = INTAKE["amira"]
    with pytest.raises(ValueError, match="actor_id"):
        handler({"profile": p, "request": r | {"actor_id": "u-someone-else"}})
