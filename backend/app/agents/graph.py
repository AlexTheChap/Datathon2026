"""L8 — parallel sub-agent graph (Strands fork/join): plan-enabled sub-agents run
CONCURRENTLY, then one synthesis node joins their findings.

Why: the tools mode called sub-agents serially — each delegation was a full
orchestrator round-trip. Here the plan decides dispatch (no routing turns) and
independent sub-agents execute at the same time, so wall-time is the slowest
sub-agent instead of their sum.

Each node wraps one sub-agent tool function (contract validation, repair loop
and evidence envelope included, see subagents/base.make_subagent_tool), run in
a worker thread so the parallel graph event loop is never blocked. Node results
are the standard evidence envelopes; the caller appends them to the run ledger
tagged with the sub-agent name (L15), exactly like the tools mode.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from strands.multiagent import GraphBuilder
from strands.agent.agent_result import AgentResult
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.telemetry.metrics import EventLoopMetrics


class ToolNode(MultiAgentBase):
    """Graph node that runs one sub-agent tool function with fixed arguments."""

    def __init__(self, name: str, tool_fn: Callable[..., dict], subject: str,
                 weights: dict[str, float] | None = None, tickers: list[str] | None = None):
        super().__init__()
        self.id = name
        self._fn = tool_fn
        self._args = {"subject": subject, "weights": weights, "tickers": tickers}

    async def invoke_async(self, task: Any = None, invocation_state: dict | None = None,
                           **kwargs: Any) -> MultiAgentResult:
        t0 = time.perf_counter()
        # Sub-agent tools are synchronous (Bedrock calls): a worker thread keeps the
        # graph's event loop free so sibling nodes genuinely run in parallel.
        try:
            envelope = await asyncio.to_thread(self._fn, **self._args)
            if not isinstance(envelope, dict):
                envelope = {"error": f"{self.id} returned a non-dict result"}
        except Exception as e:  # noqa: BLE001  throttling, tool failures...: visible, never silent
            envelope = {"error": f"{self.id} failed: {type(e).__name__}: {e}"}
        ms = int((time.perf_counter() - t0) * 1000)
        ok = "error" not in envelope
        # The NodeResult carries the envelope inside a real AgentResult: the graph's input
        # aggregation (get_agent_results) walks AgentResult/MultiAgentResult shapes only.
        import json as _json

        agent_result = AgentResult(
            stop_reason="end_turn",
            message={"role": "assistant",
                     "content": [{"text": _json.dumps(envelope, ensure_ascii=False)}]},
            metrics=EventLoopMetrics(), state={},
        )
        return MultiAgentResult(
            status=Status.COMPLETED if ok else Status.FAILED,
            results={self.id: NodeResult(result=agent_result, execution_time=ms,
                                         status=Status.COMPLETED if ok else Status.FAILED)})


def _tool_name(key: str, fn: Callable) -> str:
    """The sub-agent's own tool name (e.g. "risk_subagent"), from the registry function."""
    return getattr(fn, "tool_name", None) or getattr(fn, "__name__", key)


def _all_arrived(keys: list[str]):
    """AND-join: the synthesis node fires only when every fork has produced a result
    (default graph semantics are OR — the first finished node would trigger it)."""
    def check(state) -> bool:
        return all(k in state.results for k in keys)
    return check


def run(enabled: list[tuple[str, Callable[..., dict]]], subject: str,
        weights: dict[str, float] | None, tickers: list[str] | None,
        synthesis_agent=None, ledger=None, run_id: str | None = None) -> dict:
    """Fork every (key, sub-agent tool) in parallel, join into `synthesis_agent`.

    Returns {"summary": <synthesis text>, "findings": [envelope dicts]} — the
    envelopes are also appended to the run ledger (tagged with the sub-agent name),
    so the Explainer and the dashboard read the same records as the tools mode.
    """
    builder = GraphBuilder()
    keys = [key for key, _ in enabled]
    for key, fn in enabled:
        builder.add_node(ToolNode(key, fn, subject, weights, tickers), key)
    if synthesis_agent is not None:
        builder.add_node(synthesis_agent, "synthesis")   # node before its edges
        for key in keys:
            builder.add_edge(key, "synthesis", condition=_all_arrived(keys))
    # Bounded by construction: each node runs once (fork/join DAG), plus margin.
    builder.set_max_node_executions(len(keys) + 4)
    builder.set_execution_timeout(900)   # 15 min, same ceiling as the Bedrock clients
    result = builder.build()(subject)

    def _envelope(key: str) -> dict | None:
        """The ToolNode returns a MultiAgentResult that the graph wraps in another
        NodeResult; descend until the envelope (dict, or JSON text of one)."""
        import json as _json

        node = result.results.get(key)
        r = getattr(node, "result", None) if node else None
        for _ in range(3):
            if isinstance(r, dict):
                return r
            if hasattr(r, "message"):                     # AgentResult: text = envelope JSON
                text = str(r)
                try:
                    return _json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    return None
            inner = getattr(r, "results", None)
            r = inner.get(key).result if inner and key in inner else None
        return None

    def _node_ms(key: str) -> int:
        """Wall time of a fork node, as measured by the graph (outer NodeResult.execution_time)."""
        node = result.results.get(key)
        return int(getattr(node, "execution_time", 0) or 0)

    findings = []
    for key, _fn in enabled:
        envelope = _envelope(key) or {"error": f"{key} produced no result",
                                      "note": "The node failed before returning an evidence envelope."}
        findings.append({**envelope, "_duration_ms": _node_ms(key),
                         "_key": key, "_tool_name": _tool_name(key, _fn)})

    synthesis_result = result.results.get("synthesis")
    summary = str(synthesis_result.result) if synthesis_result is not None else ""

    if ledger is not None and run_id:
        for f in findings:
            envelope = {k: v for k, v in f.items() if not k.startswith("_")}
            ledger.append(run_id, {"payload": envelope, "agent": f["_tool_name"],
                                   "tool": f["_tool_name"],
                                   "evidence_id": envelope.get("evidence_id"),
                                   "say": envelope.get("say", ""),
                                   "duration_ms": f["_duration_ms"]})
    return {"summary": summary, "findings": findings}
