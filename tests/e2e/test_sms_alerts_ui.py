"""Text alerts in the web app (Playwright): the sign-up question and the profile page panel.

Skipped without Playwright / Chromium (pip install -e ".[ui]" && python -m playwright install chromium).
Cognito is faked at the network level for the sign-up form; the API runs for real in dev mode.
"""
from __future__ import annotations

import json

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from app.api.service import ApiService, NoDispatcher  # noqa: E402
from app.intake import MockBackend  # noqa: E402

SERVICE = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
COGNITO_CONFIG = ('window.AF_CONFIG = {auth: "cognito", region: "us-east-1", userPoolId: "us-east-1_TEST", '
                  'clientId: "testclient", demoAccounts: []};')


@pytest.fixture
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary not installed
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


DEV_CONFIG = 'window.AF_CONFIG = {"auth": "dev"};'  # explicit local test mode (never a fallback)


def new_page(browser, config_js: str = DEV_CONFIG):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route("**/config.js", lambda r: r.fulfill(status=200, content_type="application/javascript", body=config_js))
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    return pg


def test_settings_page_turns_text_alerts_on_and_off(base_url, browser):
    pg = new_page(browser)
    pg.add_init_script("localStorage.setItem('af.actor', JSON.stringify('e2e-sms'))")
    pg.goto(f"{base_url}/#/settings")
    panel = pg.locator("section[aria-labelledby=set-notify-title]")
    panel.wait_for()
    phone = panel.locator("#set-notify-phone")
    assert not panel.locator("#set-notify-notify").is_checked()   # off until chosen
    panel.get_by_text("Notify me on news related to my portfolio").click()
    phone.fill("12345")
    panel.get_by_role("button", name="Save notifications").click()
    assert "Canadian or U.S. mobile number" in panel.locator("#set-notify-phone-error").inner_text()
    phone.fill("(514) 555-0123")
    panel.get_by_role("button", name="Save notifications").click()
    pg.wait_for_function("document.querySelector('[aria-labelledby=set-notify-title] [role=status]').textContent.includes('text message')")
    saved = SERVICE.contacts.get("e2e-sms")
    assert saved.phone == "+15145550123" and saved.channel == "sms" and saved.alerts == ["news"]
    pg.reload()                                          # the choice is read back from the API
    panel = pg.locator("section[aria-labelledby=set-notify-title]")
    pg.wait_for_function("document.querySelector('#set-notify-phone') && document.querySelector('#set-notify-phone').value !== ''")
    assert panel.locator("#set-notify-notify").is_checked()
    panel.get_by_text("Notify me on news related to my portfolio").click()   # unchecked = off
    panel.get_by_role("button", name="Save notifications").click()
    pg.wait_for_function("document.querySelector('[aria-labelledby=set-notify-title] [role=status]').textContent.includes('deleted')")
    assert SERVICE.contacts.get("e2e-sms").phone is None and not SERVICE.contacts.get("e2e-sms").opt_in
    assert not pg.errors, pg.errors


def test_sign_up_asks_phone_and_one_news_checkbox(base_url, browser):
    pg = new_page(browser, COGNITO_CONFIG)
    calls = []

    def cognito(route):
        target = route.request.headers.get("x-amz-target", "")
        calls.append((target, json.loads(route.request.post_data or "{}")))
        route.fulfill(status=200, content_type="application/x-amz-json-1.1",
                      body=json.dumps({"UserConfirmed": False, "UserSub": "sub-1"}))

    pg.route("https://cognito-idp.us-east-1.amazonaws.com/", cognito)
    pg.goto(f"{base_url}/#/signin")
    pg.get_by_role("button", name="Create an account").click()
    pg.locator("#auth-email").fill("new.person@example.com")
    pg.locator("#auth-password").fill("Abcdefgh12")
    pg.locator("#auth-phone").wait_for()
    pg.get_by_text("Notify me on news related to my portfolio").click()
    pg.locator("#auth-phone").fill("555")
    pg.get_by_role("button", name="Create account").click()
    assert "Canadian or U.S. mobile number" in pg.locator("#auth-phone-error").inner_text()
    assert not calls                                     # nothing sent to Cognito with a bad number
    pg.locator("#auth-phone").fill("438 555 0199")
    pg.get_by_role("button", name="Create account").click()
    pg.get_by_role("heading", name="Check your email").wait_for()
    assert calls[0][0].endswith("SignUp")
    assert "phone" not in json.dumps(calls[0][1]).lower()  # the number never goes to Cognito
    pending = json.loads(pg.evaluate("sessionStorage.getItem('af.notify.pending')"))
    assert pending == {"opt_in": True, "channel": "sms", "phone": "438 555 0199", "alerts": ["news"]}
    assert not pg.errors, pg.errors


def test_sign_up_without_any_alert_saves_no_choice(base_url, browser):
    pg = new_page(browser, COGNITO_CONFIG)
    pg.route("https://cognito-idp.us-east-1.amazonaws.com/", lambda r: r.fulfill(
        status=200, content_type="application/x-amz-json-1.1", body=json.dumps({"UserConfirmed": False})))
    pg.goto(f"{base_url}/#/signin")
    pg.get_by_role("button", name="Create an account").click()
    pg.locator("#auth-email").fill("quiet@example.com")
    pg.locator("#auth-password").fill("Abcdefgh12")
    pg.get_by_role("button", name="Create account").click()
    pg.get_by_role("heading", name="Check your email").wait_for()
    assert pg.evaluate("sessionStorage.getItem('af.notify.pending')") is None  # notifications are optional
