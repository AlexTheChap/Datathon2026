"""Allocation sub-agent — hypothetical allocation built by the optimizer tool, never by the LLM (node ⑤)."""
from __future__ import annotations

from app.agents.subagents.base import FindingError, evidence_of, make_subagent_tool
from app.tools import agent_tools

SYSTEM_PROMPT = """You are the Allocation sub-agent of AlphaForge.
You never choose final weights yourself: the `optimize` tool does, and it is the only source of weights.
Procedure:
1. Pick a STARTING target: the portfolio in the brief if there is one, otherwise a simple hypothetical mix of
   universe tickers that fits the investor's risk band and horizon (this is only the starting point).
2. If the brief needs assets beyond the portfolio's tickers, pick a FEW candidates with the market-graph
   tools (`market_screen` for a class/profile, e.g. asset_class="etf_bond" sorted by ann_vol, esg or
   controversy filters; `market_related` for one asset's details) — make those independent screens together
   in ONE turn. Then call `optimize` with that target and
   those `candidates`: it returns the smallest change that satisfies EVERY rule of the profile, already
   verified with the governance rules. A candidate that optimize drops is not in the proposal.
3. If its status is "ok": copy `output.weights` EXACTLY (do not round, add or remove a ticker). Read the changes,
   the active constraints and the verification: that verification IS the governance verdict — never call
   `governance_check` again on weights `optimize` already verified.
4. Call `concentration` or `metrics` ONLY when the answer needs a number the optimize result does not contain
   (e.g. the HHI of the final mix). Answering from the optimize result alone is the normal case, not a shortcut.
5. If its status is "infeasible": return NO weights (an empty object) and explain which rules conflict.
Every number you state must come from a tool result, with its evidence_id.
Return ONLY a JSON object:
{
  "headline": "<one-sentence summary of the proposal>",
  "weights": {"<ticker>": <weight>, ...},
  "governance": {"verdict": "PASS" | "WARN" | "BLOCK" | null, "evidence_id": "ev-..."},
  "details": ["<what changed and why, with evidence_id>"],
  "severity": "info" | "warn" | "block"
}
Use "block" severity when no allocation satisfies the rules. This is a hypothetical scenario, not investment advice.
No return forecasts.
"""


def _validate_allocation(finding: dict, own: list[dict]) -> None:
    """The weights in the finding are the weights returned by the last successful `optimize` call."""
    optimize = [ev for r in own if (ev := evidence_of(r)) and ev.get("tool") == "optimize"]
    if not optimize:
        raise FindingError("you must call the `optimize` tool: weights come from it, never from you")
    out = optimize[-1].get("output") or {}
    weights = finding.get("weights") or {}
    if out.get("status") != "ok":
        if weights:
            raise FindingError("optimize returned no feasible allocation: `weights` must be an empty object")
        return
    expected = out.get("weights") or {}
    if set(weights) != set(expected) or any(abs(weights[t] - expected[t]) > 1e-4 for t in expected):
        raise FindingError("`weights` must be copied exactly from the last optimize result "
                           f"(evidence {optimize[-1]['evidence_id']}): {expected}")


allocation_subagent = make_subagent_tool(
    "allocation_subagent",
    SYSTEM_PROMPT,
    tools=[agent_tools.optimize, agent_tools.concentration, agent_tools.metrics,
           agent_tools.governance_check, agent_tools.market_screen, agent_tools.market_related],
    extra_validator=_validate_allocation,
    description="""Hypothetical portfolio construction from the Allocation sub-agent: the optimizer tool finds the
    smallest change to a target that satisfies every rule of the profile and verifies it (governance).
    Returns the weights (or none, with the conflicting rules, when no allocation satisfies the rules). Cite its
    nested evidence_ids.
    Args:
        subject: the goal, e.g. "balanced growth/stability avoiding controversial companies".
        weights: optional current or requested portfolio to adjust (investor context attached automatically).
        tickers: optional extra assets that may be used.
    """,
)
