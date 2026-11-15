"""Box ① ↔ ② in a real browser: the frontend served by the API, mock intake, no AWS.

Skipped unless Playwright and its Chromium are installed:
    pip install -e ".[ui]" && python -m playwright install chromium
"""
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from app.api.service import ApiService, NoDispatcher  # noqa: E402
from app.intake import MockBackend  # noqa: E402


SERVICE = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())  # plan only (no Bedrock in tests); tests attach answers to its runs


def DEV_CONFIG(route):
    """These tests use the explicit local test mode (the page never falls back to it on its own)."""
    route.fulfill(status=200, content_type="application/javascript", body='window.AF_CONFIG = {"auth": "dev"};')


def NO_CONFIG(route):
    route.fulfill(status=200, content_type="application/javascript", body="")


@pytest.fixture
def page(base_url):
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.route("**/config.js", DEV_CONFIG)  # explicit local test mode; the git-ignored sign-in config must not leak in
        ctx.add_init_script("localStorage.setItem('af.actor', JSON.stringify('e2e-' + Math.random().toString(36).slice(2)))")
        pg = ctx.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(f"{base_url}/#/ask")
        yield pg
        browser.close()


def fill_profile_form(page, risk, experience):
    """The profile page (#/profile), step by step: skip age, then answer the two required questions.
    Risk and experience are 1-to-10 scales landing in a category. The profile is saved once experience is."""
    page.goto(page.url.split("#")[0] + "#/profile")
    page.wait_for_selector("#q-card-age.q-card--open")
    page.click("button[aria-label='Skip age']")
    page.fill("#q-risk_band", str(risk))
    page.click("button[aria-label='Save risk level']")
    page.fill("#q-experience", str(experience))
    page.click("button[aria-label='Save experience']")
    page.wait_for_selector("#q-card-experience.q-card--locked")


def skip_the_rest(page):
    """Skip every remaining optional question; the last one completes the profile."""
    while page.locator("button[aria-label^='Skip ']").count():
        page.locator("button[aria-label^='Skip ']").first.click()
        page.wait_for_timeout(150)


def test_new_user_answers_inline_then_gets_a_plan(page):
    page.wait_for_selector("text=No profile yet")
    page.fill("#question", "Is my portfolio too risky?")
    page.click("button[aria-label='Ask']")
    page.wait_for_selector(".msg--user:has-text('Is my portfolio too risky?')")   # the question appears as a message
    page.wait_for_selector("text=AlphaForge needs one detail")
    page.click("label.choice:has-text('Low to moderate')")
    page.click("label.choice:has-text('Advanced')")
    page.click("button:has-text('Continue')")
    page.wait_for_selector("text=AlphaForge will look at: Portfolio risk.")
    page.wait_for_selector("text=Low to moderate")  # the side summary now shows the saved profile
    assert page.errors == []


def test_news_switched_off_in_chat_shows_in_plan_profile_and_can_be_undone(page):
    fill_profile_form(page, risk=6, experience=2)   # Moderate, Beginner
    page.click("a.nav__link:has-text('Ask')")
    page.fill("#question", "I don't care about the news. I'm thinking of putting 20% in Nvidia, everyone is talking about it.")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("text=News is now off in your profile")
    page.locator("summary:has-text('How AlphaForge worked on this')").last.click()   # the plan, inside the answer
    page.wait_for_selector("text=Off because you said")
    assert page.locator("text=Off because you said").count() == 1  # the plan quotes the user

    page.wait_for_selector("text=Analyses off")
    page.click("button:has-text('Undo')")
    page.wait_for_selector("text=News is back on")
    assert page.errors == []


def test_a_setting_said_alone_in_chat_is_confirmed_without_an_analysis(page):
    fill_profile_form(page, risk=6, experience=2)   # Moderate, Beginner
    page.click("a.nav__link:has-text('Ask')")
    page.fill("#question", "I do not care about the news anymore for my portfolio")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("text=Done. News is now switched off")
    assert page.locator("text=How AlphaForge worked on this").count() == 0     # no analysis ran
    assert page.locator("a:has-text('Set up your profile')").count() == 0
    page.click("button:has-text('Undo')")
    page.wait_for_selector("text=News is back on")
    assert page.errors == []


def test_learning_switch_is_saved_and_picks_the_learning_template(page):
    fill_profile_form(page, risk=6, experience=2)   # Moderate, Beginner
    page.check("#q-wants_to_learn")
    page.click("button[aria-label='Save learning explanations']")
    page.wait_for_selector("#q-card-wants_to_learn.q-card--locked")
    actor = page.evaluate("JSON.parse(localStorage.getItem('af.actor'))")
    assert SERVICE.get_profile(actor).wants_to_learn is True

    page.click("a.nav__link:has-text('Ask')")
    page.wait_for_selector("text=Explained so you learn")  # the side summary
    page.fill("#question", "Is my portfolio too risky?")
    page.keyboard.press("Enter")                                              # Enter sends, like a chat
    page.locator("summary:has-text('How AlphaForge worked on this')").last.click()
    page.wait_for_selector("text=Explained for beginner investors, so you learn")
    assert page.errors == []


def test_profile_validation_and_example_persona(page):
    page.goto(page.url.split("#")[0] + "#/profile")
    page.wait_for_selector("text=Start from an example")
    page.click("button[aria-label='Skip age']")
    page.click("button[aria-label='Save risk level']")  # nothing chosen yet
    page.wait_for_selector("text=Pick the risk level that fits you best.")
    page.fill("#q-risk_band", "6")
    page.click("button[aria-label='Save risk level']")
    page.fill("#q-experience", "5")
    page.click("button[aria-label='Save experience']")
    page.click("button[aria-label='Skip learning explanations']")
    page.fill("#q-horizon_min", "10")
    page.fill("#q-horizon_max", "3")
    page.click("button[aria-label='Save investment horizon']")
    page.wait_for_selector("text=The end can't be before the start.")
    page.click("button.example:has-text('Robert')")          # fills every question, unsaved
    page.click("button[aria-label='Save all answers']")
    page.wait_for_selector("#q-card-limits.q-card--locked")
    assert page.is_checked("input[name='limit-crypto']")
    assert page.input_value("#q-age") == "59"
    assert page.errors == []


def test_dark_mode_toggle_persists(page):
    page.click("#mode-toggle")
    assert page.evaluate("document.documentElement.dataset.mode") == "dark"
    page.reload()
    page.wait_for_selector("#mode-toggle")
    assert page.evaluate("document.documentElement.dataset.mode") == "dark"
    assert page.get_attribute("#mode-toggle", "aria-label") == "Switch to light mode"


def test_unknown_run_shows_a_helpful_empty_state(page, base_url):
    page.goto(f"{base_url}/#/runs/run-doesnotexist")
    page.wait_for_selector("text=We can't find this analysis")
    assert page.locator("a:has-text('Ask a new question')").count() == 1


def test_phone_layout_has_no_horizontal_scroll(base_url):
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        pg = browser.new_page(viewport={"width": 360, "height": 780})
        pg.route("**/config.js", DEV_CONFIG)
        for route in ("#/ask", "#/profile"):
            pg.goto(f"{base_url}/{route}")
            pg.wait_for_selector("h1")
            assert not pg.evaluate("document.documentElement.scrollWidth > window.innerWidth"), route
        browser.close()


# ---------- Cognito mode, offline: a fake Cognito in the browser, real token checks in the API ----------

def test_cognito_demo_sign_in_seeds_the_persona_and_signs_out(base_url, monkeypatch):
    import json
    import time as _time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.api import auth

    key, other = (rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
    region, pool, client = "us-east-1", "us-east-1_E2E", "e2e-client"
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"

    class Keys:
        class K:
            key = None

        def get_signing_key_from_jwt(self, token):
            k = self.K()
            k.key = key.public_key()
            return k

    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    monkeypatch.setattr(auth, "verifier_from_env", lambda: auth.CognitoVerifier(region, pool, client, jwk_client=Keys()))

    def id_token(signing_key=key):
        now = int(_time.time())
        return jwt.encode({"sub": "sub-robert-e2e", "email": "robert.demo@alphaforge.example", "iss": issuer, "aud": client,
                           "token_use": "id", "iat": now, "exp": now + 3600}, signing_key, algorithm="RS256")

    config = {"auth": "cognito", "region": region, "userPoolId": pool, "clientId": client,
              "demoAccounts": [{"persona": "robert", "name": "Robert", "email": "robert.demo@alphaforge.example",
                                "password": "irrelevant", "summary": "Conservative investor, 59."}]}
    calls = []

    def fake_cognito(route):
        target = route.request.headers.get("x-amz-target", "").split(".")[-1]
        calls.append(target)
        body = {"InitiateAuth": {"AuthenticationResult": {"IdToken": id_token(), "RefreshToken": "refresh", "ExpiresIn": 3600}},
                "RevokeToken": {}}.get(target, {})
        route.fulfill(status=200, content_type="application/x-amz-json-1.1", body=json.dumps(body))

    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route("**/config.js", lambda r: r.fulfill(status=200, content_type="application/javascript",
                                                     body=f"window.AF_CONFIG = {json.dumps(config)};"))
        pg.route("https://cognito-idp.us-east-1.amazonaws.com/", fake_cognito)

        pg.goto(f"{base_url}/#/profile")
        pg.wait_for_selector("text=Sign in to AlphaForge")
        assert pg.is_hidden(".nav")
        pg.click("button.demo-account:has-text('Robert')")
        pg.wait_for_selector("text=Your investor profile")  # back to the page asked for
        assert pg.input_value("#q-risk_band") == "4" and "Low to moderate" in pg.inner_text("#q-risk_band-out")  # persona profile seeded by the API
        assert pg.inner_text("#who") == "robert.demo@alphaforge.example"

        # A token the pool did not sign is refused by the API: the app signs out and says why
        session = json.loads(pg.evaluate("localStorage.getItem('af.session')"))
        session["idToken"] = id_token(other)
        pg.evaluate(f"localStorage.setItem('af.session', {json.dumps(json.dumps(session))})")
        pg.unroute("https://cognito-idp.us-east-1.amazonaws.com/")
        pg.route("https://cognito-idp.us-east-1.amazonaws.com/", lambda r: r.fulfill(
            status=400, content_type="application/x-amz-json-1.1", body=json.dumps({"__type": "NotAuthorizedException"})))
        pg.click("a.nav__link:has-text('Ask')")  # the chat loads the history: the API sees the forged token, answers 401
        # Refused right after signing in: the app names the real cause instead of "session expired"
        pg.wait_for_selector("text=AlphaForge's server refused your sign-in")
        assert pg.evaluate("localStorage.getItem('af.session')") is None
        browser.close()
    assert "InitiateAuth" in calls and errors == []



# ---------- The Explainer's answer on the plan page (real ledger fixture, fake model) ----------

def test_answer_with_checks_evidence_and_glossary(page, base_url):
    import json as _json
    import sys as _sys
    import types as _types
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "unit"))
    from test_explainer import LEDGER, FakeClient, draft  # noqa: E402
    from test_explainer import SETTINGS
    from app.agents.explainer import explain
    from app.context.prompt_loader import LocalPromptSource

    # A learner asks; the run gets the Explainer's answer built from the real ledger (fake model, no AWS)
    actor = page.evaluate("JSON.parse(localStorage.getItem('af.actor'))")
    form = {"risk_band": "low_moderate", "experience": "beginner", "language": "en", "wants_to_learn": True}
    status, out = SERVICE.post_message(actor, __import__("app.api.service", fromlist=["MessageIn"]).MessageIn(
        text="I'm thinking of putting 20% in Nvidia, everyone is talking about it.", profile_form=form))
    run = SERVICE.runs.get(out.run_id)
    answer = explain(run, {"status": "DONE", "summary": ""}, LEDGER, client=FakeClient(draft()), settings=SETTINGS,
                     prompt_source=LocalPromptSource())
    run.status, run.result = "DONE", {"summary": "", "answer": answer.model_dump(mode="json")}
    SERVICE.runs.put(run)
    SERVICE.ledger = _types.SimpleNamespace(records=lambda run_id: LEDGER)

    page.goto(f"{base_url}/#/runs/{run.run_id}")
    page.wait_for_selector("text=Profile checks")
    assert page.locator(".verdict--block").first.inner_text().strip() == "Blocked"
    assert page.locator("text=Words used in this answer").count() == 1       # a learner gets the glossary
    assert page.locator("text=All 5 statements were checked against the evidence").count() == 1
    page.wait_for_selector(".evidence-item")
    first_chip = page.locator(".answer__short .chip").first
    first_chip.click()
    target = page.locator(".evidence-item.is-focus")
    assert target.count() == 1 and "In 2022" in target.inner_text()           # chip 1 = the 2022 replay
    assert page.locator("text=All other evidence from this analysis").count() == 1
    assert page.errors == []


# ---------- My portfolio (box ① dashboard, frontend/assets/portfolio.js) ----------

def _portfolio_for(page, *, risk_follow_up=False, experience=None):
    """Give the page's user the recorded live portfolio run (Mateo, graph mode) and its ledger."""
    import copy
    import json as _json
    import types as _types
    from pathlib import Path as _Path

    from app.schemas import Profile
    from app.store.runs import RunRecord

    fx = _Path(__file__).resolve().parents[1] / "fixtures"
    ledger = [_json.loads(x) for x in (fx / "ledger_mateo_portfolio.jsonl").read_text().splitlines()]
    actor = page.evaluate("JSON.parse(localStorage.getItem('af.actor'))")
    run = RunRecord.model_validate_json((fx / "run_mateo_portfolio.json").read_text())
    run = run.model_copy(update={"actor_id": actor, "run_id": f"run-pf-{actor}"})
    profile = run.profile.model_dump()
    if experience:
        profile["experience"] = experience
    SERVICE.profiles.put(Profile.model_validate({**profile, "actor_id": actor, "capital": 150000}))
    SERVICE.runs.put(run.model_copy(update={"profile": Profile.model_validate({**run.profile.model_dump(), "actor_id": actor})}))
    ledgers = {run.run_id: ledger}
    if risk_follow_up:  # the follow-up question measured the exact weights
        weights = {"SPY": 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, "QQQ": 0.0956}
        rc = copy.deepcopy(next(r for r in ledger if r["tool"] == "risk_contribution"))
        rc["payload"][0]["output"]["weights"] = weights
        rc["payload"][0]["output"]["risk_contribution"] = {"SPY": 0.52, "QQQ": 0.17, "XIU.TO": 0.24, "XBB.TO": 0.07}
        follow = run.model_copy(update={"run_id": f"run-risk-{actor}", "request": run.request.model_copy(update={
            "user_message": "Check the risk of my portfolio: SPY 34.81%, XBB.TO 30.54%, XIU.TO 25.09%, QQQ 9.56%.",
            "task": "risk_review"})})
        SERVICE.runs.put(follow)
        ledgers[follow.run_id] = [rc]
    previous = SERVICE.ledger
    SERVICE.ledger = _types.SimpleNamespace(records=lambda run_id: ledgers.get(run_id) or
                                            (previous.records(run_id) if previous else []))
    return run


def test_portfolio_dashboard_shows_the_built_portfolio(page, base_url):
    run = _portfolio_for(page)
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector(".hero-card")
    assert "Fits your profile" in page.locator(".hero-card .verdict").inner_text()
    assert page.locator("#viz-holdings-title").count() == 1 and page.locator(".viz-bars__row").count() == 4
    assert "69%" in page.locator(".viz-seg").inner_text()                     # stocks share, from the weights
    assert page.locator("#checks-title ~ div .verdict--pass").count() == 5
    # The run measured risk on another mix: never shown as this portfolio's; the follow-up can't run here.
    page.wait_for_selector("button:has-text('Measure it')")
    assert page.locator("text=Share of the money vs share of the risk").count() == 0
    assert page.locator(f"a[href='#/runs/{run.run_id}']").count() >= 1           # linked to its analysis
    # Shown = adopted: saved in the profile, so "my portfolio" questions are analysed on it.
    for _ in range(50):
        if SERVICE.profiles.get(run.actor_id).portfolio_run_id == run.run_id:
            break
        time.sleep(0.1)
    assert SERVICE.profiles.get(run.actor_id).portfolio == {"SPY": 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, "QQQ": 0.0956}
    page.click("a.nav__link:has-text('Ask a question')")
    page.wait_for_selector("text=SPY 35%, XBB.TO 31%, XIU.TO 25%, QQQ 10%")       # the profile panel shows it
    assert page.errors == []


def test_portfolio_shows_how_it_reacts_to_rates_when_macro_is_on(page, base_url):
    _portfolio_for(page)
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector("#rates-title")
    page.wait_for_selector("section[aria-labelledby=rates-title] dl.facts", timeout=8000)
    text = page.locator("section[aria-labelledby='rates-title']").inner_text()
    assert "Bank of Canada rate" in text and "2.5%" in text
    assert "−" in text and "not a forecast" in text                                 # a 1-point rise lowers bond-fund values
    rows = page.locator("section[aria-labelledby='rates-title'] tbody tr")
    assert rows.count() == 4                                                         # SPY, XBB.TO, XIU.TO, QQQ
    assert "Not measured" in page.locator("section[aria-labelledby='rates-title'] tbody tr:has-text('SPY')").inner_text()
    assert page.errors == []


def test_rates_section_is_hidden_when_macro_is_off(page, base_url):
    run = _portfolio_for(page)
    SERVICE.put_profile(run.actor_id, {"agent_toggles": {"macro_scenarios": False}})
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector(".hero-card")
    page.wait_for_timeout(500)
    assert page.locator("#rates-title").count() == 0
    assert page.errors == []


def test_detail_level_follows_experience_and_can_be_changed(page, base_url):
    _portfolio_for(page, experience="beginner")
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector(".hero-card")
    assert page.is_checked("input[name='detail-level'][value='simple']")
    off = page.evaluate("""() => [...document.querySelectorAll('.seg-control__option')].map((l) => {
        const c = l.getBoundingClientRect(), t = l.querySelector('span').getBoundingClientRect();
        return Math.abs((c.left + c.right) / 2 - (t.left + t.right) / 2); })""")
    assert max(off) < 1.5                                                         # each label centred in its cell
    assert page.locator("text=500 large US companies").count() >= 1             # plain words for a beginner
    assert page.locator("text=Each holding's history").count() == 0
    simple = page.inner_text(".hero-card__answer")
    assert simple.startswith("Your money is spread over 4 investments") and "SPY" not in simple
    page.click("label.seg-control__option:has-text('Detailed')")
    page.wait_for_selector("text=Each holding's history")
    assert page.locator("text=Largest single position").count() == 1             # technical names when detailed
    assert page.inner_text(".hero-card__answer").startswith("Weights: SPY 34.8%")   # the summary follows the level
    page.click("a.nav__link:has-text('Ask a question')")
    page.click("a.nav__link:has-text('My portfolio')")
    page.wait_for_selector(".hero-card")
    assert page.is_checked("input[name='detail-level'][value='detailed']")      # the choice is remembered
    assert page.errors == []


def test_risk_comes_from_the_follow_up_on_the_exact_mix(page, base_url):
    _portfolio_for(page, risk_follow_up=True)
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector("text=Share of the money vs share of the risk")
    assert page.locator("text=Measured on this exact mix in a follow-up analysis").count() == 1
    page.click("#risk ~ details summary, figure:has-text('Share of the money vs share of the risk') summary")
    assert "52%" in page.locator("figure:has-text('Share of the money vs share of the risk') table").inner_text()
    assert "null" not in page.locator("main").inner_text()                     # an absent note is not printed
    assert page.errors == []


def test_saving_a_profile_builds_the_portfolio(page, base_url):
    fill_profile_form(page, risk=6, experience=5)   # Moderate, Intermediate
    skip_the_rest(page)                              # the last answer completes the profile
    page.wait_for_selector("text=Building your portfolio")
    page.click("a:has-text('See my portfolio')")
    # This test server only plans (no Bedrock): the dashboard says so instead of spinning forever.
    page.wait_for_selector("text=The analysis agents aren't running", timeout=15000)
    assert page.errors == []


def test_portfolio_without_a_profile_asks_for_one(page, base_url):
    page.goto(f"{base_url}/#/portfolio")
    page.wait_for_selector("text=First, tell AlphaForge about you")
    page.click("main a:has-text('Set up my profile')")
    page.wait_for_selector("#q-card-age.q-card--open")                        # a new user gets the first question
    assert page.errors == []


# ---------- Profile set-up, one question at a time (profile-steps.js) ----------
def test_profile_set_up_step_by_step(page, base_url):
    page.click("#usermenu-btn")
    page.click("text=Set up my profile")
    page.wait_for_selector("#q-card-age.q-card--open")
    assert page.locator("fieldset.q-card--open").count() == 1                       # one question open at a time
    assert page.locator("fieldset.q-card legend").all_inner_texts()[:3] == [
        "Your age (optional)", "Risk level (required)", "Experience (required)"]
    page.fill("#q-age", "17")
    page.click("button[aria-label='Save age']")
    page.wait_for_selector("text=between 18 and 120")                               # inline error, focus stays
    assert page.evaluate("document.activeElement.id") == "q-age"
    page.fill("#q-age", "41")
    page.click("button[aria-label='Save age']")
    page.wait_for_selector("#q-card-age.q-card--locked")
    assert page.input_value("#q-age") == "41" and page.is_disabled("#q-age")        # the saved answer stays in its card
    assert page.evaluate("document.activeElement.id") == "q-risk_band"             # focus moved to the next question
    assert page.locator("#q-card-age .q-card__actions button").all_inner_texts() == ["Change"]
    page.fill("#q-risk_band", "7")
    page.wait_for_selector("text=7 / 10 · Moderate")
    page.click("button[aria-label='Save risk level']")
    page.fill("#q-experience", "2")
    page.click("button[aria-label='Save experience']")
    page.wait_for_selector("#q-card-experience.q-card--locked")
    page.click("button[aria-label='Save learning explanations']")                    # off by default: a valid answer
    page.fill("#q-horizon_min", "12")
    page.click("button[aria-label='Save investment horizon']")
    page.click("button[aria-label='Skip capital and largest loss']")
    page.fill("#q-objective", "a house")
    page.click("button[aria-label='Save goal']")
    page.fill("#q-preferences_text", "I want some diversification and not only tech; little interest in very speculative investments.")
    page.click("button[aria-label='Save preferences and constraints']")
    page.wait_for_selector(".understood:has-text('AlphaForge understood')")
    understood = page.locator(".understood").inner_text()
    assert "I want some diversification" in understood and "not only tech" in understood
    page.check("input[name='limit-crypto']")
    page.click("button[aria-label='Save hard limits']")
    page.click("button[aria-label='Save analyses']")
    page.wait_for_selector("#q-card-analyses.q-card--locked")
    assert page.locator(".msg, .msg__bubble").count() == 0                           # no answer is echoed as a bubble
    actor = page.evaluate("JSON.parse(localStorage.getItem('af.actor'))")
    saved = SERVICE.get_profile(actor)
    assert (saved.age, saved.risk_band, saved.experience, saved.horizon_years.min) == (41, "moderate", "beginner", 12)
    assert saved.preferences == ["I want some diversification"]
    assert saved.constraints == ["not only tech", "little interest in very speculative investments"]
    assert [e.value for e in saved.exclusions] == ["crypto"]
    # Back later: every answer is locked with its Change button
    page.goto(page.url.split("#")[0] + "#/portfolio")   # (a full reload would give this test a new random user)
    page.goto(page.url.split("#")[0] + "#/profile")
    page.wait_for_selector("fieldset.q-card--locked")
    assert page.locator("fieldset.q-card--locked").count() == 10
    assert page.locator("fieldset.q-card button:has-text('Change')").count() == 10
    page.click("button[aria-label='Change goal']")
    assert page.locator("#q-card-objective .q-card__actions button").all_inner_texts() == ["Cancel", "Save"]
    assert page.locator("fieldset.q-card--locked").count() == 9                     # only that card is unlocked
    page.fill("#q-objective", "retire")
    page.click("button[aria-label='Cancel changing goal']")
    assert page.input_value("#q-objective") == "a house"
    page.click("button[aria-label='Change goal']")
    page.fill("#q-objective", "retire")
    page.click("button[aria-label='Save goal']")
    page.wait_for_selector("#q-card-objective.q-card--locked")
    assert SERVICE.get_profile(actor).objective == "retire"
    page.goto(page.url.split("#")[0] + "#/ask")                                       # the Ask page still works
    page.wait_for_selector("#question")
    assert page.errors == []


def test_floating_ask_button(page, base_url):
    page.goto(page.url.split("#")[0] + "#/portfolio")
    fab = page.locator("#ask-fab")
    fab.wait_for(state="visible")
    assert fab.get_attribute("href") == "#/ask"
    fab.click()
    page.wait_for_selector("#question")
    assert fab.is_hidden()                                                        # not on the Ask page itself
    assert page.errors == []


def test_my_portfolio_questions_know_the_portfolio_without_opening_the_dashboard(page, base_url):
    """Built while the user was elsewhere: the chat saves it in the profile before sending a question."""
    from app.api.service import MessageIn  # noqa: F401
    run = _portfolio_for(page)
    page.goto(page.url.split("#")[0] + "#/profile")                        # anywhere but the dashboard
    page.click("a.nav__link:has-text('Ask a question')")
    page.wait_for_selector("text=SPY 35%, XBB.TO 31%, XIU.TO 25%, QQQ 10%")      # synced into the profile panel
    page.fill("#question", "Is my portfolio too risky compared to SPY?")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Analysed on your saved portfolio")
    asked = [r for r in SERVICE.runs.list_for_actor(run.actor_id, 10) if r.request.user_message.startswith("Is my portfolio")][0]
    assert {p.ticker: p.weight for p in asked.request.positions} == {"SPY": 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, "QQQ": 0.0956}
    assert page.errors == []


def test_profile_page_is_drawn_once_when_the_user_navigates_during_its_load(page, base_url):
    """Leaving and coming back while the profile loads used to add the form twice (Hard limits, Analyses twice)."""
    page.add_init_script("""const f = window.fetch; window.fetch = async (u, o) => {
      if (String(u).includes('/v1/personas')) await new Promise((r) => setTimeout(r, 700)); return f(u, o); };""")
    fill_profile_form(page, risk=6, experience=5)
    page.goto(page.url.split("#")[0] + "#/portfolio")
    for h in ("#/profile", "#/ask", "#/profile"):
        page.evaluate(f"window.location.hash = '{h}'")
        page.wait_for_timeout(120)
    page.wait_for_selector("text=Hard limits")
    page.wait_for_timeout(1500)
    assert page.locator("main legend:has-text('Hard limits')").count() == 1
    assert page.locator("main legend:has-text('Analyses')").count() == 1
    assert page.errors == []


def test_esg_screening_and_controversies_are_one_hard_limit(page, base_url):
    run = _portfolio_for(page)                                                     # Mateo: no major controversies
    page.goto(page.url.split("#")[0] + "#/profile")
    page.wait_for_selector("input[name=limit-esg]")
    page.wait_for_timeout(300)
    assert page.is_checked("input[name=limit-esg]")
    assert page.locator("input[name=limit-fossil], #q-toggle-esg").count() == 0      # no fossil limit, no ESG switch
    assert page.locator("main label:has-text('ESG')").count() == 1                 # ESG is named once, in the limits
    page.click("button[aria-label='Change hard limits']")
    page.uncheck("input[name=limit-esg]")
    page.click("button[aria-label='Save hard limits']")
    for _ in range(50):
        saved = SERVICE.profiles.get(run.actor_id)
        if not saved.exclusions:
            break
        page.wait_for_timeout(100)
    assert saved.exclusions == [] and saved.agent_toggles.esg is False       # the one setting turns both off
    assert page.errors == []


def test_switching_an_analysis_off_does_not_rebuild(page, base_url):
    run = _portfolio_for(page)
    page.goto(page.url.split("#")[0] + "#/profile")
    page.wait_for_selector("#q-toggle-news")
    page.click("button[aria-label='Change analyses']")
    page.uncheck("#q-toggle-news")
    page.click("button[aria-label='Save analyses']")
    page.wait_for_selector("#q-card-analyses.q-card--locked")                      # saved, and no rebuild
    page.wait_for_timeout(500)
    assert [r.request.user_message for r in SERVICE.runs.list_for_actor(run.actor_id, 20)].count(
        "Build a simulated portfolio that fits my profile.") == 1                  # no new build
    assert page.errors == []


def test_without_sign_in_settings_there_is_no_guest_and_no_app(base_url):
    """No config.js (or one without Cognito): the page shows nothing of the app and creates no guest user."""
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001  (browser binary not installed)
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        pg = browser.new_page()
        calls = []
        pg.on("request", lambda r: calls.append(r.url) if "/v1/" in r.url else None)
        pg.route("**/config.js", NO_CONFIG)
        pg.goto(f"{base_url}/#/portfolio")
        pg.wait_for_selector("text=Sign-in isn't available")
        assert pg.is_hidden(".nav") and pg.inner_text("#who") == ""
        assert "guest" not in pg.inner_text("body").lower() and "demo user" not in pg.inner_text("body").lower()
        assert pg.evaluate("localStorage.getItem('af.actor')") is None and calls == []
        pg.goto(f"{base_url}/#/ask")
        pg.wait_for_selector("text=Sign-in isn't available")
        browser.close()


# ---------- User menu and My profile settings ----------

def test_user_menu_opens_both_pages_by_mouse_and_keyboard(page, base_url):
    btn = page.locator("#usermenu-btn")
    assert btn.get_attribute("aria-expanded") == "false" and page.is_hidden("#usermenu-list")
    btn.click()
    assert btn.get_attribute("aria-expanded") == "true"
    page.click("#usermenu-list >> text=My profile settings")
    page.wait_for_selector("h1:has-text('My profile settings')")
    assert page.is_hidden("#usermenu-list") and btn.get_attribute("aria-current") == "page"
    btn.focus()
    page.keyboard.press("ArrowDown")                                   # opens and focuses the first item
    assert page.evaluate("document.activeElement.textContent") == "Set up my profile"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.textContent") == "My profile settings"
    page.keyboard.press("Escape")
    assert page.is_hidden("#usermenu-list") and page.evaluate("document.activeElement.id") == "usermenu-btn"
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")                                       # first item: Set up my profile
    page.wait_for_function("location.hash.startsWith('#/profile')")
    page.mouse.click(5, 5)
    assert page.is_hidden("#usermenu-list")
    assert page.errors == []


def test_settings_delete_account_erases_the_data(page, base_url):
    actor = page.evaluate("JSON.parse(localStorage.getItem('af.actor'))")
    from app.schemas import Profile

    SERVICE.profiles.put(Profile(actor_id=actor, risk_band="moderate", experience="beginner", language="en"))
    page.goto(f"{base_url}/#/settings")
    page.wait_for_selector("section[aria-labelledby=set-delete-title]")
    assert page.locator("text=Local test mode").count() == 1                    # no Cognito account here
    page.click("button:has-text('Delete my account')")
    page.wait_for_selector("text=Type DELETE in capital letters.")
    assert SERVICE.profiles.get(actor) is not None                               # nothing deleted yet
    page.fill("#set-delete-confirm", "DELETE")
    page.once("dialog", lambda d: d.accept())
    page.click("button:has-text('Delete my account')")
    page.wait_for_function("location.hash.startsWith('#/profile')")
    assert SERVICE.profiles.get(actor) is None
    assert page.errors == []


def test_signed_in_account_changes_use_cognito_with_the_access_token(base_url, monkeypatch):
    import json
    import time as _time

    import jwt
    from app.api import auth
    from app.schemas import Profile
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    region, pool, client = "us-east-1", "us-east-1_E2E", "e2e-client"
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"

    class Keys:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()

    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    monkeypatch.setattr(auth, "verifier_from_env", lambda: auth.CognitoVerifier(region, pool, client, jwk_client=Keys()))
    now = int(_time.time())
    id_token = jwt.encode({"sub": "sub-settings", "email": "me@example.com", "iss": issuer, "aud": client,
                           "token_use": "id", "iat": now, "exp": now + 3600}, key, algorithm="RS256")
    SERVICE.profiles.put(Profile(actor_id="sub-settings", risk_band="moderate", experience="beginner", language="en"))
    config = {"auth": "cognito", "region": region, "userPoolId": pool, "clientId": client, "demoAccounts": []}
    calls = []

    def fake_cognito(route):
        target = route.request.headers.get("x-amz-target", "").split(".")[-1]
        calls.append((target, json.loads(route.request.post_data or "{}")))
        body = {"InitiateAuth": {"AuthenticationResult": {"IdToken": id_token, "AccessToken": "access-1",
                                                          "RefreshToken": "refresh", "ExpiresIn": 3600}},
                "ChangePassword": {}, "UpdateUserAttributes": {"CodeDeliveryDetailsList": []},
                "VerifyUserAttribute": {}, "DeleteUser": {}, "RevokeToken": {}}.get(target, {})
        if target == "ChangePassword" and json.loads(route.request.post_data)["PreviousPassword"] == "Wrong-pass1":
            route.fulfill(status=400, content_type="application/x-amz-json-1.1",
                          body=json.dumps({"__type": "NotAuthorizedException", "message": "Incorrect username or password."}))
            return
        route.fulfill(status=200, content_type="application/x-amz-json-1.1", body=json.dumps(body))

    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001  (browser binary not installed)
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route("**/config.js", lambda r: r.fulfill(status=200, content_type="application/javascript",
                                                     body=f"window.AF_CONFIG = {json.dumps(config)};"))
        pg.route("https://cognito-idp.us-east-1.amazonaws.com/", fake_cognito)
        pg.goto(f"{base_url}/#/settings")
        pg.fill("#auth-email", "me@example.com")
        pg.fill("#auth-password", "Right-pass1")
        pg.click("form.auth-form button[type=submit]")
        pg.wait_for_selector("h1:has-text('My profile settings')")                    # back to the page asked for
        assert pg.locator("text=You sign in with me@example.com.").count() == 1
        # password: the wrong current password is said plainly; the right one goes with the ACCESS token, no ClientId
        pg.fill("#set-pw-current", "Wrong-pass1")
        pg.fill("#set-pw-new", "Brand-new-pass2")
        pg.click("button:has-text('Change password')")
        pg.wait_for_selector("text=The current password is incorrect.")
        pg.fill("#set-pw-current", "Right-pass1")
        pg.click("button:has-text('Change password')")
        pg.wait_for_selector("text=Your password was changed.")
        change = [b for t, b in calls if t == "ChangePassword"][-1]
        assert change["AccessToken"] == "access-1" and "ClientId" not in change and change["ProposedPassword"] == "Brand-new-pass2"
        # email: new address, then the code sent to it
        pg.fill("#set-email-new", "new@example.com")
        pg.click("button:has-text('Change email')")
        pg.wait_for_selector("text=Enter the code we sent to new@example.com.")
        pg.fill("#set-email-code", "123456")
        pg.click("button:has-text('Confirm new email')")
        pg.wait_for_selector("text=Done: you now sign in with new@example.com.")
        assert [b["UserAttributes"] for t, b in calls if t == "UpdateUserAttributes"] == [[{"Name": "email", "Value": "new@example.com"}]]
        assert [b["Code"] for t, b in calls if t == "VerifyUserAttribute"] == ["123456"]
        # delete: the app's data first (API), then the sign-in account (Cognito DeleteUser), then the sign-in page
        pg.fill("#set-delete-confirm", "DELETE")
        pg.once("dialog", lambda d: d.accept())
        pg.click("button:has-text('Delete my account')")
        pg.wait_for_selector("text=Your account and its data were deleted.")
        assert SERVICE.profiles.get("sub-settings") is None
        assert [b for t, b in calls if t == "DeleteUser"] == [{"AccessToken": "access-1"}]
        assert pg.evaluate("localStorage.getItem('af.session')") is None and pg.is_hidden(".nav")
        browser.close()
    assert errors == []
