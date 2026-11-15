"""RunGuard (subagents/guard.py): tool budget, identical-call dedupe, model-turn counters.

Pure unit: fake events built from the real Strands dataclasses, no Bedrock, no agent loop.
"""
from strands.hooks.events import AfterModelCallEvent, AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent
from strands.interventions.actions import Deny, Proceed, Transform

from app.agents.subagents.guard import MAX_MODEL_ROUNDS, MAX_TOOL_CALLS, RunGuard, _CachedTool


class FakeTool:
    """Just enough AgentTool for the guard (name + spec)."""

    def __init__(self, name="metrics"):
        self.tool_name = name
        self.tool_spec = {"name": name}


def tool_event(name="metrics", inputs=None, with_tool=True):
    tu = {"toolUseId": "t1", "name": name, "input": inputs or {"tickers": ["XBB.TO"]}}
    return BeforeToolCallEvent(agent=None, selected_tool=FakeTool(name) if with_tool else None,
                               tool_use=tu, invocation_state={})


def result_event(name="metrics", inputs=None, payload=None):
    tu = {"toolUseId": "t1", "name": name, "input": inputs or {"tickers": ["XBB.TO"]}}
    result = {"toolUseId": "t1", "status": "success", "content": [{"json": payload or {"tool": name}}]}
    return AfterToolCallEvent(agent=None, selected_tool=FakeTool(name), tool_use=tu,
                              invocation_state={}, result=result)


def model_events():
    return (BeforeModelCallEvent(agent=None, invocation_state={}),
            AfterModelCallEvent(agent=None, invocation_state={}))


def test_tool_calls_are_allowed_then_denied_at_the_budget():
    g = RunGuard(cache={})
    for i in range(MAX_TOOL_CALLS):                      # distinct calls: each fills its own cache slot
        ev = tool_event(inputs={"tickers": [f"T{i}"]})
        assert isinstance(g.before_tool_call(ev), Proceed), i
        assert isinstance(g.after_tool_call(result_event(inputs={"tickers": [f"T{i}"]})), Proceed)
    denied = g.before_tool_call(tool_event(inputs={"tickers": ["one-too-many"]}))
    assert isinstance(denied, Deny) and "budget" in denied.reason


def test_identical_call_is_replayed_from_the_run_cache():
    cache: dict = {}
    g = RunGuard(cache=cache)
    assert isinstance(g.before_tool_call(tool_event()), Proceed)
    g.after_tool_call(result_event(payload={"tool": "metrics", "evidence_id": "ev-1"}))
    # a second sub-agent (same run, same cache) makes the IDENTICAL call
    action = g.before_tool_call(tool_event())
    assert isinstance(action, Transform)
    event = tool_event()
    action.apply(event)                       # replaces selected_tool with the replay stub
    stub = event.selected_tool
    assert isinstance(stub, _CachedTool)
    replayed = stub._replay(event.tool_use)
    assert replayed["status"] == "success"
    assert replayed["content"][0]["json"]["evidence_id"] == "ev-1"
    assert g.snapshot()["deduped"] == 1       # the replay is not billed as a tool call


def test_different_arguments_are_not_deduped():
    cache: dict = {}
    g = RunGuard(cache=cache)
    g.before_tool_call(tool_event(inputs={"tickers": ["XBB.TO"]}))
    g.after_tool_call(result_event(inputs={"tickers": ["XBB.TO"]}))
    other = g.before_tool_call(tool_event(inputs={"tickers": ["RY.TO"]}))
    assert isinstance(other, Proceed)         # different args: a real (new) call


def test_inflight_duplicate_in_one_batch_is_denied_not_recomputed():
    cache: dict = {}
    g = RunGuard(cache=cache)
    assert isinstance(g.before_tool_call(tool_event()), Proceed)          # reserved, result pending
    dup = g.before_tool_call(tool_event())                                # same batch, identical args
    assert isinstance(dup, Deny) and "already requested" in dup.reason


def test_unparseable_result_is_not_cached():
    cache: dict = {}
    g = RunGuard(cache=cache)
    g.before_tool_call(tool_event())
    bad = {"toolUseId": "t1", "status": "success", "content": [{"text": "not json"}]}
    g.after_tool_call(AfterToolCallEvent(agent=None, selected_tool=FakeTool(), tool_use=tool_event().tool_use,
                                         invocation_state={}, result=bad))
    assert cache[next(iter(cache))] is None   # stays reserved as None -> never replayed


def test_model_round_budget_and_counters():
    g = RunGuard(cache={})
    for _ in range(MAX_MODEL_ROUNDS):
        before, after = model_events()
        assert isinstance(g.before_model_call(before), Proceed)
        g.after_model_call(after)
    over, over_after = model_events()
    assert isinstance(g.before_model_call(over), Deny)
    g.after_model_call(over_after)
    snap = g.snapshot()
    assert snap["model_calls"] == MAX_MODEL_ROUNDS + 1
    assert snap["tool_calls"] == 0
    assert snap["model_ms_total"] >= 0


def test_snapshot_shape_for_the_ledger():
    g = RunGuard(cache={})
    b, a = model_events()
    g.before_model_call(b)
    g.after_model_call(a)
    g.before_tool_call(tool_event())
    g.after_tool_call(result_event())
    snap = g.snapshot()
    assert {"model_calls", "tool_calls", "deduped", "denied",
            "model_ms_total", "model_ms_avg", "model_ms_max"} <= set(snap)
