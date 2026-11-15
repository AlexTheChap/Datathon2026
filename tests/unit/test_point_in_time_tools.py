"""Point-in-time guarantees of the agent tools: the model can never push a tool past the run's as_of.
No AWS, no LLM."""
from __future__ import annotations

import pytest
from app.core.config import Settings
from app.tools import agent_tools as at
from app.tools.analysis_tools import esg_screen, macro_snapshot
from app.tools.context import (
    current_as_of,
    current_profile,
    default_as_of,
    effective_as_of,
    persona_profile,
    run_as_of,
)

WEIGHTS = {"XIU.TO": 0.3, "XBB.TO": 0.3, "ZST.TO": 0.1, "RY.TO": 0.1, "FTS.TO": 0.1, "NVDA": 0.1}


@pytest.fixture
def run(make_profile):
    """A run for Robert with as_of 2022-06-30 (ContextVars restored afterwards)."""
    t1 = current_profile.set(make_profile("robert").model_dump())
    t2 = current_as_of.set("2022-06-30")
    yield
    current_as_of.reset(t2)
    current_profile.reset(t1)


def test_default_as_of_is_the_last_curated_date_not_a_constant():
    assert default_as_of() == "2026-11-12"          # last row of data/curated/calendar.csv
    assert Settings(default_as_of="2024-01-02").default_as_of == "2024-01-02"


def test_orchestrator_default_model_id_is_a_valid_bedrock_identifier():
    """The bare id 'anthropic.claude-sonnet-4-5-v1:0' is rejected by Bedrock (ValidationException)."""
    assert Settings().model_orchestrator.split(".")[0] in {"us", "eu", "global", "apac"}
    assert Settings().model_subagent.split(".")[0] in {"us", "eu", "global", "apac"}


def test_effective_as_of_can_only_go_earlier(run):
    assert run_as_of() == "2022-06-30"
    assert effective_as_of() == "2022-06-30"
    assert effective_as_of("2030-01-01") == "2022-06-30"      # later than the run: capped
    assert effective_as_of("2022-03-01") == "2022-03-01"      # earlier: allowed


def test_run_without_as_of_uses_the_last_curated_date():
    token = current_as_of.set(None)
    try:
        assert run_as_of() == default_as_of()
    finally:
        current_as_of.reset(token)


def test_persona_profile_matches_the_governance_format(run):
    p = persona_profile()
    assert p["id"] == p["actor_id"] and p["horizon_years"] == [3, 5]


def test_metrics_and_correlation_are_capped_at_the_run_date(run):
    ev = at.metrics(["NVDA"], as_of="2030-01-01")
    assert ev["as_of"] == "2022-06-30" and ev["period"].endswith("2022-06-30")
    assert at.correlation(["NVDA", "XIU.TO"], as_of="2030-01-01")["period"].endswith("2022-06-30")
    assert at.risk_contribution(WEIGHTS, as_of="2030-01-01")["period"].endswith("2022-06-30")


def test_stress_window_is_cut_at_the_run_date(run):
    ev = at.stress(WEIGHTS)                       # the 2022 window ends 2022-12-30
    assert ev["period"] == "2022-01-03/2022-06-30"


def test_max_position_uses_the_cut_window(run):
    ev = at.max_position({"XIU.TO": 0.6, "XBB.TO": 0.4}, "NVDA", 0.15)
    assert ev["period"].endswith("2022-06-30")


def test_governance_check_is_point_in_time_and_citable(run):
    ev = at.governance_check(WEIGHTS)
    assert ev["evidence_id"].startswith("ev-") and ev["tool"] == "governance_check"
    assert ev["as_of"] == "2022-06-30" and ev["output"]["verdict"] == ev["verdict"]
    assert ev["verdict"] in ev["say"]
    assert at.governance_check(WEIGHTS)["evidence_id"] == ev["evidence_id"]      # stable id
    assert at.governance_check({**WEIGHTS, "NVDA": 0.0, "XIU.TO": 0.4})["evidence_id"] != ev["evidence_id"]


def test_governance_check_without_profile_is_an_error():
    token = current_profile.set(None)
    try:
        assert "error" in at.governance_check(WEIGHTS)
        assert "error" in at.optimize(WEIGHTS)
    finally:
        current_profile.reset(token)


def test_optimize_tool_runs_on_the_cut_data_and_returns_evidence(run):
    ev = at.optimize({**WEIGHTS, "NVDA": 0.2})
    assert ev["tool"] == "optimize" and ev["as_of"] == "2022-06-30"
    assert ev["output"]["status"] == "ok" and ev["output"]["weights"]["NVDA"] <= 0.05
    assert ev["output"]["verification"]["governance_verdict"] != "BLOCK"


def test_tool_errors_are_dicts_not_exceptions(run):
    assert "error" in at.metrics(["NOT-A-TICKER"])
    assert "error" in at.optimize({"NOT-A-TICKER": 1.0})


def test_macro_snapshot_never_looks_past_the_run_date(run):
    ev = macro_snapshot("2030-01-01")
    assert ev["output"]["date"] <= "2022-06-30"
    assert macro_snapshot()["output"]["date"] <= "2022-06-30"


def test_esg_screen_hides_controversies_published_after_the_run_date(run):
    after = esg_screen(["CAT", "TD.TO"], as_of="2030-01-01")        # run date 2022-06-30 < their dates
    assert after["output"]["controversies"] == []
    token = current_as_of.set("2026-11-12")
    try:
        full = esg_screen(["CAT", "TD.TO"])
    finally:
        current_as_of.reset(token)
    assert {c["ticker"] for c in full["output"]["controversies"]} == {"CAT", "TD.TO"}
    assert full["evidence_id"] != after["evidence_id"]
