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
        for route in ("#/ask", "#/profile"):
            pg.goto(f"{base_url}/{route}")
            pg.wait_for_selector("h1")
            assert not pg.evaluate("document.documentElement.scrollWidth > window.innerWidth"), route
        browser.close()
