"""Per-task tool-calling guide (spirit of the Plan resolver, node ④).

The @tool docstrings tell the model WHAT each tool does; this module tells it
WHEN and IN WHAT ORDER to call them for a given task (request.task). The
rendered block is injected into the orchestrator template via {{tools_block}}.

Delegation model: domain questions go to the sub-agents (each runs its own
toolset and returns a finding with nested evidence_ids); the orchestrator keeps
governance_check and the calibration/stress tools for synthesis. When the Plan
resolver exists, `tools_block` will also read the ExecutionPlan's enabled agents.
"""
from __future__ import annotations

DELEGATION = """### Delegation to sub-agents (each returns a finding + nested evidence_ids)
- Portfolio risk (stress, concentration, risk contribution, position sizing) → **`risk_subagent`**
- Macro environment / crisis scenarios → **`macro_subagent`**
- ESG index membership & controversies → **`esg_subagent`**
- Documented filing facts (10-K/NBIM extracts) → **`fundamentals_subagent`**
- Building a hypothetical allocation → **`allocation_subagent`** (self-verifies via governance)
- News backdrop → **`news_subagent`** — SIMULATED content: always label it as fictional
Delegate domain questions to them; keep `governance_check` + `max_position`/`stress`
for your own synthesis, and cite every evidence_id (theirs and yours)."""

TOOL_GUIDES: dict[str, str] = {
    "risk_review": f"""### Strategy for "risk_review"
1. **`governance_check` first** on the proposed (or current) weights: its
   PASS / WARN / BLOCK verdict frames the whole answer.
2. Delegate the domain deep-dive to **`risk_subagent`** (brief it with the exact
   question and weights); optionally **`macro_subagent`** for the rate/crisis context.
3. Calibration stays with you: when the investor names an asset, call **`max_position`**
   with their risk band's loss limit, then re-run `governance_check` at the calibrated weight.
4. A BLOCK verdict is never negotiable: explain the conflict, propose the calibrated
   alternative, do not endorse the request as-is.

{DELEGATION}""",
    "allocation_proposal": f"""### Strategy for "allocation_proposal"
1. Delegate construction to **`allocation_subagent`** with the profile details
   (risk band, horizon, exclusions) — it proposes weights and self-verifies to PASS.
2. Verify the returned weights yourself with **`governance_check`**.
3. **`stress`** the retained allocation with the profile's fictional capital
   (+ **`concentration`** to show true exposures).
4. Present the allocation as a hypothetical scenario with evidence, not a recommendation.

{DELEGATION}""",
}

GENERIC_GUIDE = f"""### Available tools — general strategy
{DELEGATION}
Direct quant tools (use for quick synthesis): `governance_check(weights)`,
`stress(weights, window, capital)`, `max_position(base_weights, asset, limit)`,
`metrics(tickers)`, `correlation(tickers)`, `concentration(weights)`,
`risk_contribution(weights)`.
Call `governance_check` on any allocation you present; cite every evidence_id."""


def tools_block(task: str) -> str:
    """Calling guide for the task; falls back to the generic guide for unknown tasks."""
    return TOOL_GUIDES.get(task, GENERIC_GUIDE)
