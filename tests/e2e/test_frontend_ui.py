"""Box ① ↔ ② in a real browser: the frontend served by the API, mock intake, no AWS.

Skipped unless Playwright and its Chromium are installed:
    pip install -e ".[ui]" && python -m playwright install chromium
"""
import socket
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from app.api.app import create_app  # noqa: E402
from app.api.service import ApiService  # noqa: E402
from app.intake import MockBackend  # noqa: E402


def NO_CONFIG(route):
    route.fulfill(status=200, content_type="application/javascript", body="")


@pytest.fixture(scope="module")
def base_url():
    import os

    os.environ["AF_AUTH_MODE"] = "dev"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(ApiService(intake_backend=MockBackend())),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def page(base_url):
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        ctx.route("**/config.js", NO_CONFIG)  # local sign-in config (git-ignored) must not leak into dev-mode tests
        ctx.add_init_script("localStorage.setItem('af.actor', JSON.stringify('e2e-' + Math.random().toString(36).slice(2)))")
        pg = ctx.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(f"{base_url}/#/ask")
        yield pg
        browser.close()


def test_new_user_answers_inline_then_gets_a_plan(page):
    page.wait_for_selector("text=No profile yet")
    page.fill("#question", "Is my portfolio too risky?")
    page.click("button:has-text('Ask')")
    page.wait_for_selector("text=AlphaForge needs one detail")
    page.click("label.choice:has-text('Low to moderate')")
    page.click("label.choice:has-text('Advanced')")
    page.click("button:has-text('Continue')")
    page.wait_for_selector("text=AlphaForge will look at: Portfolio risk.")
    page.wait_for_selector("text=Low to moderate")  # the side summary now shows the saved profile
    assert page.errors == []


def test_news_switched_off_in_chat_shows_in_plan_profile_and_can_be_undone(page):
    page.click("a.nav__link:has-text('Profile')")
    page.click("label.choice:has-text('Moderate')")
    page.click("label.choice:has-text('Beginner')")
    page.click("button:has-text('Save profile')")
    page.wait_for_selector("text=Saved at")

    page.click("a.nav__link:has-text('Ask')")
    page.fill("#question", "I don't care about the news. I'm thinking of putting 20% in Nvidia, everyone is talking about it.")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("text=News is now off in your profile")
    page.locator("a:has-text('See the plan')").last.click()
    page.wait_for_selector("text=What AlphaForge will look at")
    assert page.locator("text=Off because you said").count() == 1  # the plan quotes the user

    page.go_back()
    page.wait_for_selector("text=Analyses off")
    page.click("button:has-text('Undo')")
    page.wait_for_selector("text=News is back on")
    assert page.errors == []


def test_profile_validation_and_example_persona(page):
    page.click("a.nav__link:has-text('Profile')")
    page.wait_for_selector("text=Start from an example")
    page.click("button:has-text('Save profile')")  # nothing chosen yet
    page.wait_for_selector("text=Pick the risk level that fits you best.")
    page.fill("#horizon_min", "10")
    page.fill("#horizon_max", "3")
    page.click("button:has-text('Save profile')")
    page.wait_for_selector("text=The end can't be before the start.")
    page.click("button.example:has-text('Robert')")
    page.click("button:has-text('Save profile')")
    page.wait_for_selector("text=Saved at")
    assert page.is_checked("input[name='limit-crypto']")
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
        pg.route("**/config.js", NO_CONFIG)
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
        assert pg.is_checked("input[name='risk_band'][value='low_moderate']")  # persona profile seeded by the API
        assert pg.inner_text("#who") == "robert.demo@alphaforge.example"

        # A token the pool did not sign is refused by the API: the app signs out and says why
        session = json.loads(pg.evaluate("localStorage.getItem('af.session')"))
        session["idToken"] = id_token(other)
        pg.evaluate(f"localStorage.setItem('af.session', {json.dumps(json.dumps(session))})")
        pg.unroute("https://cognito-idp.us-east-1.amazonaws.com/")
        pg.route("https://cognito-idp.us-east-1.amazonaws.com/", lambda r: r.fulfill(
            status=400, content_type="application/x-amz-json-1.1", body=json.dumps({"__type": "NotAuthorizedException"})))
        pg.click("a.nav__link:has-text('Ask')")
        pg.fill("#question", "Is my portfolio too risky?")
        pg.click("form.composer button[type=submit]")  # the API sees the forged token and answers 401
        # Refused right after signing in: the app names the real cause instead of "session expired"
        pg.wait_for_selector("text=AlphaForge's server refused your sign-in")
        assert pg.evaluate("localStorage.getItem('af.session')") is None
        browser.close()
    assert "InitiateAuth" in calls and errors == []
