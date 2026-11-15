"""Where each sub-agent's tools come from: AWS box ⑥ when configured, local fallback otherwise.

  quant_toolset()  -> Risk sub-agent (L12)
      AF_GATEWAY_URL set : [MCPClient] on the AgentCore Gateway; the agent discovers the quant tools
                           (metrics___metrics, market___screen…) and calls them
                           over MCP (SigV4). The Lambda runs the same RiskTools code.
      unset              : the same functions in-process (app.tools.agent_tools).
  esg_toolset()    -> ESG sub-agent: the Gateway's esg___esg_screen (the Lambda reads the live market
                      graph in the VPC) or the local curated esg_screen otherwise.
  news_mode() / news_toolset() -> News sub-agent (L13)
      AF_KB_ID set       : "kb"        search_news on the Bedrock Knowledge Base (S3 Vectors), published <= as_of.
      news_index.csv     : "local"     news_search on the same dated corpus in-process (app.tools.news_tools).
      neither            : "simulated" SIMULATED placeholder (labeled fictional end to end).

Both are called at sub-agent RUN time (not import time), so tests and offline runs never connect to AWS.
Either way the tools return the same evidence envelope, so the EvidenceHook (L15) records identical evidence.
"""
from __future__ import annotations

import logging

from app.core.config import get_settings

log = logging.getLogger(__name__)


def news_kb_enabled() -> bool:
    return bool(get_settings().kb_id)


def local_news_available() -> bool:
    """The dated corpus built by scripts/ingest_news.py is present (settings without news_dir: no)."""
    s = get_settings()
    news_dir = getattr(s, "news_dir", None)
    return bool(news_dir) and (s.abs_path(news_dir) / "news_index.csv").is_file()


def news_mode() -> str:
    """"kb" (Knowledge Base), "local" (news_index.csv in-process) or "simulated" (no corpus)."""
    if news_kb_enabled():
        return "kb"
    return "local" if local_news_available() else "simulated"


def projection_tools() -> list:
    """Projection + volatility-trend tools: in-process in both modes (they read the run's profile for the horizon
    and the capital, which the Gateway Lambda does not see; not deployed as a Gateway target yet)."""
    from app.tools import agent_tools

    return [agent_tools.projection, agent_tools.vol_forecast]


def local_quant_tools() -> list:
    from app.tools import agent_tools

    return [agent_tools.metrics, agent_tools.concentration,
            agent_tools.correlation, agent_tools.risk_contribution,
            agent_tools.market_screen, agent_tools.market_related, *projection_tools()]


_mcp_clients: dict[str, object] = {}


def _gateway_client(url: str, region: str):
    """One MCPClient per gateway URL, SHARED across sub-agents and runs. Construction is free
    (no I/O); the initialize handshake + tools/list happen once and stay warm — the registry
    restarts the client transparently whenever its last consumer unregisters."""
    client = _mcp_clients.get(url)
    if client is None:
        from app.tools.gateway_client import strands_mcp_client

        client = strands_mcp_client(url, region)
        _mcp_clients[url] = client
        log.info("gateway MCP client created (shared) for %s", url)
    return client


def quant_toolset() -> list:
    """Risk sub-agent toolset: the AgentCore Gateway (one SHARED MCP client) or local functions,
    plus the in-process projection tools either way."""
    s = get_settings()
    if not s.gateway_url:
        return local_quant_tools()

    log.info("risk tools via AgentCore Gateway %s", s.gateway_url)
    return [_gateway_client(s.gateway_url, s.aws_region), *projection_tools()]


def esg_toolset() -> list:
    """ESG sub-agent toolset: the Gateway's esg_screen (the Lambda reads the live market graph in the VPC)
    or the local curated esg_screen (backed by the synced market cache when fresh)."""
    s = get_settings()
    if not s.gateway_url:
        from app.tools.analysis_tools import esg_screen

        return [esg_screen]

    log.info("esg tools via AgentCore Gateway %s", s.gateway_url)
    return [_gateway_client(s.gateway_url, s.aws_region)]


def news_toolset() -> list:
    """News sub-agent toolset: search_news on the Knowledge Base, news_search on the local corpus, or nothing
    (SIMULATED mode)."""
    mode = news_mode()
    if mode == "kb":
        from app.tools.news_kb import as_strands_tool
        return [as_strands_tool(get_settings().kb_id)]
    if mode == "local":
        from app.tools.news_tools import news_search
        return [news_search]
    return []
