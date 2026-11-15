"""Text alerts in the web app (Playwright): the sign-up question and the profile page panel.

Skipped without Playwright / Chromium (pip install -e ".[ui]" && python -m playwright install chromium).
Cognito is faked at the network level for the sign-up form; the API runs for real in dev mode.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from app.api.app import create_app  # noqa: E402
from app.api.service import ApiService, NoDispatcher  # noqa: E402
from app.intake import MockBackend  # noqa: E402

SERVICE = ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())
COGNITO_CONFIG = ('window.AF_CONFIG = {auth: "cognito", region: "us-east-1", userPoolId: "us-east-1_TEST", '
                  'clientId: "testclient", demoAccounts: []};')


@pytest.fixture(scope="module")
def base_url():
    import os

    os.environ["AF_AUTH_MODE"] = "dev"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(SERVICE), host="127.0.0.1", port=port, log_level="warning"))
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


def test_profile_page_turns_text_alerts_on_and_off(base_url, browser):
    pg = new_page(browser)
    pg.add_init_script("localStorage.setItem('af.actor', JSON.stringify('e2e-sms'))")
    pg.goto(f"{base_url}/#/profile/form")
    panel = pg.locator("section[aria-labelledby=sms-title]")
    panel.wait_for()
    phone = panel.locator("#sms-phone")
    assert not phone.is_visible()                       # the number is asked only after "yes"
    panel.get_by_text("Text me news about the stocks I hold").click()
    assert phone.is_visible()
    phone.fill("12345")
    panel.get_by_role("button", name="Save text alerts").click()
    assert "Canadian or U.S. mobile number" in panel.locator("#sms-phone-error").inner_text()
    phone.fill("(514) 555-0123")
    panel.get_by_role("button", name="Save text alerts").click()
    pg.wait_for_function("document.querySelector('[aria-labelledby=sms-title] [role=status]').textContent.includes('on for')")
    assert SERVICE.contacts.get("e2e-sms").phone == "+15145550123"

    pg.reload()                                          # the choice is read back from the API
    panel = pg.locator("section[aria-labelledby=sms-title]")
    pg.wait_for_function("document.querySelector('#sms-phone') && document.querySelector('#sms-phone').value !== ''")
    assert panel.locator("#sms-opt-in").is_checked()
    panel.get_by_text("Text me news about the stocks I hold").click()
    panel.get_by_role("button", name="Save text alerts").click()
    pg.wait_for_function("document.querySelector('[aria-labelledby=sms-title] [role=status]').textContent.includes('deleted')")
    assert SERVICE.contacts.get("e2e-sms").phone is None
    assert not pg.errors, pg.errors


def test_sign_up_asks_for_texts_then_the_number(base_url, browser):
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
    assert not pg.locator("#auth-phone").is_visible()
    pg.get_by_text("Text me news about the stocks I hold").click()
    pg.locator("#auth-phone").fill("555")
    pg.get_by_role("button", name="Create account").click()
    assert "Canadian or U.S. mobile number" in pg.locator("#auth-phone-error").inner_text()
    assert not calls                                     # nothing sent to Cognito with a bad number
    pg.locator("#auth-phone").fill("438 555 0199")
    pg.get_by_role("button", name="Create account").click()
    pg.get_by_role("heading", name="Check your email").wait_for()
    assert calls[0][0].endswith("SignUp")
    assert "phone" not in json.dumps(calls[0][1]).lower()  # the number never goes to Cognito
    pending = json.loads(pg.evaluate("sessionStorage.getItem('af.sms.pending')"))
    assert pending == {"opt_in": True, "phone": "438 555 0199"}  # saved through the API once signed in
    assert not pg.errors, pg.errors
