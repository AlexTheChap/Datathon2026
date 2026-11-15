"""Local market cache (app.tools.market_store): URI rules, S3 sync + manifest, fallback rules.
No AWS: sync_from_s3 and the S3 client are faked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools import market_store


def test_market_uri_env_overrides_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AF_MARKET_S3", "s3://b/market/")
    assert market_store.market_uri() == "s3://b/market/"
    monkeypatch.setenv("AF_MARKET_S3", "")                    # explicitly disabled (tests/offline)
    assert market_store.market_uri() is None
    monkeypatch.delenv("AF_MARKET_S3")
    monkeypatch.setattr(market_store, "REPO_ROOT", tmp_path)  # no build/graph.json here
    assert market_store.market_uri() is None
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "graph.json").write_text(json.dumps({"market_s3": "s3://state/market/"}))
    assert market_store.market_uri() == "s3://state/market/"


def test_ensure_without_uri_keeps_local_tables_and_fails_when_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AF_MARKET_S3", raising=False)
    monkeypatch.setattr(market_store, "REPO_ROOT", tmp_path)  # no deploy state
    d = tmp_path / "market"
    d.mkdir()
    (d / "market_metrics.parquet").write_bytes(b"x")
    assert market_store.ensure_market_cache(d) == d           # local tables are the truth already
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        market_store.ensure_market_cache(empty)


def test_ensure_syncs_parquets_and_fetches_the_manifest(monkeypatch, tmp_path):
    calls: dict = {}

    def fake_sync(uri, local_dir, client=None):
        calls["uri"] = uri
        calls["n"] = calls.get("n", 0) + 1
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "market_metrics.parquet").write_bytes(b"x")
        return ["market_metrics"]

    class FakeS3:
        def get_object(self, Bucket, Key):
            calls["obj"] = (Bucket, Key)
            return {"Body": self}

        def read(self):
            return b'{"generated_at": "2026-11-15T17:00:00-05:00"}'

    monkeypatch.setenv("AF_MARKET_S3", "s3://b/market/")
    monkeypatch.setattr("app.tools.curated_store.sync_from_s3", fake_sync)
    monkeypatch.setattr("boto3.client", lambda name, **k: FakeS3())
    monkeypatch.setattr(market_store, "_synced", False)
    d = market_store.ensure_market_cache(tmp_path / "market")
    assert (d / "market_metrics.parquet").exists()
    assert (d / "manifest_close.json").exists()               # the freshness stamp, fetched too
    assert calls["uri"] == "s3://b/market/" and calls["obj"] == ("b", "market/manifest_close.json")
    # once per process: the second call skips the sync entirely (no double download racing the warm-up)
    assert market_store.ensure_market_cache(d) == d and calls["n"] == 1


def test_local_market_graph_is_none_behind_the_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AF_NEPTUNE_ENDPOINT", "graph.example")
    assert market_store.local_market_graph("data/curated", tmp_path) is None


def test_local_market_graph_fallback_rules(monkeypatch, tmp_path):
    """No URI, no tables: market tools keep the curated fallback graph; esg_screen (require_tables)
    gets None so it reads its own curated source instead."""
    monkeypatch.delenv("AF_NEPTUNE_ENDPOINT", raising=False)
    monkeypatch.setattr(market_store, "REPO_ROOT", tmp_path)
    g = market_store.local_market_graph("data/curated", tmp_path / "market")
    assert g is not None and "fallback" in g.spec["source"]
    assert market_store.local_market_graph("data/curated", tmp_path / "market", require_tables=True) is None


# ---------- warm-up at intake ----------

@pytest.fixture
def warm_state(monkeypatch):
    monkeypatch.setattr(market_store, "_warmed", False)
    return []


def test_warm_now_syncs_and_probes_the_quant_lambda(monkeypatch, warm_state):
    monkeypatch.setattr(market_store, "ensure_market_cache", lambda *a, **k: warm_state.append("sync"))

    class FakeLambda:
        def invoke(self, **kw):
            warm_state.append(("invoke", kw["FunctionName"], json.loads(kw["Payload"])))

    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **k: FakeLambda())
    market_store._warm_now()
    assert "sync" in warm_state
    assert ("invoke", "alphaforge-quant-tools", {"__tool": "market___screen", "limit": 1}) in warm_state


def test_warm_now_never_raises_on_broken_infrastructure(monkeypatch, warm_state):
    monkeypatch.setattr(market_store, "ensure_market_cache", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **k: (_ for _ in ()).throw(OSError("no creds")))
    market_store._warm_now()          # must not raise


def test_kick_off_is_idempotent_per_process(monkeypatch, warm_state):
    started = []
    monkeypatch.setattr(market_store, "_warm_now", lambda **kw: started.append(kw))
    assert market_store.kick_off_market_warmup() is True      # first caller wins
    assert market_store.kick_off_market_warmup() is False     # second call in the same process: no-op
    assert started == [] or len(started) == 1                 # one thread at most
