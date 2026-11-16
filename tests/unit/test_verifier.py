"""Claim verifier: citations must exist, figures must come from the cited evidence. No LLM."""
from __future__ import annotations

from app.agents.verifier import resolve_token, verify_answer

EVIDENCE = {
    "ev-3f2a91c0de": {"evidence_id": "ev-3f2a91c0de", "tool": "metrics",
                      "say": "Over the period, this portfolio returned -12.9% (worst drop -21.1%).",
                      "output": {"portfolio": {"total_return": -0.128, "max_drawdown": -0.2112},
                                 "loss_at_trough": -84232.4}},
    "ev-3f2a00aa11": {"evidence_id": "ev-3f2a00aa11", "tool": "governance_check", "say": "Verdict BLOCK",
                      "output": {"checks": [{"id": "SINGLE_POSITION", "value": 0.1667, "limit": 0.1}]}},
    "ev-aaaa000001": {"evidence_id": "ev-aaaa000001", "tool": "risk_subagent", "say": "Risk finding",
                      "output": {"headline": "x"}, "nested_evidence_ids": ["ev-3f2a91c0de"]},
}


def test_exact_and_short_citations_resolve():
    r = verify_answer("The 2022 return was -12.9% [ev-3f2a91c0de]. Governance said BLOCK [ev-aaaa0].", EVIDENCE)
    assert r.ok and r.n_citations == 2 and r.unknown == []
    assert r.resolved == {"ev-3f2a91c0de": "ev-3f2a91c0de", "ev-aaaa0": "ev-aaaa000001"}


def test_ambiguous_short_citation_is_unknown():
    assert resolve_token("ev-3f2a", set(EVIDENCE)) is None          # matches two ids
    assert resolve_token("ev-3f2a9", set(EVIDENCE)) == "ev-3f2a91c0de"
    r = verify_answer("Something [ev-3f2a].", EVIDENCE)
    assert not r.ok and r.unknown == ["ev-3f2a"]


def test_unknown_citation_makes_the_answer_not_ok():
    r = verify_answer("Volatility is 40% [ev-ffffffffff] and the loss was -12.9% [ev-3f2a91c0de].", EVIDENCE)
    assert not r.ok and r.unknown == ["ev-ffffffffff"]
    assert "ev-3f2a91c0de" in r.resolved.values()


def test_multiple_ids_in_one_bracket():
    r = verify_answer("Loss -12.9% and a limit breach of 16.7% [ev-3f2a91c0de, ev-3f2a00aa11].", EVIDENCE)
    assert r.ok and r.n_citations == 2 and r.unmatched_numbers == []


def test_percent_matches_a_fraction_in_the_evidence_with_rounding():
    ok = verify_answer("Worst point -21.1% [ev-3f2a91c0de].", EVIDENCE)
    assert ok.unmatched_numbers == []
    bad = verify_answer("Worst point -35% [ev-3f2a91c0de].", EVIDENCE)
    assert bad.ok and bad.unmatched_numbers and bad.unmatched_numbers[0]["number"] == "-35%"


def test_money_matches_within_rounding_and_with_k_suffix():
    assert verify_answer("That is $84,232 at the worst point [ev-3f2a91c0de].", EVIDENCE).unmatched_numbers == []
    assert verify_answer("About $84k at the worst point [ev-3f2a91c0de].", EVIDENCE).unmatched_numbers == []
    assert verify_answer("About $80k at the worst point [ev-3f2a91c0de].", EVIDENCE).unmatched_numbers != []
    assert verify_answer("That is $90,000 [ev-3f2a91c0de].", EVIDENCE).unmatched_numbers != []


def test_sub_agent_citation_also_counts_the_numbers_of_its_nested_evidence():
    r = verify_answer("The risk review found a -12.9% year [ev-aaaa000001].", EVIDENCE)
    assert r.unmatched_numbers == []


def test_a_number_without_a_citation_is_flagged():
    r = verify_answer("The portfolio could lose 30% in a crash. Simulation only.", EVIDENCE)
    assert r.ok and r.uncited_numbers == ["The portfolio could lose 30% in a crash."]


def test_sentences_without_figures_need_no_citation():
    r = verify_answer("This is a simulation. Past returns do not predict future returns.", EVIDENCE)
    assert r.ok and r.n_citations == 0 and r.uncited_numbers == []


def test_years_dates_and_ids_are_not_figures():
    r = verify_answer("In 2022 (as_of 2026-11-12) the check ev-3f2a00aa11 ran.", EVIDENCE)
    assert r.uncited_numbers == [] and r.unmatched_numbers == []


def test_a_digit_suffix_of_a_word_is_not_glued_to_the_next_figure():
    r = verify_answer("Daily VaR95 0.86% and CVaR95 1.17% [ev-3f2a91c0de].", {
        "ev-3f2a91c0de": {"evidence_id": "ev-3f2a91c0de", "say": "", "output": {"var95_daily": 0.0086, "cvar": 0.0117}}})
    assert r.unmatched_numbers == [] and r.uncited_numbers == []


def test_an_amount_written_in_words_in_a_passage_matches_the_figure_quoted_from_it():
    ev = {"ev-aaaaaaaaaa": {"evidence_id": "ev-aaaaaaaaaa", "say": "",
                            "output": {"hits": [{"text": "investment commitments were $ 11.4 billion and other commitments "
                                                         "were $ 3.4 billion; Notes of US$400M due 2024"}]}}}
    ok = verify_answer("Commitments were $11.4 billion [ev-aaaaaaaaaa] and $3.4 billion [ev-aaaaaaaaaa].", ev)
    assert ok.unmatched_numbers == []
    assert verify_answer("Commitments were $114 billion [ev-aaaaaaaaaa].", ev).unmatched_numbers
