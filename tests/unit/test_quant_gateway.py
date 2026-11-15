"""Quant-tools Lambda behind the AgentCore Gateway: same evidence as a direct RiskTools call."""
from types import SimpleNamespace

import pytest

from app.tools import quant_gateway as qg

ROBERT = {"XIU.TO": 0.3, "XBB.TO": 0.3, "ZST.TO": 0.1, "RY.TO": 0.1, "FTS.TO": 0.1, "JNJ": 0.1}
ROBERT_NVDA = {**{k: v * 0.8 for k, v in ROBERT.items()}, "NVDA": 0.2}


def ctx(tool: str):
    """Shape of the Lambda context the Gateway sends (client_context.custom)."""
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}))


CASES = [
    ("metrics___metrics", {"tickers": ["NVDA", "XBB.TO"]}, lambda rt: rt.metrics(["NVDA", "XBB.TO"])),
    ("metrics___correlation", {"tickers": ["XIU.TO", "XBB.TO"], "start": "2022-01-01", "end": "2022-12-31"},
     lambda rt: rt.correlation(["XIU.TO", "XBB.TO"], start="2022-01-01", end="2022-12-31")),
    ("risk___risk_contribution", {"weights": ROBERT_NVDA}, lambda rt: rt.risk_contribution(ROBERT_NVDA)),
    ("risk___concentration", {"weights": ROBERT_NVDA}, lambda rt: rt.concentration(ROBERT_NVDA)),
    ("scenarios___stress", {"weights": ROBERT_NVDA, "window": "2022", "capital": 400000},
     lambda rt: rt.stress(ROBERT_NVDA, window="2022", capital=400000.0)),
    ("optimizer___max_position", {"base_weights": ROBERT, "asset": "NVDA", "max_drawdown_limit": 0.15},
     lambda rt: rt.max_position(ROBERT, "NVDA", 0.15)),
]


@pytest.mark.parametrize("tool, args, direct", CASES, ids=[c[0] for c in CASES])
def test_gateway_call_returns_the_same_evidence_as_a_direct_call(tool, args, direct):
    out = qg.handler(dict(args), ctx(tool))
    ref = direct(qg.tools())
    assert "error" not in out, out
    assert out["evidence_id"] == ref["evidence_id"]
    assert out["say"] == ref["say"]
    assert out["tool"] == tool.split("___")[1]
    assert {"evidence_id", "output", "say", "assumptions"} <= set(out)
    assert set(out) == set(ref)  # the full envelope passes through the Gateway untouched


def test_robert_nvdia_numbers_match_the_risk_subagent_run():
    out = qg.handler({"base_weights": ROBERT, "asset": "NVDA", "max_drawdown_limit": 0.15},
                     ctx("optimizer___max_position"))
    assert out["output"]["max_weight"] == pytest.approx(0.09)


def test_weights_also_accepted_as_a_list_of_pairs():
    as_list = [{"ticker": k, "weight": v} for k, v in ROBERT_NVDA.items()]
    a = qg.handler({"weights": as_list, "capital": 400000}, ctx("scenarios___stress"))
    b = qg.handler({"weights": ROBERT_NVDA, "capital": 400000}, ctx("scenarios___stress"))
    assert a["output"] == b["output"]


@pytest.mark.parametrize("tool, args", [
    ("metrics___metrics", {"tickers": ["NOT_A_TICKER"]}),
    ("scenarios___stress", {"weights": ROBERT, "window": "1929"}),
    ("risk___risk_contribution", {}),
])
def test_bad_arguments_return_an_error_the_agent_can_fix(tool, args):
    out = qg.handler(args, ctx(tool))
    assert "error" in out and "note" in out


def test_unknown_tool():
    out = qg.handler({}, ctx("quant___buy_now"))
    assert out["error"].startswith("Unknown tool") and "stress" in out["available"]


def test_schemas_use_only_fields_the_gateway_accepts():
    allowed = {"type", "properties", "required", "items", "description"}

    def walk(schema):
        assert set(schema) <= allowed, set(schema) - allowed
        for sub in schema.get("properties", {}).values():
            walk(sub)
        if "items" in schema:
            walk(schema["items"])

    for target, specs in qg.TARGETS.items():
        for spec in specs:
            walk(spec["inputSchema"])
            assert set(spec["inputSchema"]["required"]) <= set(spec["inputSchema"]["properties"])
    assert set(qg.TARGETS) == {"metrics", "risk", "scenarios", "optimizer"}
