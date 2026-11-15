"""Boîte ③ — Intake agent : texte libre → Profile JSON + Request JSON validés (voir docs/architecture.mmd)."""
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
