"""Evidence ledger + AfterToolCallEvent hook — no AWS, no LLM."""
from __future__ import annotations

from pathlib import Path

from app.agents.hooks import EvidenceHook
from app.store.ledger import JsonlLedger


class _ToolUse:
    def __init__(self, name: str, tool_input: dict | None = None, tool_use_id: str = ""):
        self.name = name
        self.input = tool_input or {}
        self.toolUseId = tool_use_id or f"tu-{name}"


class _FakeBeforeToolCallEvent:
    def __init__(self, tool_use):
        self.tool_use = tool_use


class _FakeAfterToolCallEvent:
    """Stand-in for the Strands event (tool_use / result / exception are enough)."""

    def __init__(self, tool_name: str, result, exception: Exception | None = None, tool_use_id: str = ""):
        self.tool_use = _ToolUse(tool_name, tool_use_id=tool_use_id)
        self.result = result
        self.exception = exception


def test_ledger_seq_and_unique_ids(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    led.append("run-1", {"tool": "metrics", "evidence_id": "ev-aaa"})
    led.append("run-1", {"tool": "correlation", "evidence_id": "ev-bbb"})
    led.append("run-1", {"tool": "metrics", "evidence_id": "ev-aaa"})  # identical re-call
    recs = led.records("run-1")
    assert [r["seq"] for r in recs] == ["E-001", "E-002", "E-003"]  # every call = 1 record, numbered in call order
    assert led.evidence_ids("run-1") == ["ev-aaa", "ev-bbb"]        # deduplicated citations
    assert led.records("unknown-run") == []


def test_hook_records_risk_tools_evidence(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-2")

    evidence = {"evidence_id": "ev-xyz", "tool": "metrics", "say": "Worst drop -21%.",
                "output": {"portfolio": {"max_drawdown": -0.21}}}
    hook._on_tool_call(_FakeAfterToolCallEvent("metrics", evidence))

    (r,) = led.records("run-2")
    assert r["seq"] == "E-001" and r["evidence_id"] == "ev-xyz"
    assert r["tool"] == "metrics" and r["payload"]["output"]["portfolio"]["max_drawdown"] == -0.21


def test_hook_non_dict_result(tmp_path: Path):
    class _Block:  # ToolResult block without a direct dict
        json = {"evidence_id": "ev-json"}

    led = JsonlLedger(tmp_path)
    EvidenceHook(led, "run-3")._on_tool_call(_FakeAfterToolCallEvent("metrics", _Block()))
    (r,) = led.records("run-3")
    assert r["evidence_id"] == "ev-json"


def test_hook_real_strands_shape(tmp_path: Path):
    """Shape observed in production (Strands ≥ 1.10): ToolResult materialized as
    dict {"content": [{"text": "<evidence JSON>"}], "status": …, "toolUseId": …}."""
    import json as _json

    class _SelectedTool:
        tool_name = "governance_check"

    class _Event:
        tool_use = None                       # absent in this flow
        selected_tool = _SelectedTool()
        exception = None
        result = {"content": [{"text": _json.dumps(
            {"evidence_id": "ev-1a", "tool": "governance_check", "verdict": "BLOCK"})}],
            "status": "success", "toolUseId": "tu-9"}

    led = JsonlLedger(tmp_path)
    EvidenceHook(led, "run-5")._on_tool_call(_Event())
    (r,) = led.records("run-5")
    assert r["tool"] == "governance_check"
    assert r["evidence_id"] == "ev-1a"
    assert r["payload"][0]["verdict"] == "BLOCK"
    assert led.evidence_ids("run-5") == ["ev-1a"]


def test_hook_failed_tool_is_still_evidence(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-4")
    hook._on_tool_call(_FakeAfterToolCallEvent("metrics", None, exception=KeyError("AAA")))
    (r,) = led.records("run-4")
    assert r["error"].startswith("KeyError") and r["payload"] == "None"


def test_hook_tags_nested_subagent_calls(tmp_path: Path):
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-6", agent="esg_subagent")
    hook._on_tool_call(_FakeAfterToolCallEvent("esg_screen", {"evidence_id": "ev-nested"}))
    (r,) = led.records("run-6")
    assert r["agent"] == "esg_subagent" and r["evidence_id"] == "ev-nested"


def test_hook_records_each_call_wall_time(tmp_path: Path):
    """A Before->After pair for the same tool-use id: the record carries the call's wall time;
    an After without its Before (older traces, direct calls) simply has no duration."""
    led = JsonlLedger(tmp_path)
    hook = EvidenceHook(led, "run-7")
    hook._on_before_tool_call(_FakeBeforeToolCallEvent(_ToolUse("metrics", tool_use_id="tu-1")))
    hook._on_tool_call(_FakeAfterToolCallEvent("metrics", {"evidence_id": "ev-ms"}, tool_use_id="tu-1"))
    hook._on_tool_call(_FakeAfterToolCallEvent("metrics", {"evidence_id": "ev-x"}, tool_use_id="tu-other"))
    r1, r2 = led.records("run-7")
    assert isinstance(r1["duration_ms"], int) and r1["duration_ms"] >= 0
    assert "duration_ms" not in r2
