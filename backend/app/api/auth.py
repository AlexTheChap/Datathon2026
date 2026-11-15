"""Who is calling (box ②, link L1): a verified Amazon Cognito ID token, nothing else, decides the actor.

Three sources, in this order:
1. Deployed behind API Gateway: its JWT authorizer already verified the token; the claims arrive in the
   Lambda event (`requestContext.authorizer.jwt.claims`).
2. AF_AUTH_MODE=cognito (local or any server without the authorizer): `Authorization: Bearer <ID token>`,
   verified here: RS256 signature with the user pool's public keys (JWKS), issuer = the pool,
   audience = the web client, token_use = "id", not expired.
3. AF_AUTH_MODE=dev (local tests only): the X-Actor-Id header, no password at all.

Settings: AF_COGNITO_REGION, AF_COGNITO_USER_POOL_ID, AF_COGNITO_CLIENT_ID (written by scripts/setup_cognito.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

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


def verifier_from_env() -> CognitoVerifier:
    region = os.environ.get("AF_COGNITO_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    pool, client = os.environ.get("AF_COGNITO_USER_POOL_ID"), os.environ.get("AF_COGNITO_CLIENT_ID")
    if not pool or not client:
        raise AuthError("Sign-in is not configured on the server (AF_COGNITO_USER_POOL_ID / AF_COGNITO_CLIENT_ID).")
    return _verifier(region, pool, client)


def identify(event: Optional[dict], headers: Any, verifier: Optional[CognitoVerifier] = None) -> Identity:
    """Resolve the caller from the API Gateway event (if any) and the request headers. Raises AuthError."""
    claims = (((event or {}).get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
    if claims.get("sub"):
        return Identity(actor_id=str(claims["sub"]), email=claims.get("email"))

    mode = os.environ.get("AF_AUTH_MODE", "jwt").lower()
    if mode == "cognito":
        auth = headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            raise AuthError("Sign in to continue.")
        return (verifier or verifier_from_env()).verify(auth.split(" ", 1)[1].strip())
    if mode == "dev" and headers.get("x-actor-id"):
        return Identity(actor_id=headers["x-actor-id"])
    raise AuthError("Sign in to continue.")
