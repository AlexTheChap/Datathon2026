"""Box 3 — Intake agent: free text -> validated Profile JSON + Request JSON (see docs/architecture.mmd)."""
from .agent import IntakeError, run_intake
from .backends import BedrockBackend, IntakeRefused, MockBackend, get_backend
from .schemas import IntakeDraft, IntakeResult

__all__ = [
    "BedrockBackend",
    "IntakeDraft",
    "IntakeError",
    "IntakeRefused",
    "IntakeResult",
    "MockBackend",
    "get_backend",
    "run_intake",
]
