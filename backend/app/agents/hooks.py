"""Hook L15: every tool call becomes one evidence record in the ledger.

Strands HookProvider (Agent(hooks=[…])): register_hooks subscribes
_on_tool_call to AfterToolCallEvent. The risk_tools already return a complete
evidence envelope (evidence_id, say, assumptions): we record it as-is,
including when the tool fails (exception field).
"""
from __future__ import annotations

import json
from typing import Any

from strands.hooks.events import AfterToolCallEvent

from app.store.ledger import EvidenceLedger, _truncate


def _block_value(b: Any) -> Any:
    """Value of a content block: {"json": …} as-is; {"text": …} decoded when JSON."""
    if isinstance(b, dict):
        for key in ("json", "text"):
            v = b.get(key)
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                return v
            s = str(v).strip()
            if s[:1] in ("{", "["):
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    return s
            return s
    for attr in ("json", "text"):
        v = getattr(b, attr, None)
        if v is not None:
            return v
    return str(b)


def _payload(result: Any) -> Any:
    """Extract a serializable payload from a tool result.

    Observed shape (Strands ≥ 1.10): dict {"content": [{"text": "<json>"}], …}.
    Older shapes accepted: plain dict, .content object, .json/.text object.
    """
    if isinstance(result, dict) and "content" in result:
        c = result["content"]
        blocks = c if isinstance(c, list) else [c]
        return _truncate([_block_value(b) for b in blocks])
    if isinstance(result, dict):
        return _truncate(result)
    content = getattr(result, "content", None)
    if content is not None:
        blocks = content if isinstance(content, list) else [content]
        return _truncate([_block_value(b) for b in blocks])
    for attr in ("json", "text"):
        v = getattr(result, attr, None)
        if v is not None:
            return _truncate(v)
    return _truncate(str(result))


class EvidenceHook:
    """Provider to pass to Agent(hooks=[EvidenceHook(ledger, run_id)])."""

    def __init__(self, ledger: EvidenceLedger, run_id: str):
        self.ledger = ledger
        self.run_id = run_id

    def register_hooks(self, registry, **kwargs) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_tool_call)

    def _on_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", None)
        selected = getattr(event, "selected_tool", None)
        name = (getattr(tool_use, "name", None)
                or getattr(selected, "tool_name", None)
                or getattr(selected, "name", None)
                or "unknown")
        record: dict[str, Any] = {
            "tool": name,
            "tool_input": getattr(tool_use, "input", None),
        }
        if getattr(event, "exception", None) is not None:
            record["error"] = f"{type(event.exception).__name__}: {event.exception}"
        payload = _payload(getattr(event, "result", None))
        record["payload"] = payload
        # evidence_id when the tool returns a risk_tools envelope (dict or {json: …} block)
        candidate = payload if isinstance(payload, dict) else None
        if candidate is None and isinstance(payload, list):
            candidate = next((b for b in payload if isinstance(b, dict) and "evidence_id" in b), None)
        if candidate:
            for key in ("evidence_id", "say", "as_of", "period", "tool"):
                if key in candidate:
                    record.setdefault(key, candidate[key])
        self.ledger.append(self.run_id, record)
