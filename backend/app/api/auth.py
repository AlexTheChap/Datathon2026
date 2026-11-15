"""Who is calling (box ②, link L1): a verified Amazon Cognito ID token, nothing else, decides the actor.

Three sources, in this order:
1. Deployed behind API Gateway: its JWT authorizer already verified the token; the claims arrive in the
   Lambda event (`requestContext.authorizer.jwt.claims`).
2. Sign-in mode "cognito" (the default everywhere but AWS Lambda): `Authorization: Bearer <ID token>`,
   verified here: RS256 signature with the user pool's public keys (JWKS), issuer = the pool,
   audience = the web client, token_use = "id", not expired.
3. AF_AUTH_MODE=dev (local tests only, never a default): the X-Actor-Id header, no password at all.

The sign-in settings (region, user pool id, web client id) are part of the app: backend/app/core/signin.json,
tracked in git and written by scripts/setup_cognito.py. They are not secrets (every visitor's browser gets them).
AF_COGNITO_REGION / AF_COGNITO_USER_POOL_ID / AF_COGNITO_CLIENT_ID override them (another pool, tests).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

SIGNIN_FILE = Path(__file__).resolve().parents[1] / "core" / "signin.json"

DEMO_EMAIL = re.compile(r"^(amira|mateo|robert)\.demo@alphaforge\.example$")


@dataclass(frozen=True)
class Identity:
    actor_id: str
    email: Optional[str] = None

    @property
    def demo_persona(self) -> Optional[str]:
        """'robert' for robert.demo@alphaforge.example (accounts made by scripts/setup_cognito.py)."""
        m = DEMO_EMAIL.match((self.email or "").lower())
        return m.group(1) if m else None


class AuthError(Exception):
    """Missing or invalid credentials (→ 401). The message is safe to show."""


class CognitoVerifier:
    def __init__(self, region: str, user_pool_id: str, client_id: str, jwk_client: Any = None):
        import jwt

        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self.client_id = client_id
        # Public keys fetched once from the pool's JWKS and cached (keys rotate rarely).
        self.jwks = jwk_client or jwt.PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True)

    def verify(self, token: str) -> Identity:
        import jwt

        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer,
                                options={"require": ["exp", "iat", "sub", "token_use"]})
        except jwt.ExpiredSignatureError as e:
            raise AuthError("Your session has expired. Sign in again.") from e
        except jwt.PyJWTError as e:
            raise AuthError("Invalid sign-in token.") from e
        if claims.get("token_use") != "id":
            raise AuthError("Invalid sign-in token.")
        return Identity(actor_id=claims["sub"], email=claims.get("email"))


@lru_cache(maxsize=4)
def _verifier(region: str, pool: str, client: str) -> CognitoVerifier:
    return CognitoVerifier(region, pool, client)


def auth_mode() -> str:
    """How callers are identified. AF_AUTH_MODE when set; otherwise "jwt" on AWS Lambda (API Gateway's authorizer
    already checked the token) and "cognito" everywhere else. Never "dev" unless asked for explicitly."""
    mode = os.environ.get("AF_AUTH_MODE", "").strip().lower()
    if mode:
        return mode
    return "jwt" if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else "cognito"


def signin_settings() -> dict[str, str | None]:
    """{"region", "user_pool_id", "client_id"}: the environment wins, else backend/app/core/signin.json."""
    path = Path(os.environ.get("AF_SIGNIN_FILE") or SIGNIN_FILE)
    try:
        file = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        file = {}
    return {
        "region": os.environ.get("AF_COGNITO_REGION") or file.get("region") or os.environ.get("AWS_REGION") or "us-east-1",
        "user_pool_id": os.environ.get("AF_COGNITO_USER_POOL_ID") or file.get("user_pool_id") or None,
        "client_id": os.environ.get("AF_COGNITO_CLIENT_ID") or file.get("client_id") or None,
    }


def browser_config(demo_password: str | None = None) -> dict[str, Any] | None:
    """The page's sign-in settings (window.AF_CONFIG), or None when no pool is configured. The one-click demo
    accounts appear only when their shared password is known (AF_DEMO_PASSWORD, scripts/setup_cognito.py)."""
    s = signin_settings()
    if not s["user_pool_id"] or not s["client_id"]:
        return None
    demos = []
    if demo_password:
        personas = json.loads((Path(__file__).resolve().parents[1] / "core" / "personas.json")
                              .read_text(encoding="utf-8"))["personas"]
        demos = [{"persona": p["id"], "name": p["id"].capitalize(), "email": f"{p['id']}.demo@alphaforge.example",
                  "password": demo_password,
                  "summary": f"{p['profile_type']} investor, {p['age']}, {p['occupation'].lower()}."} for p in personas]
    return {"auth": "cognito", "region": s["region"], "userPoolId": s["user_pool_id"], "clientId": s["client_id"],
            "demoAccounts": demos}


def verifier_from_env() -> CognitoVerifier:
    s = signin_settings()
    if not s["user_pool_id"] or not s["client_id"]:
        raise AuthError("Sign-in is not configured on the server (backend/app/core/signin.json).")
    return _verifier(s["region"], s["user_pool_id"], s["client_id"])


def identify(event: Optional[dict], headers: Any, verifier: Optional[CognitoVerifier] = None) -> Identity:
    """Resolve the caller from the API Gateway event (if any) and the request headers. Raises AuthError."""
    claims = (((event or {}).get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
    if claims.get("sub"):
        return Identity(actor_id=str(claims["sub"]), email=claims.get("email"))

    mode = auth_mode()
    if mode == "cognito":
        auth = headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            raise AuthError("Sign in to continue.")
        return (verifier or verifier_from_env()).verify(auth.split(" ", 1)[1].strip())
    if mode == "dev" and headers.get("x-actor-id"):
        return Identity(actor_id=headers["x-actor-id"])
    raise AuthError("Sign in to continue.")
