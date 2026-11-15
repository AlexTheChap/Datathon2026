"""④ Lambda entry point: the "no profile yet" path (Intake returned profile: null)."""
import pytest
from pydantic import ValidationError

from app.context import handler
from app.context.handler import OUT_OF_SCOPE_NOTE, PROFILE_REQUIRED_MESSAGE
from app.schemas.context_result import ContextResult


def _request(task: str, **extra) -> dict:
    return {"request_id": "req-1", "actor_id": "u-new", "task": task,
            "user_message": "Buy 100 Tesla shares for me right now.", "intake_confidence": 0.95, **extra}


def test_new_user_out_of_scope_is_told_to_create_a_profile():
    out = handler({"profile": None, "request": _request("out_of_scope")})
    assert out == {"schema_version": "1.0", "status": "profile_required",
                   "message": PROFILE_REQUIRED_MESSAGE + OUT_OF_SCOPE_NOTE}
    assert "create your investor profile" in out["message"]
    assert "never places real trades" in out["message"]
    ContextResult.model_validate(out)


@pytest.mark.parametrize("task", ["risk_review", "allocation_proposal", "esg_screen"])
def test_new_user_in_scope_request_is_told_to_create_a_profile(task):
    out = handler({"profile": None, "request": _request(task)})
    assert out["status"] == "profile_required"
    assert out["message"] == PROFILE_REQUIRED_MESSAGE  # no trade note for a normal request
    assert "execution_plan" not in out and "prompt_ref" not in out


@pytest.mark.parametrize("event", [
    {"profile": None, "request": None},   # Intake refused (guardrail): neither profile nor request
    {"profile": None},                    # keys missing altogether
])
def test_no_profile_and_no_request(event):
    assert handler(event)["status"] == "profile_required"


def test_no_profile_still_rejects_an_invalid_request():
    with pytest.raises(ValidationError):
        handler({"profile": None, "request": _request("buy_now")})


def test_profile_without_request_is_rejected(example_profile):
    with pytest.raises(ValidationError):
        handler({"profile": example_profile.model_dump(mode="json"), "request": None})


def test_message_does_not_echo_user_input():
    """Fixed text only: nothing from the user's message is reflected back."""
    out = handler({"profile": None, "request": _request("out_of_scope", user_message="<script>alert(1)</script>")})
    assert "<script>" not in out["message"]


def test_context_result_rejects_inconsistent_states():
    with pytest.raises(ValidationError):
        ContextResult(status="planned")                      # planned without a plan
    with pytest.raises(ValidationError):
        ContextResult(status="profile_required")             # no message
