"""ESG sub-agent — index membership and controversy signals (architecture node ⑤).

The toolset resolves per run (app.tools.toolsets.esg_toolset): the Gateway's esg___esg_screen — the
Lambda reads the LIVE market graph (Neptune, freshness-gated) — or the local curated esg_screen.
"""
from __future__ import annotations

from app.agents.subagents.base import make_subagent_tool
from app.tools.toolsets import esg_toolset

SYSTEM_PROMPT = """You are the ESG sub-agent of AlphaForge.

Using ONLY the esg_screen tool, assess the ESG/controversy dimension of the
brief. Distinguish strictly:
- PREFERENCE signal: stock absent from the reference ESG index (never a ban;
  the reason is not published).
- CONSTRAINT: controversy lines with status "verified" (reviewed) — severity
  tells you how strong. Lines that are not verified must be reported as
  "unverified", never as violations.

Return ONLY a JSON object:
{
  "headline": "<one-sentence ESG takeaway>",
  "details": ["<fact with its evidence_id>"],
  "severity": "info" | "warn" | "block",
  "flagged": [{"ticker": "...", "kind": "index" | "controversy", "status": "..."}]
}
No investment advice.
"""

esg_subagent = make_subagent_tool(
    "esg_subagent",
    SYSTEM_PROMPT,
    tools=esg_toolset,   # resolved per run: Gateway (live graph) or local curated esg_screen
    description="""ESG screening (reference-index membership + documented controversies
    with review status) from the ESG sub-agent, grounded in curated ESGV/FTSE
    screens and 10-K/NBIM extracts. Cite its nested evidence_ids.

    Args:
        subject: e.g. "ESG/controversy check for the investor's constraints".
        tickers: assets to screen, e.g. ["ENB.TO", "CNQ.TO", "XOM"].
    """,
)
