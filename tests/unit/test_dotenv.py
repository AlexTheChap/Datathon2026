"""Local .env loader — injects without overriding the real environment."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.dotenv import load_dotenv as _load_dotenv


def test_injects_and_ignores_comments(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AF_TEST_A", raising=False)
    f = tmp_path / "e"
    f.write_text('# a comment\nAF_TEST_A="value"\nINVALID_LINE\n', encoding="utf-8")
    _load_dotenv(f)
    assert os.environ["AF_TEST_A"] == "value"


def test_real_environment_wins(tmp_path: Path):
    os.environ["AF_TEST_B"] = "real"
    f = tmp_path / "e"
    f.write_text("AF_TEST_B=file\n", encoding="utf-8")
    _load_dotenv(f)
    assert os.environ["AF_TEST_B"] == "real"


def test_missing_file_is_silent(tmp_path: Path):
    _load_dotenv(tmp_path / "does-not-exist")  # must not raise


def test_pasted_shell_prefixes_and_empty_values(tmp_path: Path, monkeypatch):
    for k in ("AF_TEST_C", "AF_TEST_D", "AF_TEST_E"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / "e"
    f.write_text('export AF_TEST_C=bash\n$env:AF_TEST_D="ps"\nAF_TEST_E=\n', encoding="utf-8")
    _load_dotenv(f)
    assert os.environ["AF_TEST_C"] == "bash" and os.environ["AF_TEST_D"] == "ps"
    assert "AF_TEST_E" not in os.environ
