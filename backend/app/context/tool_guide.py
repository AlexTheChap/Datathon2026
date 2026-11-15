"""Per-task tool-calling guide, rendered into the orchestrator template via {{tools_block}}.

PURE DELEGATION: the orchestrator holds NO quantitative tools — only the deterministic
governance verdict. Every number comes from a sub-agent finding (each sub-agent owns its
domain toolset; its internal calls are traced in the ledger tagged with its name).

The @tool docstrings tell the model WHAT each tool does; this module tells it WHEN and IN
WHAT ORDER to call them for a given task (request.task). With an ExecutionPlan (box 4, L6),
only the sub-agents the plan enables are listed, followed by the plan itself (reasons +
parameters), so the model is never told about a sub-agent that is not registered for this
run. The orchestrator passes a structured payload to each sub-agent (subject + optional
weights/tickers — the investor context is attached automatically by render_brief).
"""
from __future__ import annotations

import json
from typing import Any, Optional

_DELEGATION_LINES: dict[str, str] = {
    "risk": "- Portfolio risk (stress, concentration, risk contribution, position sizing) → **`risk_subagent`**",
    "macro_scenarios": "- Macro environment / crisis scenarios → **`macro_subagent`**",
    "esg": "- ESG index membership & controversies → **`esg_subagent`**",
    "fundamentals": "- Documented filing facts (SEC 8-K events, 10-K/NBIM extracts) → **`fundamentals_subagent`**",
    "allocation": "- Building a hypothetical allocation → **`allocation_subagent`** (weights come from the optimizer tool, verified)",
    "news": ("- Dated news (SEC 8-K, rate decisions, press headlines, annual-report risk sections; published ≤ as_of) "
             "→ **`news_subagent`** — the corpus is incomplete: say so when it finds nothing; "
             "if its record says SIMULATED, label the content as fictional"),
}
_DELEGATION_FOOTER = """Give each sub-agent a precise `subject` (+ `weights` / `tickers` when relevant); calibration
questions belong in the subject (e.g. "...and the maximum NVDA weight respecting a -15% loss
limit"). You keep only `governance_check(weights)` (deterministic PASS/WARN/BLOCK).
Cite every evidence_id (the sub-agents' and your own governance check)."""

_STRATEGIES: dict[str, str] = {
    "risk_review": """### Strategy for "risk_review"
1. **`governance_check` first** on the proposed (or current) weights: its
   PASS / WARN / BLOCK verdict frames the whole answer.
2. Delegate the deep-dive: **`risk_subagent`** with a question-focused subject and the
   proposed weights; add **`macro_subagent`** when rates/crisis context matters.
3. Calibration: position sizing lives inside `risk_subagent` — ask for it in the subject
   (e.g. "...and the maximum NVDA weight respecting a -15% loss limit"), then run
   `governance_check` yourself on the calibrated weights.
4. A BLOCK verdict is never negotiable: explain the conflict, propose the calibrated
   alternative, do not endorse the request as-is.""",
    "allocation_proposal": """### Strategy for "allocation_proposal"
1. Delegate construction: **`allocation_subagent`** with the goal in the subject (risk band,
   horizon and exclusions are attached automatically) — its weights come from the
   optimizer tool and are already verified; if it returns none, no allocation satisfies the rules: say which.
2. Verify the returned weights yourself with **`governance_check`**.
3. Add **`risk_subagent`** / **`macro_subagent`** on the retained weights for the crisis
   context when useful.
4. Present the allocation as a hypothetical scenario with evidence, not a recommendation.""",
}

_GENERIC_HEAD = "### Available tools — general strategy"
_GENERIC_TAIL = """Workflow for any task: `governance_check` the relevant weights, delegate the domain
questions, synthesize with citations, and state limitations."""


def _delegation(enabled: Optional[list[str]]) -> str:
    agents = list(_DELEGATION_LINES) if enabled is None else enabled
    if not agents:
        return "### Sub-agents\nNone in this run: no analysis is possible (task out of scope)."
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
