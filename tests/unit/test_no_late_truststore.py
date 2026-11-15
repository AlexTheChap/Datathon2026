"""truststore.inject_into_ssl() must never run inside the app: called after botocore is loaded, it recurses forever in
ssl.SSLContext.options and breaks every later HTTPS call of the process. truststore ships in the Lambda image (a
dependency of httpx2/httpcore2), so a late call in the intake's guardrail check took the deployed API down on
2026-11-15 ("Fitfolio can't reach its AI model"). Scripts may inject it at process start, before boto3 loads."""
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "backend" / "app"


def test_the_app_never_injects_truststore():
    offenders = [f"{p.relative_to(APP)}:{n}" for p in APP.rglob("*.py")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if "inject_into_ssl(" in line and not line.lstrip().startswith("#")]
    assert offenders == []
