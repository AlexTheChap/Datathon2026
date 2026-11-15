"""Parallel sub-agent graph (L8) — no AWS, no LLM: node execution, AND-join, envelopes, ledger."""
from __future__ import annotations

import asyncio

from strands.multiagent.base import Status

from app.agents.graph import ToolNode, _all_arrived, run
from app.store.ledger import InMemoryLedger


def _node(fn, name="node"):
    return ToolNode(name, fn, subject="What about NVDA at 20%?",
                    weights={"XIU.TO": 0.8, "NVDA": 0.2}, tickers=["NVDA"])


def test_tool_node_completed_returns_the_envelope():
    envelope = {"evidence_id": "ev-1", "tool": "risk_subagent", "say": "ok"}

    async def go():
        return await _node(lambda **kw: envelope).invoke_async("ignored")

    result = asyncio.run(go())
    assert result.status == Status.COMPLETED
    import json as _json
    assert _json.loads(str(result.results["node"].result)) == envelope
    assert result.results["node"].status == Status.COMPLETED


def test_tool_node_failed_when_the_tool_reports_an_error():
    async def go():
        return await _node(lambda **kw: {"error": "no valid finding"}).invoke_async("ignored")

    result = asyncio.run(go())
    assert result.status == Status.FAILED
    import json as _json
    assert _json.loads(str(result.results["node"].result))["error"].startswith("no valid finding")


def test_all_arrived_requires_every_key():
    class State:
        results = {"risk": object()}

    check = _all_arrived(["risk", "macro"])
    assert check(State()) is False
    State.results["macro"] = object()
    assert check(State()) is True


def test_run_finds_envelopes_and_tags_the_ledger():
    led = InMemoryLedger()

    def risk_ok(**kw):
        assert kw["subject"] == "question"            # the subject flows through the node args
        return {"evidence_id": "ev-r", "tool": "risk_subagent", "say": "risk ok"}

    def news_err(**kw):
        return {"error": "news_subagent failed: x"}

    enabled = [("risk", risk_ok), ("news", news_err)]
    out = run(enabled, "question", {"XIU.TO": 1.0}, ["NVDA"],
             synthesis_agent=None, ledger=led, run_id="run-g")

    assert out["summary"] == ""                    # synthesis_agent=None -> no synthesis text
    by_key = {f["_key"]: f for f in out["findings"]}
    assert by_key["risk"]["say"] == "risk ok"
    assert by_key["news"]["error"].startswith("news_subagent failed")
    recs = led.records("run-g")
    # without a registry tool_name, the fallback is the function __name__
    assert {(r["agent"], r["evidence_id"]) for r in recs} == {("risk_ok", "ev-r"), ("news_err", None)}


def test_run_passes_structured_args_to_each_node():
    seen = {}

    def risk(**kw):
        seen["risk"] = kw
        return {"evidence_id": "ev-a", "tool": "risk_subagent", "say": "s"}

    def news(**kw):
        seen["news"] = kw
        return {"evidence_id": "ev-b", "tool": "news_subagent", "say": "s"}

    run([("risk", risk), ("news", news)], "q", {"NVDA": 0.2}, ["NVDA"])
    assert seen["risk"]["weights"] == {"NVDA": 0.2} and seen["risk"]["tickers"] == ["NVDA"]
    assert seen["news"]["subject"] == "q"
