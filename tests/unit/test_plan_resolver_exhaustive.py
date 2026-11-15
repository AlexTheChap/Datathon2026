"""Exhaustive check of the Plan resolver: EVERY input combination is compared with an oracle
written here, independently of core/plan_registry.yaml and plan_resolver.py.

If someone changes the registry or the code and the behaviour changes, this test fails: update the
oracle ON PURPOSE (and the diagram if a flow changes).

Space covered: 7 tasks × 64 profile toggle combinations × include_rag × 4 profile types = 1,792 plans.
"""
import itertools
import json

from app.context import resolve_plan
from app.schemas import TASKS
from app.schemas.execution_plan import AGENTS, ExecutionPlan

# ---------- Oracle: the specification, written by hand ----------

REQUIRED = {
    "risk_review": {"risk"},
    "scenario_analysis": {"macro_scenarios", "risk"},
    "allocation_proposal": {"allocation", "risk"},
    "esg_screen": {"esg"},
    "fundamentals_review": {"fundamentals"},
    "news_review": {"news"},
    "out_of_scope": set(),
}


def oracle(profile, request) -> set[str]:
    if request.task == "out_of_scope":
        return set()
    toggles = profile.agent_toggles.model_dump()
    optional = set()
    if request.toggles.include_rag:
        optional.add("news")
    if any("esg" in p.lower().split() for p in profile.preferences):
        optional.add("esg")
    forced = {"esg"} if any(e.type in ("controversy", "esg_activity") for e in profile.exclusions) else set()
    return REQUIRED[request.task] | forced | {a for a in optional if toggles[a]}


def test_oracle_covers_every_task():
    assert set(REQUIRED) == set(TASKS)


# ---------- Sweep ----------

PROFILE_VARIANTS = {
    "amira": {},                                   # no ESG preference or exclusion
    "mateo": {},                                   # ESG preference + controversy exclusion
    "mateo_pref_only": {"exclusions": []},         # ESG preference only
    "robert": {},                                  # crypto exclusion (asset_class): adds nothing
}


def _all_cases(make_profile, make_request):
    for (variant, overrides), bits in itertools.product(
        PROFILE_VARIANTS.items(), itertools.product([True, False], repeat=len(AGENTS))
    ):
        persona = variant.split("_")[0]
        profile = make_profile(persona, agent_toggles=dict(zip(AGENTS, bits)), **overrides)
        for task, rag in itertools.product(TASKS, [True, False]):
            request = make_request(
                f"persona-{persona}", task,
                toggles={"include_rag": rag},
            )
            yield variant, profile, request


def test_every_combination_matches_the_specification(make_profile, make_request):
    failures, n = [], 0
    for variant, profile, request in _all_cases(make_profile, make_request):
        n += 1
        plan = resolve_plan(profile, request)
        toggles = profile.agent_toggles.model_dump()
        enabled = set(plan.enabled_names)
        ctx = f"{variant} task={request.task} toggles={toggles} req={request.toggles.model_dump()}"

        expected = oracle(profile, request)
        if enabled != expected:
            failures.append(f"{ctx}: enabled {sorted(enabled)} != expected {sorted(expected)}")
            continue

        # Structural invariants
        if plan.run_governance != bool(enabled):
            failures.append(f"{ctx}: run_governance={plan.run_governance}")
        if not plan.run_explainer:
            failures.append(f"{ctx}: run_explainer must always be true")
        for a in plan.enabled_agents:
            if a.forced != (not toggles[a.agent]):
                failures.append(f"{ctx}: {a.agent}.forced={a.forced} but toggle={toggles[a.agent]}")
            if not a.reason:
                failures.append(f"{ctx}: {a.agent} has no reason")
        for d in plan.disabled_agents:
            if not d.reason:
                failures.append(f"{ctx}: disabled {d.agent} has no reason")
        # An agent the user switched off but that is enabled must ALWAYS be reported
        for a in plan.enabled_agents:
            if a.forced and not any(w.startswith(f"{a.agent} enabled although") for w in plan.warnings):
                failures.append(f"{ctx}: forced {a.agent} without warning")

        # Serialization: the plan crosses the InvokeAgentRuntime payload as JSON
        if ExecutionPlan.model_validate(json.loads(json.dumps(plan.model_dump(mode="json")))) != plan:
            failures.append(f"{ctx}: JSON round-trip changed the plan")

    assert n == 4 * 64 * len(TASKS) * 2
    assert not failures, f"{len(failures)} / {n} cases fail, first ones:\n" + "\n".join(failures[:10])


def test_turning_a_toggle_off_never_enables_anything(make_profile, make_request):
    """Monotonicity: switching an agent off in the profile can only remove agents, never add one."""
    for task in TASKS:
        for agent in AGENTS:
            req = make_request("persona-mateo", task, toggles={"stress_tests": True, "include_rag": True})
            on = set(resolve_plan(make_profile("mateo"), req).enabled_names)
            off = set(resolve_plan(make_profile("mateo", agent_toggles={agent: False}), req).enabled_names)
            assert off <= on, (task, agent)
            assert on - off <= {agent}, (task, agent)
