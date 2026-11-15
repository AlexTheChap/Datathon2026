"""Sub-agent toggles said in chat (box ③ → saved profile → box ④ plan). Mock backend: no network."""
import pytest

from app.context import resolve_plan
from app.intake import IntakeDraft, MockBackend, run_intake
from app.intake.schemas import AgentToggleChange, ProfileDraft, RequestDraft
from app.store.profiles import InMemoryProfileStore

FORM = {"risk_band": "low_moderate", "experience": "advanced", "language": "en"}


@pytest.fixture
def mock():
    return MockBackend()


def _news(plan):
    return next(d for d in plan.disabled_agents if d.agent == "news")


def test_dont_care_about_news_turns_it_off_with_the_users_words(mock):
    res = run_intake("I don't care about the news. Is my portfolio too risky?", "robert",
                     backend=mock, profile_form=FORM)
    assert res.status == "ready"
    assert res.profile.agent_toggles.news is False
    assert res.profile.agent_toggle_reasons == {"news": "don't care about the news"}
    assert [(c.agent, c.enabled) for c in res.toggle_changes] == [("news", False)]
    # Switching news off is not a request for news
    assert res.request.toggles.include_rag is False
    assert res.request.task == "risk_review"


def test_negative_esg_mention_does_not_pick_the_esg_task(mock):
    res = run_intake("I don't care about ESG. Is my portfolio too risky?", "u", backend=mock, profile_form=FORM)
    assert res.request.task == "risk_review"
    assert res.profile.agent_toggles.esg is False


def test_choice_is_saved_and_applies_to_later_messages(mock):
    store = InMemoryProfileStore()
    run_intake("I don't care about the news. Is my portfolio too risky?", "robert",
               backend=mock, profile_form=FORM, store=store)
    # Next message, no form: the stored profile still has news off, even if the text hints at news
    res = run_intake("I'm thinking of putting 20% in Nvidia, everyone is talking about it.", "robert",
                     backend=mock, store=store)
    assert res.profile.agent_toggles.news is False
    assert res.toggle_changes == []  # nothing new said about toggles in this message

    plan = resolve_plan(res.profile, res.request)
    assert "news" not in plan.enabled_names
    assert 'user said: "don\'t care about the news"' in _news(plan).reason  # the Explainer can quote it


def test_user_can_switch_it_back_on(mock):
    store = InMemoryProfileStore()
    run_intake("I don't care about the news. Is my portfolio too risky?", "r", backend=mock, profile_form=FORM, store=store)
    res = run_intake("Actually, bring back the news. Is my portfolio too risky?", "r", backend=mock, store=store)
    assert res.profile.agent_toggles.news is True
    assert res.profile.agent_toggle_reasons["news"] == "bring back the news"
    assert store.get("r").agent_toggles.news is True


def test_required_agent_stays_on_but_the_choice_is_quoted(mock):
    """'Risk' is required for risk_review: box ④ keeps it (forced) and says why, quoting the user."""
    res = run_intake("I don't care about risk analysis. Is my portfolio too risky?", "u",
                     backend=mock, profile_form=FORM)
    assert res.profile.agent_toggles.risk is False
    plan = resolve_plan(res.profile, res.request)
    risk = next(a for a in plan.enabled_agents if a.agent == "risk")
    assert risk.forced and 'user said: "don\'t care about risk analysis"' in risk.reason


def test_invented_quote_is_dropped():
    class InventsAQuote:
        name = "fake"

        def extract(self, text, known, feedback=None):
            return IntakeDraft(
                profile=ProfileDraft(risk_band="moderate", experience="beginner", language="en", learning="unsaid",
                                     agent_toggle_changes=[AgentToggleChange(agent="esg", enabled=False,
                                                                             quote="I hate ESG")]),
                request=RequestDraft(task="risk_review", intake_confidence=0.9))

    res = run_intake("Is my portfolio too risky?", "u", backend=InventsAQuote())
    assert res.profile.agent_toggles.esg is True  # unchanged
    assert res.toggle_changes == []
    assert any("'esg'" in w and "not in the message" in w for w in res.warnings)


def test_form_wins_per_agent_and_does_not_reenable_the_others(mock):
    store = InMemoryProfileStore()
    run_intake("I don't care about the news. Is my portfolio too risky?", "r", backend=mock, profile_form=FORM, store=store)
    res = run_intake("Is my portfolio too risky?", "r", backend=mock, store=store,
                     profile_form={"agent_toggles": {"esg": False}})
    assert res.profile.agent_toggles.news is False      # not silently switched back on by a partial form
    assert res.profile.agent_toggles.esg is False
    assert "esg" not in res.profile.agent_toggle_reasons  # set in the form: no chat quote


def test_form_overrides_a_chat_choice_and_clears_its_quote(mock):
    res = run_intake("I don't care about the news. Is my portfolio too risky?", "u", backend=mock,
                     profile_form={**FORM, "agent_toggles": {"news": True}})
    assert res.profile.agent_toggles.news is True
    assert "news" not in res.profile.agent_toggle_reasons


def test_no_profile_means_nothing_saved_or_reported(mock):
    res = run_intake("I don't care about the news. Is my portfolio too risky?", "newcomer", backend=mock)
    assert res.profile is None and res.toggle_changes == []


def test_bedrock_draft_schema_exposes_toggle_changes():
    """The JSON schema sent to Bedrock (structured outputs) lets the model fill agent_toggle_changes."""
    schema = IntakeDraft.model_json_schema()
    assert "agent_toggle_changes" in schema["$defs"]["ProfileDraft"]["properties"]
    assert set(schema["$defs"]["AgentToggleChange"]["properties"]["agent"]["enum"]) == {
        "risk", "news", "esg", "fundamentals", "macro_scenarios", "allocation"}


# ---------- A message that only changes a setting ----------

def test_a_setting_alone_is_confirmed_and_runs_no_analysis(mock):
    store = InMemoryProfileStore()
    run_intake("Is my portfolio too risky?", "robert", backend=mock, profile_form=FORM, store=store)
    res = run_intake("I do not care about the news anymore for my portfolio", "robert", backend=mock, store=store)
    assert res.status == "profile_updated"
    assert res.message.startswith("Done. News is now switched off")
    assert store.get("robert").agent_toggles.news is False                    # saved


def test_a_setting_with_a_question_still_answers_the_question(mock):
    store = InMemoryProfileStore()
    run_intake("Is my portfolio too risky?", "robert", backend=mock, profile_form=FORM, store=store)
    res = run_intake("I don't care about the news. Is my portfolio too risky?", "robert", backend=mock, store=store)
    assert res.status == "ready" and res.message is None


def test_a_new_limit_says_the_portfolio_will_be_rebuilt(mock):
    store = InMemoryProfileStore()
    run_intake("Is my portfolio too risky?", "robert", backend=mock, profile_form={**FORM, "portfolio": {"SPY": 1.0}},
               store=store)
    res = run_intake("No crypto please", "robert", backend=mock, store=store)
    assert res.status == "profile_updated"
    assert "New hard limit: no cryptocurrencies." in res.message and "rebuilt" in res.message


class _SettingsOnly:
    """The model reads the message as settings only, but nothing new is said: nothing to change."""
    name = "bedrock"

    def extract(self, text, known_profile, feedback=None):
        return IntakeDraft(profile=ProfileDraft(learning="unsaid"),
                           request=RequestDraft(task="risk_review", settings_only=True, intake_confidence=0.9))


def test_a_setting_already_saved_is_said_so():
    store = InMemoryProfileStore()
    run_intake("Is my portfolio too risky?", "robert", backend=MockBackend(), profile_form=FORM, store=store)
    res = run_intake("Keep things as they are", "robert", backend=_SettingsOnly(), store=store)
    assert res.status == "profile_updated" and res.message.startswith("Nothing to change")
