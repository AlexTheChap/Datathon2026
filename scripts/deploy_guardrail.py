"""Bedrock Guardrail "alphaforge": the intake's input filter and the Explainer's output check (L18).

  .venv/bin/python scripts/deploy_guardrail.py [--write-env] [--test]   # idempotent: updates it and adds a version
  .venv/bin/python scripts/deploy_guardrail.py --test-only
  .venv/bin/python scripts/deploy_guardrail.py --delete

One guardrail, two uses:
- INPUT (app.intake.backends.BedrockBackend): prompt attacks; secrets and identity numbers blocked.
- OUTPUT (app.agents.output_guardrail): contextual GROUNDING (each statement supported by the evidence it cites);
  denied topics, OUTPUT ONLY, so a user asking "is it guaranteed?" is not refused; contact details anonymized.
Thresholds: --grounding (default 0.7; too many correct paraphrases removed → lower it). RELEVANCE is off by default
(--relevance 0): checked one statement at a time, a correct fact ("volatility was 51%") scored 0.19 against
"Should I put 20% in Nvidia?" in the live test (2026-11-15), so it would remove most good statements.

Writes build/guardrail.json; --write-env puts AF_GUARDRAIL_ID / AF_GUARDRAIL_VERSION into .env. The API Lambda already
has bedrock:ApplyGuardrail and receives both variables (scripts/deploy_api.py): redeploy it after a new version.
Credentials: --profile, otherwise the AWS_* variables, otherwise the repo's .env file (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _aws import BUILD, ROOT, load_dotenv  # noqa: E402

NAME = "alphaforge"
STATE = BUILD / "guardrail.json"
TAGS = {"project": "alphaforge", "component": "guardrail"}

BLOCKED_INPUT = "AlphaForge can't process this message. Please rephrase it without instructions to the system or personal identifiers."
BLOCKED_OUTPUT = "This part of the answer was withheld by a safety check."

TOPICS = [
    ("Guaranteed returns", "Promising or implying that an investment return, gain or outcome is certain, guaranteed or risk-free.",
     ["This allocation is guaranteed to make money.", "You cannot lose with this portfolio."]),
    ("Personal trade instruction", "Telling the reader to buy, sell or trade a specific security now, as a personal "
     "recommendation, rather than describing a simulated allocation.",
     ["You should buy Nvidia today.", "Sell all your bonds immediately."]),
    ("Tax or legal advice", "Giving individual tax or legal advice, such as how to file, declare or legally structure investments.",
     ["Put it in your TFSA to avoid paying tax on it.", "You can legally deduct these losses this way."]),
]
# Input: blocked (nobody needs to send these to a simulator). Output: anonymized.
PII_BLOCK = ["CREDIT_DEBIT_CARD_NUMBER", "CA_SOCIAL_INSURANCE_NUMBER", "US_SOCIAL_SECURITY_NUMBER", "PASSWORD",
             "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "INTERNATIONAL_BANK_ACCOUNT_NUMBER"]
PII_MASK_OUTPUT = ["EMAIL", "PHONE", "ADDRESS"]


def config(grounding: float, relevance: float) -> dict:
    return {
        "description": "AlphaForge: intake input filter and Explainer output grounding check (L18).",
        "contentPolicyConfig": {"filtersConfig": [
            {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}]},
        "topicPolicyConfig": {"topicsConfig": [
            {"name": n, "definition": d, "examples": ex, "type": "DENY",
             "inputEnabled": False, "outputEnabled": True, "outputAction": "BLOCK"} for n, d, ex in TOPICS]},
        "sensitiveInformationPolicyConfig": {"piiEntitiesConfig": (
            [{"type": t, "action": "BLOCK", "inputAction": "BLOCK", "outputAction": "BLOCK"} for t in PII_BLOCK] +
            [{"type": t, "action": "ANONYMIZE", "inputEnabled": False, "outputEnabled": True,
              "outputAction": "ANONYMIZE"} for t in PII_MASK_OUTPUT])},
        "contextualGroundingPolicyConfig": {"filtersConfig": [
            {"type": "GROUNDING", "threshold": grounding, "action": "BLOCK", "enabled": True}] + (
            [{"type": "RELEVANCE", "threshold": relevance, "action": "BLOCK", "enabled": True}] if relevance > 0 else [])},
        "blockedInputMessaging": BLOCKED_INPUT,
        "blockedOutputsMessaging": BLOCKED_OUTPUT,
    }


def find(bedrock) -> dict | None:
    for page in bedrock.get_paginator("list_guardrails").paginate():
        for g in page.get("guardrails", []):
            if g.get("name") == NAME:
                return g
    return None


def deploy(bedrock, grounding: float, relevance: float) -> tuple[str, str]:
    cfg = config(grounding, relevance)
    found = find(bedrock)
    if found:
        gid = found["id"]
        bedrock.update_guardrail(guardrailIdentifier=gid, name=NAME, **cfg)
        print(f"• Updated guardrail {gid}")
    else:
        tags = [{"key": k, "value": v} for k, v in TAGS.items()]  # Bedrock: lowercase keys, unlike IAM/Lambda
        gid = bedrock.create_guardrail(name=NAME, tags=tags, **cfg)["guardrailId"]
        print(f"• Created guardrail {gid}")
    version = bedrock.create_guardrail_version(guardrailIdentifier=gid,
                                               description=f"grounding {grounding} relevance {relevance}")["version"]
    print(f"• Version {version} (grounding >= {grounding}, relevance >= {relevance})")
    return gid, version


def wait_ready(bedrock, gid: str, version: str, timeout: int = 180) -> None:
    """ApplyGuardrail refuses a guardrail still CREATING / UPDATING / VERSIONING."""
    import time

    deadline = time.time() + timeout
    while True:
        status = bedrock.get_guardrail(guardrailIdentifier=gid, guardrailVersion=version)["status"]
        if status == "READY":
            return
        if status == "FAILED" or time.time() > deadline:
            raise SystemExit(f"Guardrail {gid} v{version} is {status}, not READY.")
        time.sleep(5)


def write_env(gid: str, version: str) -> Path:
    path = ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in (("AF_GUARDRAIL_ID", gid), ("AF_GUARDRAIL_VERSION", version)):
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, flags=re.MULTILINE):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.MULTILINE)
        else:
            text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def test(runtime, gid: str, version: str) -> bool:
    """A grounded statement must pass, an invented judgment and a guarantee must be blocked."""
    source = json.dumps({"id": "ev-test", "say": "NVDA annualized volatility over 2022 was 51%; max drawdown -24.2%."})
    question = "Should I put 20% in Nvidia?"
    cases = [("Nvidia's volatility over 2022 was 51% a year.", False),
             ("Nvidia is a low-risk, defensive holding for a cautious investor.", True),
             ("This allocation is guaranteed to make money.", True)]
    ok = True
    for text, should_block in cases:
        out = runtime.apply_guardrail(guardrailIdentifier=gid, guardrailVersion=version, source="OUTPUT", content=[
            {"text": {"text": source, "qualifiers": ["grounding_source"]}},
            {"text": {"text": question, "qualifiers": ["query"]}},
            {"text": {"text": text, "qualifiers": ["guard_content"]}}])
        blocked = out.get("action") == "GUARDRAIL_INTERVENED"
        scores = [(f["type"], round(f.get("score", 0), 2)) for a in out.get("assessments", [])
                  for f in (a.get("contextualGroundingPolicy") or {}).get("filters", [])]
        good = blocked == should_block
        ok &= good
        print(f"  {'OK ' if good else 'BAD'} {'blocked' if blocked else 'passed '} {scores} {text}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="AWS profile; otherwise AWS_* variables, otherwise .env")
    ap.add_argument("--region", default=None, help="default: AWS_REGION, otherwise us-east-1")
    ap.add_argument("--grounding", type=float, default=0.7)
    ap.add_argument("--relevance", type=float, default=0.0,
                    help="0 = off (default): one statement alone rarely 'answers' the question, so a correct fact "
                         "scored 0.19 in the live test and would be removed")
    ap.add_argument("--write-env", action="store_true", help="write AF_GUARDRAIL_ID / AF_GUARDRAIL_VERSION into .env")
    ap.add_argument("--test", action="store_true", help="after deploying: three ApplyGuardrail calls")
    ap.add_argument("--test-only", action="store_true")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    try:  # Windows: system certificates for botocore (before boto3 is imported)
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    if not args.profile:
        load_dotenv()
    import boto3

    region = args.region or os.environ.get("AWS_REGION") or "us-east-1"
    session = boto3.Session(profile_name=args.profile, region_name=region)
    bedrock = session.client("bedrock")
    print(f"• Account: {session.client('sts').get_caller_identity()['Account']}  region: {region}")

    if args.delete:
        found = find(bedrock)
        if found:
            bedrock.delete_guardrail(guardrailIdentifier=found["id"])
            print(f"• Deleted guardrail {found['id']} (unset AF_GUARDRAIL_ID in .env and in the API Lambda)")
        STATE.unlink(missing_ok=True)
        return
    if args.test_only:
        state = json.loads(STATE.read_text())
        wait_ready(bedrock, state["id"], state["version"])
        raise SystemExit(0 if test(session.client("bedrock-runtime"), state["id"], state["version"]) else 1)

    gid, version = deploy(bedrock, args.grounding, args.relevance)
    wait_ready(bedrock, gid, version)
    print("• READY")
    BUILD.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"id": gid, "version": version, "region": region,
                                 "grounding": args.grounding, "relevance": args.relevance}, indent=2))
    if args.write_env:
        print(f"• AF_GUARDRAIL_ID / AF_GUARDRAIL_VERSION written to {write_env(gid, version)}")
    else:
        print(f"  use it with: AF_GUARDRAIL_ID={gid} AF_GUARDRAIL_VERSION={version}   (or re-run with --write-env)")
    print("  then redeploy the API: python scripts/deploy_api.py")
    if args.test:
        raise SystemExit(0 if test(session.client("bedrock-runtime"), gid, version) else 1)


if __name__ == "__main__":
    main()
