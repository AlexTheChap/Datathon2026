"""Explainer (box ⑤ EXP): evidence catalogue, claim check, answer paths. No AWS: a fake client plays Bedrock,
the evidence is a real run's ledger (Robert, "20% in Nvidia") saved in tests/fixtures."""
import json
import types
from pathlib import Path

import pytest

from app.agents.claim_check import check, context_numbers, stated_numbers
from app.agents.evidence import build_catalog, governance_items
from app.agents.explainer import SHORT_ANSWER_REMOVED, explain
from app.context import resolve_context
from app.context.prompt_loader import LocalPromptSource
from app.core.config import Settings
from app.schemas import Request
from app.schemas.execution_plan import ExecutionPlan
from app.schemas.prompt_ref import PromptRef
from app.schemas.run_result import ExplainerDraft

LEDGER = [json.loads(line) for line in
          (Path(__file__).resolve().parents[1] / "fixtures" / "ledger_robert_nvda.jsonl").read_text().splitlines()]
SETTINGS = Settings(model_explainer="test-model", explainer_effort="low")


@pytest.fixture(scope="module")
def catalog():
    return build_catalog(LEDGER)


def make_run(make_profile, task="allocation_proposal", **profile_overrides):
    profile = make_profile("robert", **profile_overrides)
    request = Request(request_id="req-x", actor_id=profile.actor_id, task=task,
                      user_message="I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
                      positions=[{"ticker": "NVDA", "weight": 0.2}], toggles={"include_rag": True},
                      as_of="2026-11-12", intake_confidence=0.9)
    ctx = resolve_context(profile, request, LocalPromptSource())
    return types.SimpleNamespace(profile=profile, request=request, execution_plan=ExecutionPlan(**ctx["execution_plan"]),
                                 prompt_ref=PromptRef(**ctx["prompt_ref"]))


GOOD = {
    "short_answer": "A 20% Nvidia position is blocked: the 2022 replay's worst point is -24.2%, past the -15% limit.",
    "short_answer_evidence": ["ev-c5f01b9379", "ev-9de6c73e44"],
    "key_points": [{"text": "Nvidia is 20% of the weight but about 78% of the risk.", "evidence": ["ev-4a54cab2ec"]},
                   {"text": "That mix would have lost about $96,712 on $400,000 at its worst point.", "evidence": ["ev-c5f01b9379"]}],
    "checks": [{"allocation": "Your idea: Nvidia at 20%", "verdict": "BLOCK", "text": "Stress loss -21.1% vs -15%.",
                "evidence": ["ev-9de6c73e44"]}],
    "limitations": [{"text": "Only one crisis, 2022, is in the data.", "evidence": []}],
    "glossary": [{"term": "Maximum drawdown", "meaning": "The biggest fall from a peak."}],
    "clarifying_question": None,
}


def draft(**changes):
    return ExplainerDraft(**{**GOOD, **changes})


class FakeClient:
    """messages.parse returns the queued drafts (or raises), and records every request."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        if a == "refusal":
            return types.SimpleNamespace(stop_reason="refusal", parsed_output=None)
        return types.SimpleNamespace(stop_reason="end_turn", parsed_output=a)


def run_explain(run, client, orchestrator=None, records=LEDGER):
    return explain(run, orchestrator or {"status": "DONE", "summary": "draft notes"}, records, client=client,
                   settings=SETTINGS, prompt_source=LocalPromptSource())


# ---------- Evidence catalogue ----------

def test_catalogue_finds_nested_ids_the_ledger_list_misses(catalog):
    for cited in ("ev-9de6c73e44", "ev-aae1dc8931", "ev-5173e5ad06"):  # cited by the real answer, only nested
        assert cited in catalog and "MAX_STRESS_LOSS" in catalog[cited].say
    assert catalog["ev-9de6c73e44"].parent == "E-015" or catalog["ev-9de6c73e44"].parent  # linked to its verdict
    assert {g.verdict for g in governance_items(catalog)} >= {"BLOCK", "PASS"}


def test_model_view_has_no_raw_series(catalog):
    view = json.dumps([i.for_model() for i in catalog.values()])
    assert len(view) < 40_000 and "ev-c5f01b9379" in view


# ---------- Claim check ----------

def test_numbers_in_text_are_read_with_their_units():
    found = {(s.raw, s.kind) for s in stated_numbers("lost -24% (-$97k on $400,000) in 2022 from 2021-11-29; 3 checks; ratio 1.33")}
    assert found == {("-24%", "pct"), ("-$97k", "money"), ("$400,000", "money"), ("1.33", "plain")}


def test_backed_claims_pass_and_invented_ones_fail(catalog):
    ctx = context_numbers({"capital": 400000}, {"user_message": "20% in Nvidia"})
    issues, unknown = check(draft(), catalog, ctx)
    assert issues == [] and unknown == 0
    bad = draft(key_points=[{"text": "Nvidia returned +35% in 2022.", "evidence": ["ev-c5f01b9379"]}])
    issues, _ = check(bad, catalog, ctx)
    assert [i.section for i in issues] == ["key_points"] and "35%" in issues[0].reason


def test_unknown_ids_and_wrong_verdicts_are_caught(catalog):
    d = draft(key_points=[{"text": "Volatility is 51% a year.", "evidence": ["ev-0000000000"]}],
              checks=[{"allocation": "Your idea", "verdict": "PASS", "text": "Fine.", "evidence": ["ev-9de6c73e44"]}])
    issues, unknown = check(d, catalog, set())
    reasons = " | ".join(i.reason for i in issues)
    assert unknown == 1 and "does not exist" in reasons and "51%" in reasons and "says PASS" in reasons


def test_a_check_citing_a_detail_inherits_its_verdict(catalog):
    """Citing the stress check inside a governance result counts as citing that result's verdict."""
    issues, _ = check(draft(), catalog, set())
    assert not [i for i in issues if i.section == "checks"]


# ---------- Answer paths ----------

def test_out_of_scope_is_declined_without_a_model_call(make_profile):
    client = FakeClient()
    answer = run_explain(make_run(make_profile, task="out_of_scope"), client)
    assert answer.kind == "declined" and "never places real trades" in answer.short_answer and client.calls == []


def test_failed_analysis_gives_an_evidence_only_fallback(make_profile):
    answer = run_explain(make_run(make_profile), FakeClient(), orchestrator={"status": "FAILED", "error": "boom"})
    assert answer.kind == "fallback" and "boom" in answer.note
    assert all(p.evidence for p in answer.key_points)  # every sentence is an evidence `say`, cited


def test_clean_answer_in_one_call(make_profile):
    client = FakeClient(draft())
    answer = run_explain(make_run(make_profile), client)
    assert answer.kind == "explained" and len(client.calls) == 1
    assert answer.verification.claims_checked == 5 and answer.verification.removed == [] and not answer.verification.retried
    assert answer.prompt.key == "explainer.advanced.md" and answer.model == "test-model"


def test_request_sent_to_bedrock(make_profile):
    client = FakeClient(draft())
    run_explain(make_run(make_profile), client)
    call = client.calls[0]
    assert call["model"] == "test-model" and call["output_format"] is ExplainerDraft
    assert call["output_config"] == {"effort": "low"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"} and "Explainer" in call["system"][0]["text"]
    first = call["messages"][0]["content"]
    assert first[0]["cache_control"] == {"type": "ephemeral"}   # the payload block is cached for the retry
    payload = json.loads(first[0]["text"])
    assert payload["question"].startswith("I'm thinking of putting 20%") and payload["reader"]["experience"] == "advanced"
    assert {g["verdict"] for g in payload["governance"]} == {"BLOCK", "PASS"}  # the orchestrator's final verdicts only


def test_retry_reuses_the_cached_payload_unchanged(make_profile):
    bad = draft(key_points=[{"text": "Nvidia returned +35% in 2022.", "evidence": ["ev-c5f01b9379"]}])
    client = FakeClient(bad, draft())
    run_explain(make_run(make_profile), client)
    assert client.calls[0]["messages"][0]["content"] == client.calls[1]["messages"][0]["content"]
    assert len(client.calls[1]["messages"]) == 3                # payload, then the draft, then the feedback


def test_retry_fixes_the_answer(make_profile):
    bad = draft(key_points=[{"text": "Nvidia returned +35% in 2022.", "evidence": ["ev-c5f01b9379"]}])
    client = FakeClient(bad, draft())
    answer = run_explain(make_run(make_profile), client)
    assert len(client.calls) == 2 and answer.verification.retried and answer.verification.removed == []
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "35%" in feedback and "Return the whole answer again" in feedback


def test_what_still_fails_after_the_retry_is_removed(make_profile):
    bad = draft(short_answer="Nvidia soared in 2022, up 312%.",
                key_points=GOOD["key_points"] + [{"text": "It returned +35%.", "evidence": ["ev-c5f01b9379"]}])
    answer = run_explain(make_run(make_profile), FakeClient(bad, bad))
    assert answer.short_answer == SHORT_ANSWER_REMOVED
    assert [p.text for p in answer.key_points] == [p["text"] for p in GOOD["key_points"]]
    assert {r.section for r in answer.verification.removed} == {"short_answer", "key_points"}


def test_verdicts_never_disappear(make_profile):
    """If every written check fails, the verdicts come straight from the governance evidence."""
    bad = draft(checks=[{"allocation": "x", "verdict": "PASS", "text": "Fine.", "evidence": ["ev-9de6c73e44"]}])
    answer = run_explain(make_run(make_profile), FakeClient(bad, bad))
    assert {c.verdict for c in answer.checks} == {"BLOCK", "PASS"}


def test_inline_ids_are_moved_out_of_the_text(make_profile):
    d = draft(short_answer="Blocked: worst point -24.2% vs -15% [ev-c5f01b9379] (ev-9de6c73e44).")
    answer = run_explain(make_run(make_profile), FakeClient(d))
    assert answer.short_answer == "Blocked: worst point -24.2% vs -15%."


@pytest.mark.parametrize("failure", [RuntimeError("bedrock down"), "refusal"])
def test_api_error_or_refusal_falls_back(make_profile, failure):
    answer = run_explain(make_run(make_profile), FakeClient(failure))
    assert answer.kind == "fallback" and "unavailable" in answer.note


def test_template_changed_since_planning_falls_back(make_profile):
    run = make_run(make_profile)
    run.prompt_ref = run.prompt_ref.model_copy(update={"sha256": "0" * 64})
    client = FakeClient(draft())
    answer = run_explain(run, client)
    assert answer.kind == "fallback" and "template" in answer.note and client.calls == []


def test_glossary_only_for_readers_who_want_to_learn(make_profile):
    standard = run_explain(make_run(make_profile, experience="beginner"), FakeClient(draft()))
    learning = run_explain(make_run(make_profile, experience="beginner", wants_to_learn=True), FakeClient(draft()))
    assert standard.glossary == [] and [g.term for g in learning.glossary] == ["Maximum drawdown"]
    assert learning.prompt.key == "explainer.beginner.learning.md"


def test_what_the_user_switched_off_is_listed(make_profile):
    answer = run_explain(make_run(make_profile, agent_toggles={"news": False},
                                  agent_toggle_reasons={"news": "I don't care about the news"}), FakeClient(draft()))
    assert any("I don't care about the news" in line for line in answer.not_analysed)


# ---------- Verdict names and prompt hygiene ----------

def test_unverified_verdicts_are_reported_not_crashed(make_profile):
    """governance_check returns UNVERIFIED (e.g. an ESG exclusion on stocks without reviewed rows)."""
    from app.agents.evidence import EvidenceItem
    from app.agents.explainer import _verdict_checks
    from app.schemas.run_result import RunAnswer

    cat = {"ev-1": EvidenceItem(id="ev-1", tool="governance_check", agent="orchestrator", verdict="UNVERIFIED",
                                say="Governance verdict UNVERIFIED for this profile.", weights={"ENB.TO": 0.5})}
    answer = RunAnswer(kind="explained", short_answer="x", checks=_verdict_checks(cat))
    assert answer.checks[0].verdict == "UNVERIFIED"
    assert draft(checks=[{"allocation": "x", "verdict": "UNVERIFIED", "text": "y", "evidence": []}]).checks[0].verdict


def test_no_prompt_mentions_removed_stress_tools_or_2022_examples():
    import re

    from app.agents.subagents import allocation, esg, fundamentals, macro, news, risk
    from app.context import tool_guide

    prompts_dir = Path(__file__).resolve().parents[2] / "backend" / "app" / "prompts"
    texts = {f.name: f.read_text(encoding="utf-8") for f in prompts_dir.glob("*.md")}
    texts.update({m.__name__: m.SYSTEM_PROMPT for m in (allocation, esg, fundamentals, macro, risk)})
    texts.update({"news": news.KB_PROMPT + news.LOCAL_PROMPT})
    texts.update({"tool_guide": tool_guide.tools_block("risk_review") + tool_guide.tools_block("allocation_proposal")})
    for name, text in texts.items():
        assert not re.search(r"`stress`|stress_tests|max_position|\b2022\b|NON_VERIFIE", text), name


def test_an_invented_percentage_is_not_backed_by_a_stray_number(catalog):
    """A plain "1" or "-1" somewhere in the evidence text used to back "up 100%"; only fractions in data fields
    and percentages written as such can back a percentage now."""
    ctx = context_numbers({"capital": 400000}, {"user_message": "20% in Nvidia"})
    invented = draft(key_points=[{"text": "Nvidia was up 100% in that period.", "evidence": ["ev-c5f01b9379"]}])
    issues, _ = check(invented, catalog, ctx)
    assert [i.section for i in issues] == ["key_points"] and "100%" in issues[0].reason
    issues, _ = check(draft(), catalog, ctx)  # the real figures (fractions and "% in text") still pass
    assert issues == []


def test_graph_mode_synthesis_verdict_is_the_final_one():
    """In graph mode (agents/graph.py) the top-level governance check is recorded by the "synthesis" node."""
    from app.agents.explainer import _final_verdicts

    ledger = [json.loads(line) for line in (Path(__file__).resolve().parents[1] / "fixtures" /
                                            "ledger_mateo_portfolio.jsonl").read_text().splitlines()]
    final = _final_verdicts(build_catalog(ledger))
    assert [g.agent for g in final] == ["synthesis"] and final[0].verdict == "PASS"


def test_the_explainer_sees_the_holdings_analysed(make_profile):
    """A question about "my portfolio" carries the saved holdings: the Explainer gets them with the reader."""
    client = FakeClient(draft())
    run_explain(make_run(make_profile), client)
    reader = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])["reader"]   # a cached content block
    assert reader["portfolio"] == [{"ticker": "NVDA", "weight": 0.2}]


# ---------- Rule 9: the reader's words (no verdict codes, rule ids or bare tickers) ----------

JARGON = "Governance returns BLOCK on it, while ADM gets a WARN; SHORT_HORIZON_DEFENSIVE was the only active constraint."


def test_codes_and_tickers_trigger_the_retry_with_the_names_to_use(make_profile):
    client = FakeClient(draft(key_points=[{"text": JARGON, "evidence": []}]), draft())
    answer = run_explain(make_run(make_profile), client)
    feedback = client.calls[1]["messages"][-1]["content"]
    assert answer.verification.retried and len(client.calls) == 2
    assert '"BLOCK"' in feedback and '"ADM"' in feedback and "Archer Daniels Midland" in feedback
    assert "SHORT_HORIZON_DEFENSIVE" in feedback and "rule 9" in feedback


def test_what_the_retry_leaves_is_put_in_plain_words(make_profile):
    bad = draft(key_points=[{"text": JARGON, "evidence": []}],
                checks=[{**GOOD["checks"][0], "allocation": "Your idea: NVDA at 20%"}])
    answer = run_explain(make_run(make_profile), FakeClient(bad, bad))
    text = " ".join([answer.short_answer, *(p.text for p in answer.key_points),
                     *(f"{c.allocation} {c.text}" for c in answer.checks)])
    for word in ("BLOCK", "WARN", "ADM", "SHORT_HORIZON", "overnance", "active constraint", "NVDA"):
        assert word not in text, word
    assert "Archer Daniels Midland" in text and "NVIDIA at 20%" in text
    assert answer.checks[0].verdict == "BLOCK"                # the code stays in the field the app translates


def test_the_model_gets_the_names_of_the_tickers_it_sees(make_profile):
    client = FakeClient(draft())
    run_explain(make_run(make_profile), client)
    names = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])["names"]
    assert names and all(t.isupper() or "." in t or "-" in t for t in names)


def test_plain_words_module():
    from app.agents import plain_words as pw

    names = {"ADM": "Archer Daniels Midland", "XIU.TO": "iShares S&P/TSX 60 Index ETF"}
    assert pw.problems("Nvidia fits your profile.", names) == []
    assert pw.plainify("Archer Daniels Midland (ADM) passes; XIU.TO too.", names) == \
        "Archer Daniels Midland passes; iShares S&P/TSX 60 Index ETF too."
    assert pw.plainify("The governance verdict is UNVERIFIED.", names) == "The check against your profile is not confirmed."
    assert "ALL" not in pw.universe_names() and "A" not in pw.universe_names()   # everyday words stay words
