"""Output guardrail (L18): Bedrock Guardrails on the Explainer's answer. No AWS: a fake bedrock-runtime client
answers ApplyGuardrail; the evidence and drafts are the Explainer tests' (Robert, "20% in Nvidia")."""
import threading

from test_explainer import GOOD, LEDGER, SETTINGS, FakeClient, draft, make_run  # same fixtures as the Explainer tests

from app.agents.evidence import build_catalog
from app.agents.explainer import SHORT_ANSWER_REMOVED, explain
from app.agents.output_guardrail import MAX_CONTENT, MAX_QUERY, build_content, screen
from app.context.prompt_loader import LocalPromptSource

GUARDED = SETTINGS.model_copy(update={"guardrail_id": "gr-test", "guardrail_version": "3"})
INVENTED = "Nvidia is a low-risk, defensive holding."


def ok():
    return {"action": "NONE", "outputs": [], "assessments": [{}]}


def ungrounded(score=0.31):
    return {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "blocked"}], "assessments": [{
        "contextualGroundingPolicy": {"filters": [
            {"type": "GROUNDING", "threshold": 0.7, "score": score, "action": "BLOCKED", "detected": True},
            {"type": "RELEVANCE", "threshold": 0.5, "score": 0.9, "action": "NONE", "detected": False}]}}]}


class FakeGuardrail:
    """apply_guardrail answers `rule(statement)` (default: pass); records every request."""

    def __init__(self, rule=None, fail=None):
        self.rule, self.fail, self.calls = rule or (lambda _t: ok()), fail, []
        self._lock = threading.Lock()

    def apply_guardrail(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        statement = next(c["text"]["text"] for c in kwargs["content"] if c["text"]["qualifiers"] == ["guard_content"])
        return self.rule(statement)


def run_explain(run, client, guardrail, settings=GUARDED):
    return explain(run, {"status": "DONE", "summary": "draft notes"}, LEDGER, client=client, settings=settings,
                   prompt_source=LocalPromptSource(), guardrail_client=guardrail)


def with_invented_point():
    return draft(key_points=GOOD["key_points"] + [{"text": INVENTED, "evidence": ["ev-4a54cab2ec"]}])


# ---------- screen() ----------

def test_not_configured_makes_no_call_and_changes_nothing(make_profile):
    guard = FakeGuardrail()
    answer = run_explain(make_run(make_profile), FakeClient(draft()), guard, settings=SETTINGS)
    assert guard.calls == [] and answer.verification.output_guardrail is None and answer.verification.removed == []


def test_switch_off_only_the_output_check(make_profile):
    guard = FakeGuardrail()
    off = GUARDED.model_copy(update={"output_guardrail": False})
    answer = run_explain(make_run(make_profile), FakeClient(draft()), guard, settings=off)
    assert guard.calls == [] and answer.verification.output_guardrail is None


def test_every_statement_is_sent_as_output_with_its_evidence():
    catalog = build_catalog(LEDGER)
    guard = FakeGuardrail()
    result = screen(draft(), catalog, "20% in Nvidia?", settings=GUARDED, client=guard)
    assert result.status == "passed" and result.issues == []
    assert len(guard.calls) == 1 + len(GOOD["key_points"]) + len(GOOD["checks"]) + len(GOOD["limitations"])
    call = guard.calls[0]
    assert call["source"] == "OUTPUT" and call["guardrailIdentifier"] == "gr-test" and call["guardrailVersion"] == "3"
    by_kind = {c["text"]["qualifiers"][0]: c["text"]["text"] for c in call["content"]}
    assert by_kind["query"] == "20% in Nvidia?" and "ev-c5f01b9379" in by_kind["grounding_source"]


def test_uncited_statement_gets_filters_only_no_grounding():
    content = build_content("Only one crisis, 2022, is in the data.", [], "20% in Nvidia?")
    assert [c["text"]["qualifiers"] for c in content] == [["guard_content"]]


def test_a_governance_check_is_filtered_on_its_sentence_only():
    """Live test: "Your idea: Nvidia at 20%. Stress loss…" read as a trade instruction and scored grounding 0.47."""
    catalog = build_catalog(LEDGER)
    guard = FakeGuardrail()
    screen(draft(), catalog, "q", settings=GUARDED, client=guard)
    sent = [c["content"] for c in guard.calls if c["content"][-1]["text"]["text"] == GOOD["checks"][0]["text"]]
    assert len(sent) == 1 and [b["text"]["qualifiers"] for b in sent[0]] == [["guard_content"]]


def test_sizes_are_capped_to_the_api_limits():
    catalog = build_catalog(LEDGER)
    item = catalog["ev-c5f01b9379"]
    content = build_content("x" * (MAX_CONTENT + 50), [item], "q" * (MAX_QUERY + 50))
    sizes = {c["text"]["qualifiers"][0]: len(c["text"]["text"]) for c in content}
    assert sizes["query"] == MAX_QUERY and sizes["guard_content"] == MAX_CONTENT


def test_denied_topic_is_reported():
    catalog = build_catalog(LEDGER)
    topic = {"action": "GUARDRAIL_INTERVENED", "outputs": [], "assessments": [
        {"topicPolicy": {"topics": [{"name": "Guaranteed returns", "type": "DENY", "action": "BLOCKED"}]}}]}
    guard = FakeGuardrail(lambda t: topic if t == INVENTED else ok())
    result = screen(with_invented_point(), catalog, "q", settings=GUARDED, client=guard)
    assert result.status == "flagged" and [i.index for i in result.issues] == [2]
    assert "denied topic Guaranteed returns" in result.issues[0].reason


def test_an_aws_error_is_unavailable_never_raised():
    catalog = build_catalog(LEDGER)
    result = screen(draft(), catalog, "q", settings=GUARDED, client=FakeGuardrail(fail=RuntimeError("throttled")))
    assert result.status == "unavailable" and result.issues == []


# ---------- Inside explain() ----------

def test_ungrounded_statement_is_removed_after_the_retry(make_profile):
    guard = FakeGuardrail(lambda t: ungrounded() if t == INVENTED else ok())
    client = FakeClient(with_invented_point(), with_invented_point())
    answer = run_explain(make_run(make_profile), client, guard)
    v = answer.verification
    assert answer.kind == "explained" and v.retried and v.output_guardrail == "flagged"
    assert [r.text for r in v.removed] == [INVENTED] and "grounding 0.31 < 0.70" in v.removed[0].reason
    assert INVENTED not in [p.text for p in answer.key_points] and len(answer.key_points) == 2
    assert "not supported by its cited evidence" in client.calls[1]["messages"][-1]["content"]


def test_fixed_on_the_retry_keeps_everything(make_profile):
    guard = FakeGuardrail(lambda t: ungrounded() if t == INVENTED else ok())
    client = FakeClient(with_invented_point(), draft())
    answer = run_explain(make_run(make_profile), client, guard)
    assert answer.verification.retried and answer.verification.removed == []
    assert answer.verification.output_guardrail == "passed"


def test_ungrounded_short_answer_is_replaced(make_profile):
    short = GOOD["short_answer"]
    guard = FakeGuardrail(lambda t: ungrounded() if t == short else ok())
    answer = run_explain(make_run(make_profile), FakeClient(draft(), draft()), guard)
    assert answer.short_answer == SHORT_ANSWER_REMOVED and answer.short_answer_evidence == []


def test_personal_data_is_masked_not_removed(make_profile):
    named = "Robert Smith, Nvidia is 20% of the weight but about 78% of the risk."
    masked = "{NAME}, Nvidia is 20% of the weight but about 78% of the risk."
    pii = {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": masked}], "assessments": [
        {"sensitiveInformationPolicy": {"piiEntities": [{"type": "NAME", "match": "Robert Smith", "action": "ANONYMIZED"}]}}]}
    guard = FakeGuardrail(lambda t: pii if t == named else ok())
    d = draft(key_points=[{"text": named, "evidence": ["ev-4a54cab2ec"]}] + GOOD["key_points"][1:])
    answer = run_explain(make_run(make_profile), FakeClient(d), guard)
    assert answer.key_points[0].text == masked and answer.verification.removed == []
    assert not answer.verification.retried and answer.verification.output_guardrail == "passed"


def test_guardrail_outage_keeps_a_good_answer(make_profile):
    guard = FakeGuardrail(fail=ConnectionError("no network"))
    answer = run_explain(make_run(make_profile), FakeClient(draft()), guard)
    assert answer.kind == "explained" and answer.verification.output_guardrail == "unavailable"
    assert answer.verification.removed == [] and not answer.verification.retried
