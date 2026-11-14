"""Per-task tool-calling guide (spirit of the Plan resolver, node ④).

The @tool docstrings tell the model WHAT each tool does; this module tells it
WHEN and IN WHAT ORDER to call them for a given task (request.task). The
rendered block is injected into the orchestrator template via {{tools_block}}.

When the Plan resolver exists, `tools_block` will also filter by enabled
agents (enabled_agents) — same signature, one more source of truth.
"""
from __future__ import annotations

TOOL_GUIDES: dict[str, str] = {
    "risk_review": """### Strategy for "risk_review"
1. **`governance_check` first** on the proposed (or current) weights: its
   PASS / WARN / BLOCK verdict frames the whole answer.
2. **`stress`** on the same weights with the profile's fictional capital and the
   "2022" window: quantifies the crisis-scenario loss.
3. Depending on the request:
   - the investor names a specific asset ("what if I put X% in it?") → **`max_position`**
     with their risk band's loss limit (rules.yaml), then re-run `governance_check`
     at the calibrated weight;
   - "where is the risk?" → **`risk_contribution`** (risk share vs weight) and
     **`concentration`** (true exposures, ETFs decomposed);
   - comparing assets → **`metrics`** (+ `correlation` for several assets).
4. A BLOCK verdict is never negotiable: explain the conflict, propose the calibrated
   alternative (`max_position`), do not endorse the request as-is.""",
    "allocation_proposal": """### Strategy for "allocation_proposal"
1. **`metrics`** on plausible candidates (broad universe) to ground the proposal.
2. Build a hypothetical allocation consistent with the profile (risk band, horizon,
   declared ESG/crypto exclusions), then **`governance_check`**: iterate to PASS
   (or a motivated WARN) — never BLOCK.
3. **`stress`** the retained allocation (fictional capital, "2022") + **`concentration`**
   to show true exposures (ETFs decomposed).
4. Present the allocation as a hypothetical scenario with its evidence, not a
   recommendation: the investor decides.""",
}

GENERIC_GUIDE = """### Available tools — general strategy
- `governance_check(weights)`: PASS / WARN / BLOCK verdict against the profile and rules.yaml.
- `stress(weights, window, capital)`: crisis-window replay (2022 by default).
- `max_position(base_weights, asset, limit)`: largest share of an asset that respects
  the profile's loss tolerance.
- `metrics(tickers)` / `correlation(tickers)`: returns, volatility, max drawdown / correlations.
- `concentration(weights)`: true exposures by company, sector, class (ETFs decomposed).
- `risk_contribution(weights)`: risk share per position.
Call `governance_check` on any allocation you present; cite every evidence_id."""


def tools_block(task: str) -> str:
    """Calling guide for the task; falls back to the generic guide for unknown tasks."""
    return TOOL_GUIDES.get(task, GENERIC_GUIDE)
