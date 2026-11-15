"""scripts/_aws.py: the helpers shared by the deploy scripts, driven with fake boto3 clients (no AWS)."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import _aws  # noqa: E402


class _Calls:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class FakeIam(_Calls):
    class exceptions:
        class NoSuchEntityException(Exception):
            pass

    def __init__(self, existing: bool):
        super().__init__()
        self.existing = existing

    def get_role(self, RoleName):
        if not self.existing:
            raise self.exceptions.NoSuchEntityException()
        return {"Role": {"Arn": f"arn:existing/{RoleName}"}}

    def create_role(self, **kw):
        self.calls.append(("create_role", kw))
        return {"Role": {"Arn": f"arn:new/{kw['RoleName']}"}}


class FakeS3(_Calls):
    class exceptions:
        class ClientError(Exception):
            pass

    def __init__(self, existing: bool):
        super().__init__()
        self.existing = existing

    def head_bucket(self, Bucket):
        if not self.existing:
            raise self.exceptions.ClientError()

    def get_waiter(self, _name):
        return type("W", (), {"wait": lambda self, **kw: None})()

    def __getattr__(self, name):  # create_bucket, put_bucket_*: recorded
        return lambda **kw: self.calls.append((name, kw))


def test_ensure_role_tags_with_the_callers_component():
    iam = FakeIam(existing=False)
    arn, created = _aws.ensure_role(iam, "r", _aws.LAMBDA_TRUST, "desc", {"component": "api"})
    assert (arn, created) == ("arn:new/r", True)
    assert iam.calls[0][1]["Tags"] == [{"Key": "component", "Value": "api"}]
    assert _aws.ensure_role(FakeIam(existing=True), "r", _aws.LAMBDA_TRUST, "d", {}) == ("arn:existing/r", False)


def test_trust_policy_source_account_condition():
    assert "Condition" not in _aws.LAMBDA_TRUST["Statement"][0]
    st = _aws.trust_policy("scheduler.amazonaws.com", "123")["Statement"][0]
    assert st["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert st["Condition"] == {"StringEquals": {"aws:SourceAccount": "123"}}


def test_ensure_bucket_creates_then_always_hardens():
    s3 = FakeS3(existing=False)
    _aws.ensure_bucket(s3, "b", "ca-central-1", {"component": "x"}, versioning=True)
    assert s3.names() == ["create_bucket", "put_public_access_block", "put_bucket_encryption",
                          "put_bucket_versioning", "put_bucket_tagging"]
    assert s3.calls[0][1]["CreateBucketConfiguration"] == {"LocationConstraint": "ca-central-1"}

    s3 = FakeS3(existing=True)
    _aws.ensure_bucket(s3, "b", "us-east-1")
    assert s3.names() == ["put_public_access_block", "put_bucket_encryption"]


def test_zip_dir_skips_bytecode(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "app" / "__pycache__").mkdir(parents=True)
    (pkg / "app" / "m.py").write_text("x = 1")
    (pkg / "app" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"")
    (pkg / "app" / "stray.pyc").write_bytes(b"")
    out = _aws.zip_dir(pkg, tmp_path / "out.zip")
    assert zipfile.ZipFile(out).namelist() == ["app/m.py"]
