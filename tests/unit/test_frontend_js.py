"""Runs the web app's JavaScript unit tests (tests/js/*.test.mjs) with node, when node is installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_unit_tests_pass():
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    out = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-2000:]
