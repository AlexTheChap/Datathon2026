"""Run context shared by the agent tools (node ⑥): the investor profile and the reference date of the run.

A Strands tool is stateless, so the orchestrator sets these ContextVars once per run
(`run_orchestrator`). Every tool then reads its point-in-time date from here, never from the LLM:
`effective_as_of(arg)` returns the run's `as_of`, or an EARLIER date if the model asks for one. A model can
never push a tool past the run's reference date (no look-ahead bias).
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
from typing import Any

import pandas as pd
from app.core.config import get_settings

# Profile validated by the Intake, set by the orchestrator at the start of a run.
current_profile: ContextVar[dict[str, Any] | None] = ContextVar("af_current_profile", default=None)
# Reference date of the run ("YYYY-MM-DD"); None = the last date of the curated data.
current_as_of: ContextVar[str | None] = ContextVar("af_current_as_of", default=None)


@lru_cache
def _last_data_date(curated_dir: str) -> str:
    cal = pd.read_csv(f"{curated_dir}/calendar.csv", usecols=["date"])
    return str(pd.Timestamp(cal["date"].iloc[-1]).date())


def default_as_of() -> str:
    """AF_DEFAULT_AS_OF if set, otherwise the last date of the curated data."""
    s = get_settings()
    return s.default_as_of or _last_data_date(str(s.abs_path(s.curated_dir)))


def run_as_of() -> str:
    """Reference date of the current run."""
    return current_as_of.get() or default_as_of()


def effective_as_of(requested: str | None = None) -> str:
    """The date a tool must use: the run's as_of, or an earlier date asked for by the caller."""
    limit = run_as_of()
    if requested:
        requested = str(pd.Timestamp(requested).date())
        if requested < limit:
            return requested
    return limit


def persona_profile() -> dict[str, Any] | None:
    """Profile of the run in the format read by `governance_check.check` and `optimizer.optimize`
    (personas.json style: an "id" key and the horizon as [min, max])."""
    profile = current_profile.get()
    if profile is None:
        return None
    profile = dict(profile)
    profile.setdefault("id", profile.get("actor_id"))
    h = profile.get("horizon_years")
    if isinstance(h, dict):
        profile["horizon_years"] = [h.get("min"), h.get("max")]
    return profile
