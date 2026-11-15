"""Box ⑥ wiring: Risk → AgentCore Gateway, News → Knowledge Base, local fallbacks (no AWS, no LLM)."""
from types import SimpleNamespace

import pytest

from app.agents.subagents import base
from app.agents.subagents.news import KB_PROMPT, SIMULATED_PROMPT, news_subagent
from app.agents.subagents.risk import risk_subagent
from app.tools import toolsets

LOCAL_NAMES = ["metrics", "concentration", "correlation", "risk_contribution",
               "market_screen", "market_related"]


@pytest.fixture
def settings(monkeypatch):
    """Settings with no AWS box 6 by default; each test turns on what it needs."""
    s = SimpleNamespace(gateway_url=None, kb_id=None, aws_region="us-east-1",
                        model_subagent="test-model", news_dir=None)
    monkeypatch.setattr(toolsets, "get_settings", lambda: s)
    monkeypatch.setattr(base, "get_settings", lambda: s)
    return s


@pytest.fixture
def fake_agent(monkeypatch):
    """Replace the Strands Agent/BedrockModel: record what each sub-agent run receives."""
    seen = []

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self.kwargs = kwargs

        def __call__(self, brief):
            # A sub-agent with tools must call one (output contract): record one evidence and cite it.
            if self.kwargs.get("tools"):
                hook = self.kwargs["hooks"][0]
                hook.ledger.append(hook.run_id, {"tool": "t", "agent": hook.agent, "evidence_id": "ev-0000000001",
                                                 "payload": {"evidence_id": "ev-0000000001", "tool": "t"}})
                return '{"headline": "h", "details": ["fact ev-0000000001"], "severity": "info"}'
            return '{"headline": "h", "details": [], "severity": "info"}'

    monkeypatch.setattr(base, "Agent", FakeAgent)
    monkeypatch.setattr(base, "BedrockModel", lambda **kw: None)
    return seen


def test_local_quant_tools_without_gateway(settings):
    assert [t.tool_name for t in toolsets.quant_toolset()] == LOCAL_NAMES


def test_gateway_gives_an_mcp_client_without_connecting(settings):
    from strands.tools.mcp import MCPClient

    settings.gateway_url = "https://gw.example/mcp"
    tools = toolsets.quant_toolset()
    assert len(tools) == 1 and isinstance(tools[0], MCPClient)


def test_news_toolset(settings):
    assert toolsets.news_toolset() == []
    settings.kb_id = "KB123"
    assert [t.tool_name for t in toolsets.news_toolset()] == ["search_news"]


def test_risk_subagent_uses_gateway_when_configured(settings, fake_agent):
    from strands.tools.mcp import MCPClient

    risk_subagent._tool_func("NVDA at 20%")
    assert [t.tool_name for t in fake_agent[-1]["tools"]] == LOCAL_NAMES
    settings.gateway_url = "https://gw.example/mcp"
    risk_subagent._tool_func("NVDA at 20%")
    assert isinstance(fake_agent[-1]["tools"][0], MCPClient)


def test_news_subagent_simulated_without_kb(settings, fake_agent):
    ev = news_subagent._tool_func("NVDA news")
    assert fake_agent[-1]["system_prompt"] == SIMULATED_PROMPT and fake_agent[-1]["tools"] == []
    assert any("SIMULATED" in a for a in ev["assumptions"])


def test_news_subagent_real_with_kb(settings, fake_agent):
    settings.kb_id = "KB123"
    ev = news_subagent._tool_func("NVDA news")
    assert fake_agent[-1]["system_prompt"] == KB_PROMPT
    assert [t.tool_name for t in fake_agent[-1]["tools"]] == ["search_news"]
    assert all("SIMULATED" not in a for a in ev["assumptions"])


def test_orchestrator_facing_specs_unchanged():
    for sub in (risk_subagent, news_subagent):
        assert {"subject", "weights", "tickers"} <= set(sub.tool_spec["inputSchema"]["json"]["properties"])


def test_local_news_corpus_is_used_without_a_kb(settings, fake_agent, tmp_path):
    from app.agents.subagents.news import LOCAL_PROMPT
    (tmp_path / "news_index.csv").write_text("event_id,published_at\n")
    settings.news_dir, settings.abs_path = tmp_path, (lambda p: p)
    assert toolsets.news_mode() == "local"
    assert [t.tool_name for t in toolsets.news_toolset()] == ["news_search"]
    ev = news_subagent._tool_func("NVDA news")
    assert fake_agent[-1]["system_prompt"] == LOCAL_PROMPT
    assert all("SIMULATED" not in a for a in ev["assumptions"])
    settings.kb_id = "KB123"
    assert toolsets.news_mode() == "kb"                      # the Knowledge Base wins when configured
