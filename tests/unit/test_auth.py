"""Sign-in (box ② auth.py): Cognito ID tokens verified locally, demo accounts seeded. No AWS: a key pair made
here plays the user pool's signing key."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import auth
from app.api.app import create_app
from app.api.auth import AuthError, CognitoVerifier, Identity, identify
from app.api.service import ApiService, NoDispatcher
from app.intake import MockBackend

REGION, POOL, CLIENT = "us-east-1", "us-east-1_TestPool", "test-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"

POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    """Stands in for PyJWKClient: returns the pool's public key whatever the token says."""

    class _Key:
        key = POOL_KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Key()


def token(sub="user-123", email="ana@example.com", key=POOL_KEY, **override):
    now = int(time.time())
    claims = {"sub": sub, "email": email, "iss": ISSUER, "aud": CLIENT, "token_use": "id", "iat": now, "exp": now + 3600,
              **override}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256",
                      headers={"kid": "test-kid"})


@pytest.fixture
def verifier():
    return CognitoVerifier(REGION, POOL, CLIENT, jwk_client=FakeJWKS())


# ---------- Token verification ----------

def test_valid_id_token_gives_the_cognito_sub(verifier):
    assert verifier.verify(token()) == Identity(actor_id="user-123", email="ana@example.com")


@pytest.mark.parametrize("bad, why", [
    (lambda: token(key=OTHER_KEY), "forged signature"),
    (lambda: token(aud="another-app"), "token for another app client"),
    (lambda: token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"), "another user pool"),
    (lambda: token(token_use="access"), "access token instead of ID token"),
    (lambda: token(exp=int(time.time()) - 10), "expired"),
    (lambda: token(sub=None), "no subject"),
    (lambda: "not.a.jwt", "garbage"),
])
def test_bad_tokens_are_refused(verifier, bad, why):
    with pytest.raises(AuthError):
        verifier.verify(bad())


def test_expired_token_says_so(verifier):
    with pytest.raises(AuthError, match="expired"):
        verifier.verify(token(exp=int(time.time()) - 10))


def test_cognito_mode_needs_a_bearer_and_ignores_the_dev_header(verifier, monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    with pytest.raises(AuthError):
        identify(None, {"x-actor-id": "robert"}, verifier)
    assert identify(None, {"authorization": f"Bearer {token()}"}, verifier).actor_id == "user-123"


def test_api_gateway_claims_win(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "gw-sub", "email": "x@y.z"}}}}}
    assert identify(event, {}) == Identity(actor_id="gw-sub", email="x@y.z")


def test_unconfigured_server_refuses_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    monkeypatch.setenv("AF_SIGNIN_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("AF_COGNITO_USER_POOL_ID", raising=False)
    with pytest.raises(AuthError, match="not configured"):
        identify(None, {"authorization": f"Bearer {token()}"})


# ---------- Sign-in is part of the app: on by default, settings in the tracked signin.json ----------

def test_sign_in_is_on_by_default_and_never_dev(monkeypatch):
    monkeypatch.delenv("AF_AUTH_MODE", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert auth.auth_mode() == "cognito"
    with pytest.raises(AuthError):
        identify(None, {"x-actor-id": "guest-12345"})                  # the old guest trick gets nothing
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "alphaforge-api")
    assert auth.auth_mode() == "jwt"                                   # behind API Gateway's authorizer


def test_tracked_settings_are_used_and_the_environment_overrides_them(monkeypatch):
    import json as _json

    for k in ("AF_COGNITO_USER_POOL_ID", "AF_COGNITO_CLIENT_ID", "AF_COGNITO_REGION", "AF_SIGNIN_FILE"):
        monkeypatch.delenv(k, raising=False)
    tracked = _json.loads(auth.SIGNIN_FILE.read_text(encoding="utf-8"))
    assert tracked["user_pool_id"] and tracked["client_id"]             # committed: no local setting needed
    s = auth.signin_settings()
    assert s["user_pool_id"] == tracked["user_pool_id"] and s["client_id"] == tracked["client_id"]
    monkeypatch.setenv("AF_COGNITO_USER_POOL_ID", "us-east-1_OTHER")
    assert auth.signin_settings()["user_pool_id"] == "us-east-1_OTHER"


def test_browser_config_adds_demo_accounts_only_with_their_password(monkeypatch):
    monkeypatch.delenv("AF_COGNITO_USER_POOL_ID", raising=False)
    cfg = auth.browser_config()
    assert cfg["auth"] == "cognito" and cfg["userPoolId"] and cfg["demoAccounts"] == []
    demo = auth.browser_config("Pw-1234567890a")
    assert {a["email"] for a in demo["demoAccounts"]} == {f"{n}.demo@alphaforge.example" for n in ("amira", "mateo", "robert")}


def test_config_js_needs_no_local_file(monkeypatch, tmp_path):
    """The page gets the sign-in settings from the API even when frontend/config.js does not exist."""
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    monkeypatch.setenv("AF_FRONTEND_DIR", str(tmp_path))
    monkeypatch.delenv("AF_AUTH_MODE", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AF_COGNITO_USER_POOL_ID", raising=False)
    r = TestClient(create_app(ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher()))).get("/config.js")
    assert '"auth": "cognito"' in r.text and auth.signin_settings()["user_pool_id"] in r.text


def test_demo_persona_only_for_the_demo_domain():
    assert Identity("s", "robert.demo@alphaforge.example").demo_persona == "robert"
    assert Identity("s", "ROBERT.demo@AlphaForge.example").demo_persona == "robert"
    assert Identity("s", "robert@gmail.com").demo_persona is None
    assert Identity("s", "evil.demo@alphaforge.example").demo_persona is None
    assert Identity("s", None).demo_persona is None


# ---------- Through the API ----------

@pytest.fixture
def client(monkeypatch, verifier):
    monkeypatch.setenv("AF_AUTH_MODE", "cognito")
    monkeypatch.setattr(auth, "verifier_from_env", lambda: verifier)
    return TestClient(create_app(ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())))


def bearer(**kw):
    return {"Authorization": f"Bearer {token(**kw)}"}


def test_routes_require_a_valid_token(client):
    assert client.get("/v1/profile").status_code == 401
    assert client.get("/v1/profile", headers={"X-Actor-Id": "robert"}).status_code == 401
    assert client.get("/v1/profile", headers=bearer(key=OTHER_KEY)).status_code == 401
    r = client.post("/v1/runs", json={"text": "hi"}, headers=bearer(exp=int(time.time()) - 5))
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    assert r.headers["www-authenticate"] == "Bearer"


def test_demo_account_starts_with_its_persona_profile(client):
    h = bearer(sub="sub-robert", email="robert.demo@alphaforge.example")
    me = client.get("/v1/me", headers=h).json()
    assert me == {"actor_id": "sub-robert", "email": "robert.demo@alphaforge.example", "demo_persona": "robert"}
    profile = client.get("/v1/profile", headers=h).json()
    assert (profile["risk_band"], profile["experience"], profile["capital"]) == ("low_moderate", "advanced", 400000)
    assert profile["exclusions"][0]["value"] == "crypto"
    r = client.post("/v1/runs", json={"text": "I'm thinking of putting 20% in Nvidia, everyone is talking about it."},
                    headers=h)
    assert r.status_code == 202  # ready straight away: no profile question


def test_demo_profile_changes_are_kept(client):
    h = bearer(sub="sub-amira", email="amira.demo@alphaforge.example")
    client.put("/v1/profile", json={"risk_band": "moderate"}, headers=h)
    assert client.get("/v1/profile", headers=h).json()["risk_band"] == "moderate"  # not re-seeded


def test_real_account_starts_empty(client):
    h = bearer(sub="sub-ana", email="ana@example.com")
    assert client.get("/v1/profile", headers=h).status_code == 404
    assert client.get("/v1/me", headers=h).json()["demo_persona"] is None


def test_users_cannot_see_each_others_runs(client):
    robert = bearer(sub="sub-robert", email="robert.demo@alphaforge.example")
    run_id = client.post("/v1/runs", json={"text": "Is my portfolio too risky?"}, headers=robert).json()["run_id"]
    assert client.get(f"/v1/runs/{run_id}", headers=robert).status_code == 200
    assert client.get(f"/v1/runs/{run_id}", headers=bearer(sub="sub-ana")).status_code == 404


# ---------- The page's sign-in config always matches the API's mode ----------

@pytest.fixture
def site(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (tmp_path / "config.js").write_text('window.AF_CONFIG = {"auth": "cognito"};', encoding="utf-8")
    monkeypatch.setenv("AF_FRONTEND_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("mode, auth", [("cognito", "cognito"), ("dev", "dev"), ("jwt", None)])
def test_config_js_matches_how_the_api_checks_callers(site, monkeypatch, mode, auth):
    """Cognito settings only when the API verifies Cognito tokens; the no-password test mode only with AF_AUTH_MODE=dev;
    otherwise no settings at all, and the page refuses to run (never a guest user)."""
    monkeypatch.setenv("AF_AUTH_MODE", mode)
    r = TestClient(create_app(ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher()))).get("/config.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-store"
    if auth is None:
        assert "AF_CONFIG" not in r.text
    else:
        assert f'"auth": "{auth}"' in r.text


def test_startup_log_names_the_sign_in_mode(monkeypatch, caplog):
    import logging

    from app.api.app import log_auth_mode
    for mode, words in [("cognito", "Amazon Cognito"), ("dev", "dev mode"), ("jwt", "get 401")]:
        monkeypatch.setenv("AF_AUTH_MODE", mode)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            log_auth_mode()
        assert words in caplog.text, mode


def test_web_app_files_are_revalidated_on_every_load():
    """A changed app.js or module must never be mixed with an old cached copy."""
    r = TestClient(create_app(ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher()))).get("/assets/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
