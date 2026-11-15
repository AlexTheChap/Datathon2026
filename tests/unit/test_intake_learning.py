"""Explanation style from what people say (box ③ → part ④ template choice). Mock intake, no network."""
import pytest

from app.context.prompt_loader import LocalPromptSource, resolve_explainer_prompt
from app.intake import MockBackend, run_intake
from app.store.profiles import InMemoryProfileStore

FORM = {"risk_band": "moderate", "experience": "beginner", "language": "en"}


@pytest.fixture
def mock():
    return MockBackend()


@pytest.mark.parametrize("text, expected", [
    ("I'm a student and I want to understand. Is my portfolio too risky?", True),
    ("Can you explain it to me? Is my portfolio too risky?", True),
    ("Just give me the answer: is my portfolio too risky?", False),
    ("Is my portfolio too risky?", False),  # nothing said: default stays off
])
def test_wish_to_learn_comes_from_the_text(mock, text, expected):
    res = run_intake(text, "u", backend=mock, profile_form=FORM)
    assert res.profile.wants_to_learn is expected


def test_learning_choice_is_kept_and_can_be_turned_off(mock):
    store = InMemoryProfileStore()
    run_intake("I'm a student, I want to learn. Is my portfolio too risky?", "s", backend=mock, profile_form=FORM, store=store)
    assert run_intake("What would happen in a crash?", "s", backend=mock, store=store).profile.wants_to_learn is True
    assert run_intake("Keep it short: what would happen in a crash?", "s", backend=mock, store=store).profile.wants_to_learn is False


def test_three_readers_get_three_explainer_templates(mock):
    """A retiree new to investing, a student, a broker: the template follows experience and wish to learn."""
    retiree = run_intake("I'm new to investing, cautious, I just want to know: is my portfolio too risky?", "r",
                         backend=mock, profile_form={"language": "en"})
    student = run_intake("I'm a student, beginner, moderate risk, I want to understand: is my portfolio too risky?", "s",
                         backend=mock, profile_form={"language": "en"})
    broker = run_intake("I'm a stock broker, high risk. Is my portfolio too risky compared to SPY?", "b",
                        backend=mock, profile_form={"language": "en"})
    keys = [resolve_explainer_prompt(r.profile, LocalPromptSource()).key for r in (retiree, student, broker)]
    assert keys == ["explainer.beginner.md", "explainer.beginner.learning.md", "explainer.advanced.md"]


def test_age_alone_does_not_change_the_style(mock):
    """Never inferred from demographics: an older person who says nothing about learning keeps the default."""
    res = run_intake("I'm 78 years old, cautious, beginner. Is my portfolio too risky?", "o", backend=mock,
                     profile_form={"language": "en"})
    assert res.profile.wants_to_learn is False and res.profile.experience == "beginner"
