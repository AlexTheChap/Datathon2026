"""Per-task tool-calling guide (spirit of the Plan resolver, node ④).

PURE DELEGATION: the orchestrator holds NO quantitative tools — only the
deterministic governance verdict. Every number comes from a sub-agent finding
(each sub-agent owns its domain tools; its internal calls are traced in the
ledger tagged with its name). The orchestrator decides WHICH sub-agents to
call, passes a precise `subject` (+ optional weights/tickers — the investor
context is attached automatically), and synthesizes the cited answer.
"""
from __future__ import annotations

DELEGATION = """### Delegation to sub-agents (your only source of numbers)
Each sub-agent owns the quantitative tools in its domain and returns ONE finding
with nested evidence_ids. Give it a precise `subject` (the question) and, when
relevant, `weights` / `tickers` — the investor profile and portfolio context are
attached automatically; do NOT repeat them in the subject.
- `risk_subagent(subject, weights, tickers)`: stress replay, concentration, risk
  contribution, per-asset metrics, calibrated position limits (max sizing)
- `macro_subagent(subject, weights, tickers)`: macro snapshot, rate sensitivity,
  crisis scenarios (2022 replay)
- `esg_subagent(subject, tickers)`: ESG index membership + documented controversies
- `fundamentals_subagent(subject, tickers)`: documented filing facts (says no_data
  when none — never invents)
- `allocation_subagent(subject, weights)`: builds a hypothetical allocation and
  self-verifies it against the rules until PASS
- `news_subagent(subject, tickers)`: SIMULATED digest — always label it fictional
You keep only `governance_check(weights)` (deterministic PASS/WARN/BLOCK).
Cite every evidence_id (the sub-agents' and your own governance check)."""

TOOL_GUIDES: dict[str, str] = {
    "risk_review": f"""### Strategy for "risk_review"
1. **`governance_check` first** on the proposed (or current) weights: its
   PASS / WARN / BLOCK verdict frames the whole answer.
2. Delegate the deep-dive: **`risk_subagent`** with a question-focused subject and
   the proposed weights; add **`macro_subagent`** when rates/crisis context matters.
3. Calibration: position sizing lives inside `risk_subagent` — ask for it in the
   subject (e.g. "...and the maximum NVDA weight respecting a -15% loss limit"),
   then run `governance_check` yourself on the calibrated weights.
4. A BLOCK verdict is never negotiable: explain the conflict, propose the calibrated
   alternative, do not endorse the request as-is.

{DELEGATION}""",
    "allocation_proposal": f"""### Strategy for "allocation_proposal"
1. Delegate construction: **`allocation_subagent`** with the goal in the subject
   (risk band, horizon and exclusions are attached automatically) — it returns
   weights already self-verified to PASS.
2. Verify the returned weights yourself with **`governance_check`**.
3. Add **`risk_subagent`** / **`macro_subagent`** on the retained weights for the
   crisis context when useful.
4. Present the allocation as a hypothetical scenario with evidence, not a recommendation.

{DELEGATION}""",
}

GENERIC_GUIDE = f"""### Available tools — general strategy
{DELEGATION}
Workflow for any task: `governance_check` the relevant weights, delegate the
domain questions, synthesize with citations, and state limitations."""


def tools_block(task: str) -> str:
    """Calling guide for the task; falls back to the generic guide for unknown tasks."""
    return TOOL_GUIDES.get(task, GENERIC_GUIDE)
