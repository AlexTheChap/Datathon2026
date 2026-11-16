"""Create (or reuse) Fitfolio's sign-in on Amazon Cognito (box ① login, link L1), with boto3 only.

    python scripts/setup_cognito.py            # create or update the user pool, the web client, 3 demo accounts
    python scripts/setup_cognito.py --test     # + sign in as each demo account and show the token's claims
    python scripts/setup_cognito.py --delete   # delete the user pool and every account in it

What it sets up:
- user pool "alphaforge-users": email as the username, email verified with a 6-digit code (Cognito's built-in
  sender), passwords of 10+ characters with upper case, lower case and a digit, recovery by email;
- app client "alphaforge-web" (public: no secret, the browser calls Cognito directly): USER_PASSWORD_AUTH +
  refresh, user-existence errors hidden, ID/access tokens valid 1 hour, refresh 7 days;
- demo accounts amira/mateo/robert.demo@alphaforge.example (a reserved domain, never a real mailbox), with one
  shared random password. The API gives each one its persona's profile on first sign-in.
  Re-running the script resets them (password, email): a visitor may have changed them, since the password is
  public. The web app and the API refuse email / password changes and deletion for these accounts.

Writes:
- backend/app/core/signin.json (TRACKED in git): region, user pool id, web client id. The app reads it by default,
  so every copy of the site has sign-in without any local setting. Not secrets: every visitor's browser gets them.
- .env (git-ignored): AF_DEMO_PASSWORD only (reused on the next run). The server adds the one-click demo accounts
  to the page when it is set. That password is public to anyone who can open the app, by design: those accounts
  only hold the brief's fictional profiles.
Credentials: --profile, otherwise the AWS_* variables, otherwise .env.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
SIGNIN_JSON = ROOT / "backend" / "app" / "core" / "signin.json"
PERSONAS = ROOT / "backend" / "app" / "core" / "personas.json"
POOL = "alphaforge-users"
CLIENT = "alphaforge-web"
DEMO_DOMAIN = "alphaforge.example"  # RFC 2606: reserved, can never receive mail
TAGS = {"project": "alphaforge", "component": "sign-in"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import load_dotenv  # noqa: E402  (same .env reader as the other deploy scripts)


def upsert_env(values: dict[str, str | None]) -> None:
    """Set (or remove, with None) keys in .env without touching the other lines and without printing values."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ") if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            seen.add(key)
            if values[key] is not None:
                out.append(f"{key}={values[key]}")
            continue
        out.append(line)
    new = [f"{k}={v}" for k, v in values.items() if k not in seen and v is not None]
    if new:
        out += ["", "# --- Sign-in (written by scripts/setup_cognito.py) ---", *new]
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def demo_password() -> str:
    """Reuse the one in .env, else a random one that meets the policy (upper, lower, digit, 16 chars)."""
    existing = os.environ.get("AF_DEMO_PASSWORD")
    if existing:
        return existing
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(16))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


VERIFICATION_MESSAGE = {
    "DefaultEmailOption": "CONFIRM_WITH_CODE",
    "EmailSubject": "Your Fitfolio code",
    "EmailMessage": "Your Fitfolio verification code is {####}. Fitfolio is a simulation; it never asks "
                    "for bank details.",
}
# Settings update_user_pool accepts; any of them left out is reset to its default, so the current ones are passed back.
_UPDATABLE = ("Policies", "DeletionProtection", "LambdaConfig", "AutoVerifiedAttributes", "SmsVerificationMessage",
              "EmailVerificationMessage", "EmailVerificationSubject", "SmsAuthenticationMessage",
              "UserAttributeUpdateSettings", "MfaConfiguration", "DeviceConfiguration", "EmailConfiguration",
              "SmsConfiguration", "UserPoolTags", "AdminCreateUserConfig", "UserPoolAddOns",
              "AccountRecoverySetting", "UserPoolTier")


def refresh_messages(idp, pool_id: str) -> None:
    """Put the current verification email on an existing pool (it is only set at creation otherwise)."""
    pool = idp.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    if pool.get("VerificationMessageTemplate", {}).get("EmailMessage") == VERIFICATION_MESSAGE["EmailMessage"]             and pool["VerificationMessageTemplate"].get("EmailSubject") == VERIFICATION_MESSAGE["EmailSubject"]:
        return
    current = {k: pool[k] for k in _UPDATABLE if k in pool}
    for k in ("EmailVerificationMessage", "EmailVerificationSubject"):  # superseded by the template
        current.pop(k, None)
    if current.get("MfaConfiguration") == "OFF":
        current.pop("SmsAuthenticationMessage", None)
    idp.update_user_pool(UserPoolId=pool_id, **current, VerificationMessageTemplate=VERIFICATION_MESSAGE)
    print("• Verification email updated (Fitfolio)")


def find_pool(idp) -> str | None:
    for page in idp.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == POOL:
                return p["Id"]
    return None


def ensure_pool(idp) -> str:
    pool_id = find_pool(idp)
    if pool_id:
        print(f"• Existing user pool: {pool_id}")
        refresh_messages(idp, pool_id)
        return pool_id
    pool_id = idp.create_user_pool(
        PoolName=POOL,
        Policies={"PasswordPolicy": {"MinimumLength": 10, "RequireUppercase": True, "RequireLowercase": True,
                                     "RequireNumbers": True, "RequireSymbols": False,
                                     "TemporaryPasswordValidityDays": 7}},
        UsernameAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        UsernameConfiguration={"CaseSensitive": False},
        AccountRecoverySetting={"RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]},
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": False},
        EmailConfiguration={"EmailSendingAccount": "COGNITO_DEFAULT"},
        VerificationMessageTemplate=VERIFICATION_MESSAGE,
        UserPoolTags=TAGS,
    )["UserPool"]["Id"]
    print(f"• User pool created: {pool_id}")
    return pool_id


def ensure_client(idp, pool_id: str) -> str:
    settings = dict(
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        PreventUserExistenceErrors="ENABLED",
        AccessTokenValidity=60, IdTokenValidity=60, RefreshTokenValidity=7,
        TokenValidityUnits={"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "days"},
        EnableTokenRevocation=True,
        ReadAttributes=["email", "email_verified"],
        WriteAttributes=["email"],
    )
    for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id, MaxResults=60):
        for c in page["UserPoolClients"]:
            if c["ClientName"] == CLIENT:
                idp.update_user_pool_client(UserPoolId=pool_id, ClientId=c["ClientId"], ClientName=CLIENT, **settings)
                print(f"• Existing app client (settings refreshed): {c['ClientId']}")
                return c["ClientId"]
    client_id = idp.create_user_pool_client(UserPoolId=pool_id, ClientName=CLIENT, GenerateSecret=False,
                                            **settings)["UserPoolClient"]["ClientId"]
    print(f"• App client created: {client_id}")
    return client_id


def ensure_demo_users(idp, pool_id: str, password: str) -> list[dict]:
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))["personas"]
    accounts = []
    for p in personas:
        email = f"{p['id']}.demo@{DEMO_DOMAIN}"
        try:
            idp.admin_create_user(UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": email},
                                                  {"Name": "email_verified", "Value": "true"}])
            state = "created"
        except idp.exceptions.UsernameExistsException:
            state = "exists"
            # Reset what a visitor may have changed (the password is public): the email is the demo marker.
            idp.admin_update_user_attributes(UserPoolId=pool_id, Username=email,
                                             UserAttributes=[{"Name": "email", "Value": email},
                                                             {"Name": "email_verified", "Value": "true"}])
        idp.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)
        accounts.append({"persona": p["id"], "name": p["id"].capitalize(), "email": email,
                         "summary": f"{p['profile_type']} investor, {p['age']}, {p['occupation'].lower()}."})
        print(f"• Demo account {state}: {email}")
    return accounts


def write_signin(region: str, pool_id: str | None, client_id: str | None) -> None:
    """The app's sign-in settings, tracked in git (commit the file so every copy of the site gets sign-in)."""
    data = {"_comment": "Fitfolio sign-in (Amazon Cognito user pool 'alphaforge-users', web client 'alphaforge-web'). "
                        "Not secrets: every visitor's browser receives these ids. Written by scripts/setup_cognito.py; "
                        "AF_COGNITO_* environment variables override them.",
            "region": region, "user_pool_id": pool_id, "client_id": client_id}
    SIGNIN_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"• Wrote {SIGNIN_JSON.relative_to(ROOT)} (commit it)")


def smoke(idp, client_id: str, accounts: list[dict], password: str) -> None:
    import base64

    for a in accounts:
        out = idp.initiate_auth(ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
                                AuthParameters={"USERNAME": a["email"], "PASSWORD": password})
        token = out["AuthenticationResult"]["IdToken"]
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        print(f"✓ {a['email']}: signed in · token_use={payload['token_use']} sub={payload['sub'][:8]}… "
              f"email_verified={payload.get('email_verified')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--region")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()
    if not args.profile:
        load_dotenv()
    import boto3

    region = args.region or os.environ.get("AWS_REGION") or "us-east-1"
    session = boto3.Session(profile_name=args.profile, region_name=region)
    print(f"• Account: {session.client('sts').get_caller_identity()['Account']}  region: {region}")
    idp = session.client("cognito-idp")

    if args.delete:
        pool_id = find_pool(idp)
        if pool_id:
            idp.delete_user_pool(UserPoolId=pool_id)
            print(f"• User pool deleted: {pool_id}")
        write_signin(region, None, None)
        upsert_env({"AF_DEMO_PASSWORD": None})
        print("• Sign-in settings removed: the site shows \"Sign-in isn't available\" until a pool exists again")
        return

    pool_id = ensure_pool(idp)
    client_id = ensure_client(idp, pool_id)
    password = demo_password()
    accounts = ensure_demo_users(idp, pool_id, password)
    write_signin(region, pool_id, client_id)
    upsert_env({"AF_DEMO_PASSWORD": password})
    print("• .env updated (AF_DEMO_PASSWORD, for the one-click demo accounts)")
    if args.test:
        smoke(idp, client_id, accounts, password)


if __name__ == "__main__":
    main()
