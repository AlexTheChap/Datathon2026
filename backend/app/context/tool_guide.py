"""Per-task tool-calling guide, rendered into the orchestrator template via {{tools_block}}.

The @tool docstrings tell the model WHAT each tool does; this module tells it WHEN and IN WHAT
ORDER to call them for a given task (request.task). With an ExecutionPlan (box 4, L6), only the
sub-agents the plan enables are listed, followed by the plan itself (reasons + parameters), so the
model is never told about a sub-agent that is not registered for this run.

Delegation model: domain questions go to the sub-agents (each runs its own toolset and returns a
finding with nested evidence_ids); the orchestrator keeps governance_check and the
calibration/stress tools for synthesis.
"""
from __future__ import annotations

import json
from typing import Any, Optional

_DELEGATION_LINES: dict[str, str] = {
    "risk": "- Portfolio risk (stress, concentration, risk contribution, position sizing) → **`risk_subagent`**",
    "macro_scenarios": "- Macro environment / crisis scenarios → **`macro_subagent`**",
    "esg": "- ESG index membership & controversies → **`esg_subagent`**",
    "fundamentals": "- Documented filing facts (10-K/NBIM extracts) → **`fundamentals_subagent`**",
    "allocation": "- Building a hypothetical allocation → **`allocation_subagent`** (self-verifies via governance)",
    "news": "- News backdrop → **`news_subagent`** — SIMULATED content: always label it as fictional",
}
_DELEGATION_FOOTER = """Delegate domain questions to them; keep `governance_check` + `max_position`/`stress`
for your own synthesis, and cite every evidence_id (theirs and yours)."""

_STRATEGIES: dict[str, str] = {
    "risk_review": """### Strategy for "risk_review"
1. **`governance_check` first** on the proposed (or current) weights: its
   PASS / WARN / BLOCK verdict frames the whole answer.
2. Delegate the domain deep-dive to **`risk_subagent`** (brief it with the exact
   question and weights); optionally **`macro_subagent`** for the rate/crisis context.
3. Calibration stays with you: when the investor names an asset, call **`max_position`**
   with their risk band's loss limit, then re-run `governance_check` at the calibrated weight.
4. A BLOCK verdict is never negotiable: explain the conflict, propose the calibrated
   alternative, do not endorse the request as-is.""",
    "allocation_proposal": """### Strategy for "allocation_proposal"
1. Delegate construction to **`allocation_subagent`** with the profile details
   (risk band, horizon, exclusions) — it proposes weights and self-verifies to PASS.
2. Verify the returned weights yourself with **`governance_check`**.
3. **`stress`** the retained allocation with the profile's fictional capital
   (+ **`concentration`** to show true exposures).
4. Present the allocation as a hypothetical scenario with evidence, not a recommendation.""",
}

_GENERIC_HEAD = "### Available tools — general strategy"
_GENERIC_TAIL = """Direct quant tools (use for quick synthesis): `governance_check(weights)`,
`stress(weights, window, capital)`, `max_position(base_weights, asset, limit)`,
`metrics(tickers)`, `correlation(tickers)`, `concentration(weights)`,
`risk_contribution(weights)`.
Call `governance_check` on any allocation you present; cite every evidence_id."""


def _delegation(enabled: Optional[list[str]]) -> str:
    agents = list(_DELEGATION_LINES) if enabled is None else enabled
    if not agents:
        return "### Sub-agents\nNone in this run: use the direct quant tools only."
    lines = "\n".join(_DELEGATION_LINES[a] for a in agents)
    return f"### Delegation to sub-agents (each returns a finding + nested evidence_ids)\n{lines}\n{_DELEGATION_FOOTER}"


def _plan_block(plan: Any) -> str:
    lines = ["### Execution plan for this run (deterministic, decided before you)"]
    for a in plan.enabled_agents:
        params = f" — parameters: {json.dumps(a.parameters)}" if a.parameters else ""
        lines.append(f"- `{a.agent}`: {a.reason}{params}")
    if plan.disabled_agents:
        lines.append("- Not available in this run (not registered as tools, do not simulate them): "
                     + ", ".join(f"`{d.agent}`" for d in plan.disabled_agents))
    if plan.needs_clarification:
        lines.append("- The request was unclear: state the assumptions you make.")
    return "\n".join(lines)


def tools_block(task: str, plan: Any = None) -> str:
    """Calling guide for the task (generic guide for unknown tasks), restricted to the plan when given."""
    deleg = _delegation(plan.enabled_names if plan is not None else None)
    if task in _STRATEGIES:
        body = f"{_STRATEGIES[task]}\n\n{deleg}"
    else:
        body = f"{_GENERIC_HEAD}\n{deleg}\n{_GENERIC_TAIL}"
    return body if plan is None else f"{body}\n\n{_plan_block(plan)}"
