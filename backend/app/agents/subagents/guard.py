"""Per-invocation guard for one sub-agent run: turn budget, duplicate-tool dedupe, call timing.

Three jobs, all through the Strands intervention API (no changes to the agent loop):

1. TOOL BUDGET — after `max_tool_calls` executions, further tool calls are denied with a
   message telling the model to answer from the evidence it already has. The observed failure
   (risk_subagent re-running its whole battery, 15 calls in one run) becomes impossible; a
   legitimate batched path needs at most ~2 rounds.
2. DEDUPE — an identical (tool, arguments) call already executed by ANY sub-agent of the same
   run returns the cached envelope immediately (a stub tool, so the model sees a normal tool
   result): risk and allocation both screening the same universe is billed once.
3. TIMING/COUNTERS — every model call and tool call of this agent is counted and timed, for the
   ledger record `make_subagent_tool` appends at the end of the run (model turns were invisible
   before: the ledger only showed tool calls).
"""
from __future__ import annotations

import json
import time
from typing import Any

from strands.interventions.actions import Deny, Proceed, Transform
from strands.interventions.handler import InterventionHandler
from strands.tools.tools import PythonAgentTool

# A batched run is 1-2 tool rounds + the answer; 8 calls is headroom, not a target.
MAX_TOOL_CALLS = 8
# Runaway safety net: a legit sub-agent finishes in 2-3 model rounds (tool round(s) + JSON).
MAX_MODEL_ROUNDS = 8

_BUDGET_MESSAGE = ("Tool budget reached for this analysis: no more tool calls. Write your JSON answer now "
                   "from the tool results already in this conversation.")
_INFLIGHT_MESSAGE = ("You already requested this exact call in this batch: its result arrives with this "
                     "same batch. Do not repeat it.")


def _canonical(value: Any) -> str:
    """Stable JSON for a tool input dict (sorted keys, so {'a':1,'b':2} == {'b':2,'a':1})."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _call_key(name: str, tool_use: Any) -> str:
    """(tool, canonical arguments) identity of one tool call."""
    input_ = tool_use.get("input") if isinstance(tool_use, dict) else getattr(tool_use, "input", None)
    return f"{name}:{_canonical(input_ if input_ is not None else {})}"


def _result_payload(result: Any) -> Any:
    """First dict/list payload of a tool result (dict {'content': [blocks]} or object with .content)."""
    content = None
    if isinstance(result, dict):
        content = result.get("content")
    else:
        content = getattr(result, "content", None)
    blocks = content if isinstance(content, list) else ([content] if content is not None else [])
    for b in blocks:
        v = b.get("json", b.get("text")) if isinstance(b, dict) else getattr(b, "json", None)
        if v is None and not isinstance(b, dict):
            v = getattr(b, "text", None)
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str) and v[:1] in ("{", "["):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                continue
    return None


class _CachedTool(PythonAgentTool):
    """Stands in for a tool whose identical call already ran: replays the recorded envelope."""

    def __init__(self, name: str, spec: dict, payload: Any):
        super().__init__(name, spec, self._replay)
        self._payload = payload

    def _replay(self, tool_use: dict, **_: Any) -> dict:
        return {"toolUseId": (tool_use or {}).get("toolUseId", "cached"),
                "status": "success",
                "content": [{"json": self._payload}]}


class RunGuard(InterventionHandler):
    """Attach to a sub-agent Agent(interventions=[…]); counters cover the whole run (all repair turns)."""

    name = "af-subagent-guard"

    def __init__(self, cache: dict[str, Any] | None = None,
                 max_tool_calls: int = MAX_TOOL_CALLS, max_model_rounds: int = MAX_MODEL_ROUNDS):
        self.cache = cache if cache is not None else {}
        self.max_tool_calls = max_tool_calls
        self.max_model_rounds = max_model_rounds
        self.model_calls = 0
        self.tool_calls = 0
        self.deduped = 0
        self.denied = 0
        self.model_ms: list[int] = []
        self._t0: float | None = None

    def snapshot(self) -> dict:
        """Counters for the ledger: model turns were invisible before (the ledger only showed tools)."""
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "deduped": self.deduped, "denied": self.denied,
                "model_ms_total": sum(self.model_ms),
                "model_ms_avg": sum(self.model_ms) // len(self.model_ms) if self.model_ms else 0,
                "model_ms_max": max(self.model_ms, default=0)}

    # ---- model turns ------------------------------------------------------------------------
    def before_model_call(self, event, **_: Any):
        self.model_calls += 1
        self._t0 = time.perf_counter()
        if self.model_calls > self.max_model_rounds:
            return Deny(reason="Model round budget reached: end this invocation now.")
        return Proceed()

    def after_model_call(self, event, **_: Any):
        if self._t0 is not None:
            self.model_ms.append(int((time.perf_counter() - self._t0) * 1000))
            self._t0 = None
        return Proceed()

    # ---- tool calls --------------------------------------------------------------------------
    def before_tool_call(self, event, **_: Any):
        tool_use = getattr(event, "tool_use", None)
        selected = getattr(event, "selected_tool", None)
        name = (tool_use.get("name") if isinstance(tool_use, dict) else None) \
            or getattr(selected, "tool_name", None) or "unknown"
        key = _call_key(name, tool_use)
        cached = self.cache.get(key)
        if cached is not None:   # an identical call already completed somewhere in this run
            self.deduped += 1
            spec = getattr(selected, "tool_spec", {}) or {}
            return Transform(
                apply=lambda e, _t=_CachedTool(name, spec, cached): setattr(e, "selected_tool", _t),
                reason=f"identical call already executed this run (deduped): {name}",
            )
        if self.tool_calls >= self.max_tool_calls:
            self.denied += 1
            return Deny(reason=_BUDGET_MESSAGE)
        if key in {k for k, v in self.cache.items() if v is None}:   # same batch: result is already coming
            self.denied += 1
            return Deny(reason=_INFLIGHT_MESSAGE)
        self.tool_calls += 1
        self.cache[key] = None   # reserved until the result arrives (see after_tool_call)
        return Proceed()

    def after_tool_call(self, event, **_: Any):
        tool_use = getattr(event, "tool_use", None)
        selected = getattr(event, "selected_tool", None)
        name = (tool_use.get("name") if isinstance(tool_use, dict) else None) \
            or getattr(selected, "tool_name", None)
        if name:
            key = _call_key(name, tool_use)
            if key in self.cache and self.cache[key] is None:
                payload = _result_payload(getattr(event, "result", None))
                if payload is not None:   # unparseable results stay unreserved: never replay a guess
                    self.cache[key] = payload
        return Proceed()
