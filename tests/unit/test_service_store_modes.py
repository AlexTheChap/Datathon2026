"""service_from_env: AF_STORE=file keeps profiles on disk, so they survive a restart."""
from pathlib import Path

import pytest
from app.api.service import service_from_env
from app.store.profiles import JsonFileProfileStore
from app.store.runs import InMemoryRunStore


def test_file_mode_keeps_profiles_across_a_restart(tmp_path):
    env = {"AF_STORE": "file", "AF_DATA_DIR": str(tmp_path / "store"), "AF_DISPATCH": "none"}
    svc = service_from_env(env)
    assert isinstance(svc.profiles, JsonFileProfileStore)
    assert isinstance(svc.runs, InMemoryRunStore)              # documented: runs are not persisted in file mode
    svc.put_profile("alice", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    assert (tmp_path / "store" / "profiles" / "alice.json").is_file()
    again = service_from_env(env)                               # "restart"
    assert again.get_profile("alice").risk_band == "moderate"


def test_default_data_dir_is_the_repo_local_af_store(monkeypatch):
    from app.core.config import REPO_ROOT

    monkeypatch.setattr(JsonFileProfileStore, "__init__", lambda self, d: setattr(self, "dir", Path(d)))
    svc = service_from_env({"AF_STORE": "file", "AF_DISPATCH": "none"})
    assert svc.profiles.dir == REPO_ROOT / ".af_store" / "profiles"


def test_unknown_store_is_rejected():
    with pytest.raises(ValueError, match="memory, file or dynamodb"):
        service_from_env({"AF_STORE": "postgres"})
