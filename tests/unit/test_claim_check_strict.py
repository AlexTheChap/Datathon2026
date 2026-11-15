"""Claim check (agents/claim_check.py): what a stated number may be backed by — no LLM, no AWS."""
from __future__ import annotations

from app.agents.claim_check import _supported, check, stated_numbers
from app.agents.evidence import build_catalog
from app.schemas.run_result import AnswerPoint, ExplainerDraft


def _supports(text: str, pool: set[tuple[str, float]]) -> bool:
    return all(_supported(s, pool) for s in stated_numbers(text))


def test_tolerance_follows_the_written_precision():
    pool = {("frac", 0.2418)}
    assert _supports("a drawdown of 24%", pool)                 # rounding of -24.18%
    assert _supports("a drawdown of 24.2%", pool)
    assert not _supports("a drawdown of 24.6%", pool)          # precise but wrong (used to pass: +/-0.6 point)


def test_a_gain_is_not_backed_by_a_loss_but_a_loss_may_be_positive():
    assert not _supports("the portfolio gained 24%", {("frac", -0.2418)})
    assert not _supports("a return of +24%", {("frac", -0.2418)})
    assert _supports("it lost 24%", {("frac", -0.2418)})        # signed drawdown
    assert _supports("a one-day loss of 2.1% (VaR)", {("frac", 0.021)})   # VaR is stored as a positive loss
    assert _supports("weights up to 15%", {("frac", -0.15)})   # "up to" is a limit, not a gain


def _record(seq: str, payload: dict) -> dict:
    return {"seq": seq, "tool": payload["tool"], "agent": "risk_subagent", "payload": payload}


def test_only_numbers_shown_to_the_explainer_can_back_a_claim():
    """A large item (here 40 correlations) is shown to the model as `say` + at most 30 facts: a figure that only
    exists deep in the raw envelope is not evidence the model saw."""
    matrix = {f"T{i}": round(0.01 * i + 0.005, 4) for i in range(40)}   # 0.005 ... 0.395
    env = {"evidence_id": "ev-corr01", "tool": "correlation", "say": "Correlations of the holdings.",
           "output": {"pairs": matrix}}
    catalog = build_catalog([_record("E-001", env)])
    item = catalog["ev-corr01"]
    assert ("frac", 0.005) in item.numbers                      # an early fact is shown
    assert ("frac", 0.395) not in item.numbers                  # past the 30 facts: never shown

    draft = ExplainerDraft(short_answer="Correlation of 39.5% between the two.",
                           short_answer_evidence=["ev-corr01"],
                           key_points=[AnswerPoint(text="A correlation of 0.5% for T0.", evidence=["ev-corr01"])],
                           checks=[], limitations=[], glossary=[], clarifying_question=None)
    issues, _ = check(draft, catalog, set())
    assert [(i.section, i.index) for i in issues] == [("short_answer", 0)]
