"""Box 3 — Intake agent: free text -> validated Profile JSON + Request JSON (see docs/architecture.mmd)."""
from .agent import IntakeError, derive_preferences, run_intake
from .backends import BedrockBackend, IntakeRefused, IntakeUnavailable, LambdaBackend, MockBackend, get_backend
from .schemas import IntakeDraft, IntakeResult

__all__ = [
    "BedrockBackend",
    "IntakeDraft",
    "IntakeError",
    "IntakeRefused",
    "IntakeResult",
    "IntakeUnavailable",
    "LambdaBackend",
    "MockBackend",
    "derive_preferences",
    "get_backend",
    "run_intake",
]
