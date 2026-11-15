"""Where each sub-agent's tools come from: AWS box ⑥ when configured, local fallback otherwise.

  quant_toolset()  -> Risk sub-agent (L12)
      AF_GATEWAY_URL set : [MCPClient] on the AgentCore Gateway; the agent discovers the 4 quant tools
                           (metrics___metrics, optimizer___metrics…) and calls them
                           over MCP (SigV4). The Lambda runs the same RiskTools code.
      unset              : the same 6 functions in-process (app.tools.agent_tools).
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


def gateway_enabled() -> bool:
    return bool(get_settings().gateway_url)


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


def local_quant_tools() -> list:
    from app.tools import agent_tools

    return [agent_tools.metrics, agent_tools.concentration,
            agent_tools.correlation, agent_tools.risk_contribution]


def quant_toolset() -> list:
    """Risk sub-agent toolset: the AgentCore Gateway (one MCP client per sub-agent run) or local functions."""
    s = get_settings()
    if not s.gateway_url:
        return local_quant_tools()
    from app.tools.gateway_client import strands_mcp_client

    log.info("risk tools via AgentCore Gateway %s", s.gateway_url)
    return [strands_mcp_client(s.gateway_url, s.aws_region)]


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
