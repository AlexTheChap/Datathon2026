"""Sub-agent output contract: schema, citations, evidence behind the finding, domain validators, repair loop.
The Strands Agent is replaced by a scripted fake: no AWS, no LLM."""
from __future__ import annotations

import json
from typing import ClassVar

import pytest
from app.agents.subagents import base
from app.agents.subagents.allocation import _validate_allocation
from app.agents.subagents.base import (
    FindingError,
    event_validator,
    make_subagent_tool,
    render_brief,
    run_context,
    validate_finding,
)
from app.store.ledger import InMemoryLedger
from app.tools.context import current_profile

EV1, EV2, OTHER = "ev-aaaaaaaaaa", "ev-bbbbbbbbbb", "ev-cccccccccc"


def finding(**kw) -> dict:
    return {"headline": "H", "details": [f"fact {EV1}"], "severity": "info", **kw}


def record(eid: str, tool_name: str = "metrics", **payload) -> dict:
    return {"tool": tool_name, "evidence_id": eid, "payload": {"evidence_id": eid, "tool": tool_name, **payload}}


# ---------- validate_finding ----------

def test_valid_finding_passes_and_keeps_extra_keys():
    out = validate_finding(finding(weights={"A": 1.0}), [EV1])
    assert out["weights"] == {"A": 1.0} and out["severity"] == "info"


@pytest.mark.parametrize("bad", [
    {"details": [], "severity": "info"},                              # no headline
    {"headline": "", "details": [], "severity": "info"},              # empty headline
    {"headline": "H", "details": [], "severity": "critical"},         # severity out of the enum
    {"headline": "H", "details": "one string", "severity": "info"},   # details must be a list
    {"headline": "H", "details": [{"a": 1}], "severity": "info"},     # details must be strings
])
def test_schema_violations_are_rejected(bad):
    with pytest.raises(FindingError, match="contract"):
        validate_finding(bad, [EV1])


def test_unknown_citation_is_rejected():
    with pytest.raises(FindingError, match="ev-bbbbbbbbbb"):
        validate_finding(finding(details=[f"x {EV1}", f"y {EV2}"]), [EV1])


def test_a_sub_agent_with_tools_must_have_called_one():
    with pytest.raises(FindingError, match="without calling any tool"):
        validate_finding(finding(details=["no citation"]), [], requires_evidence=True)
    assert validate_finding(finding(details=["no citation"]), [], requires_evidence=False)["headline"] == "H"


# ---------- domain validators ----------

def test_allocation_weights_must_be_the_optimizer_weights():
    opt = record(EV1, "optimize", output={"status": "ok", "weights": {"XIU.TO": 0.6, "XBB.TO": 0.4}})
    _validate_allocation({"weights": {"XIU.TO": 0.6, "XBB.TO": 0.4}}, [opt])
    _validate_allocation({"weights": {"XIU.TO": 0.60004, "XBB.TO": 0.39996}}, [opt])       # within 1e-4
    for wrong in ({"XIU.TO": 0.7, "XBB.TO": 0.3}, {"XIU.TO": 1.0}, {"XIU.TO": 0.6, "XBB.TO": 0.3, "SPY": 0.1}, {}):
        with pytest.raises(FindingError, match="copied exactly"):
            _validate_allocation({"weights": wrong}, [opt])


def test_allocation_without_optimize_is_rejected():
    with pytest.raises(FindingError, match="optimize"):
        _validate_allocation({"weights": {"XIU.TO": 1.0}}, [record(EV1, "metrics")])


def test_allocation_infeasible_means_no_weights():
    opt = record(EV1, "optimize", output={"status": "infeasible", "weights": {}})
    _validate_allocation({"weights": {}}, [opt])
    with pytest.raises(FindingError, match="empty"):
        _validate_allocation({"weights": {"XIU.TO": 1.0}}, [opt])


def test_news_event_ids_must_come_from_the_search():
    search = record(EV1, "news_search", output={"events": [{"event_id": "N-0123456789"}]})
    event_validator({"items": [{"event_id": "N-0123456789"}]}, [search])
    event_validator({"items": [], "no_news": ["TSLA"]}, [search])
    with pytest.raises(FindingError, match="N-9999999999"):
        event_validator({"items": [{"event_id": "N-9999999999"}]}, [search])
    with pytest.raises(FindingError):
        event_validator({"details": ["N-0123456789"]}, [])                  # no search at all


# ---------- the tool: repair loop with a scripted agent ----------

class FakeAgent:
    """Replaces strands.Agent: each call plays the next script step (ledger records to write, answer text)."""
    script: ClassVar[list] = []
    calls: ClassVar[list] = []

    def __init__(self, model=None, system_prompt=None, tools=None, hooks=None, **_):
        self.hook = hooks[0]

    def __call__(self, message):
        FakeAgent.calls.append(message)
        step = FakeAgent.script.pop(0)
        if isinstance(step, Exception):
            raise step
        records, text = step
        for r in records:
            self.hook.ledger.append(self.hook.run_id, {**r, "agent": self.hook.agent})
        return text


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(base, "Agent", FakeAgent)
    monkeypatch.setattr(base, "BedrockModel", lambda **kw: object())
    FakeAgent.script, FakeAgent.calls = [], []
    ledger = InMemoryLedger()
    token = run_context.set({"ledger": ledger, "run_id": "run-t", "as_of": "2026-11-12"})
    yield ledger
    run_context.reset(token)


def sub(tools=("t",), validator=None):
    return make_subagent_tool("test_subagent", "prompt", tools=list(tools), extra_validator=validator)


def test_valid_answer_becomes_an_envelope_with_the_nested_evidence(fake):
    FakeAgent.script = [([record(EV1)], json.dumps(finding()))]
    out = sub()(subject="q")
    assert out["tool"] == "test_subagent" and out["nested_evidence_ids"] == [EV1]
    assert out["output"]["headline"] == "H" and out["evidence_id"].startswith("ev-")
    assert len(FakeAgent.calls) == 1


def test_a_rejected_answer_gets_one_repair_with_the_reasons(fake):
    FakeAgent.script = [([record(EV1)], json.dumps(finding(details=[f"invented {EV2}"]))),
                        ([], json.dumps(finding()))]
    out = sub()(subject="q")
    assert out["nested_evidence_ids"] == [EV1] and "error" not in out
    assert len(FakeAgent.calls) == 2 and "rejected" in FakeAgent.calls[1] and EV2 in FakeAgent.calls[1]


def test_two_rejections_return_an_error_not_an_unverified_finding(fake):
    bad = json.dumps(finding(details=[f"invented {EV2}"]))
    FakeAgent.script = [([record(EV1)], bad), ([], bad)]
    out = sub()(subject="q")
    assert "no valid finding" in out["error"] and "output" not in out
    assert len(FakeAgent.calls) == 2


def test_no_tool_call_is_rejected(fake):
    FakeAgent.script = [([], json.dumps(finding(details=["no citation"]))),
                        ([], json.dumps(finding(details=["still none"])))]
    assert "no valid finding" in sub()(subject="q")["error"]


def test_a_sub_agent_without_tools_needs_no_evidence(fake):
    FakeAgent.script = [([], json.dumps(finding(details=["plain"])))]
    assert sub(tools=())(subject="q")["output"]["details"] == ["plain"]


def test_evidence_of_another_sub_agent_cannot_be_cited(fake):
    fake.append("run-t", {"tool": "metrics", "evidence_id": OTHER, "agent": "other_subagent",
                          "payload": {"evidence_id": OTHER}})
    bad = json.dumps(finding(details=[f"stolen {OTHER}"]))
    FakeAgent.script = [([record(EV1)], bad), ([], bad)]
    assert "error" in sub()(subject="q")


def test_domain_validator_runs_on_the_agents_own_records(fake):
    FakeAgent.script = [([record(EV1, "optimize", output={"status": "ok", "weights": {"A": 1.0}})],
                         json.dumps(finding(weights={"A": 1.0})))]
    out = sub(validator=_validate_allocation)(subject="q")
    assert out["output"]["weights"] == {"A": 1.0}


def test_model_errors_are_returned_as_an_error_dict(fake):
    FakeAgent.script = [RuntimeError("ThrottlingException: Too many requests")]
    out = sub()(subject="q")
    assert "RuntimeError" in out["error"] and "Throttling" in out["error"]


def test_runs_without_an_orchestrator_use_a_private_ledger(monkeypatch):
    monkeypatch.setattr(base, "Agent", FakeAgent)
    monkeypatch.setattr(base, "BedrockModel", lambda **kw: object())
    token = run_context.set(None)
    try:
        FakeAgent.script, FakeAgent.calls = [([record(EV1)], json.dumps(finding()))], []
        assert sub()(subject="q")["nested_evidence_ids"] == [EV1]
    finally:
        run_context.reset(token)


# ---------- the brief ----------

@pytest.mark.parametrize("horizon, text", [
    ({"min": 3, "max": 5}, "horizon=3–5y"),
    ({"min": 15, "max": None}, "horizon=15y+"),
    ({"min": None, "max": 5}, "horizon=up to 5y"),
    ({"min": None, "max": None}, "horizon=unspecified"),
])
def test_brief_horizon_never_prints_none(horizon, text):
    token = current_profile.set({"risk_band": "high", "horizon_years": horizon, "capital": 50000.0})
    ctx = run_context.set(None)
    try:
        out = render_brief("q")
    finally:
        run_context.reset(ctx)
        current_profile.reset(token)
    assert text in out and "None" not in out


# ---------- figures must come from the cited evidence ----------

STRESS_EV = {EV1: {"evidence_id": EV1, "tool": "stress", "say": "Worst point -21.1%.",
                   "output": {"portfolio": {"total_return": -0.115, "max_drawdown": -0.2112}, "loss": -76896.0}}}


def test_figures_in_details_must_match_the_cited_evidence():
    ok = [f"2022 replay: maximum drawdown -21.1% ({EV1}).", f"About $76,896 lost at the trough [{EV1}]."]
    validate_finding(finding(details=ok), [EV1], evidence=STRESS_EV)


@pytest.mark.parametrize("line, message", [
    (f"Maximum drawdown -35% ({EV1}).", "does not appear in the evidence"),
    (f"A 1pp rate rise would cost about 4% ({EV1}).", "does not appear in the evidence"),
    ("Maximum drawdown -21.1%, no citation here.", "without an evidence_id"),
])
def test_invented_or_uncited_figures_are_rejected(line, message):
    with pytest.raises(FindingError, match=message):
        validate_finding(finding(details=[f"anchor {EV1}", line]), [EV1], evidence=STRESS_EV)


def test_a_sub_agent_that_computes_its_own_number_gets_one_repair(fake):
    invented = json.dumps(finding(details=[f"A 1pp rate rise would cost about 4% ({EV1})."]))
    ok = json.dumps(finding(details=[f"Maximum drawdown -21.1% ({EV1})."]))
    FakeAgent.script = [([record(EV1, "stress", say="Worst point -21.1%.", output={"max_drawdown": -0.2112})], invented),
                        ([], ok)]
    out = sub()(subject="q")
    assert "error" not in out and out["output"]["details"] == [f"Maximum drawdown -21.1% ({EV1})."]
    assert "does not appear in the evidence" in FakeAgent.calls[1]


def test_a_citation_at_the_end_of_a_multi_sentence_detail_covers_the_whole_line():
    line = "Replay: total return -11.5%. Worst point -21.1%, peak 2022-01-03. (ev-aaaaaaaaaa)"
    validate_finding(finding(details=[line]), [EV1], evidence=STRESS_EV)
