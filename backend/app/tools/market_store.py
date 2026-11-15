"""Local market cache: the S3 market tables — the very files the graph loader feeds Neptune —
synced into data/market, so LOCAL tools serve the live snapshot in-process: milliseconds, no
gateway hop, no Lambda cold start. Freshness stays honest: manifest_close.json is fetched
alongside the parquets, so the 12h rule keys off the refresh's generated_at, never the download
time (sync_from_s3 skips .json files; parquet mtimes alone would always look fresh).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def market_uri() -> str | None:
    """s3://<bucket>/market/ from AF_MARKET_S3, else the deploy state (build/graph.json).
    An explicitly EMPTY AF_MARKET_S3 disables the sync (tests, offline runs)."""
    uri = os.environ.get("AF_MARKET_S3", "").strip()
    if uri:
        return uri
    if "AF_MARKET_S3" in os.environ:
        return None                     # explicitly disabled
    try:
        return json.loads((REPO_ROOT / "build" / "graph.json").read_text(encoding="utf-8")).get("market_s3")
    except (OSError, ValueError):
        return None


_sync_lock = threading.Lock()
_synced = False


def ensure_market_cache(local_dir: str | Path, uri: str | None = None) -> Path:
    """Bring data/market in step with S3 ONCE PER PROCESS (the graph caches the frames after
    load; re-syncing mid-process would only race the warm-up thread). Returns the dir; raises
    when the sync fails and no usable cache exists (callers fall back, the freshness rule
    still judges).

    A same-size file may have new content: start clean, same as the graph loader's /tmp pattern.
    """
    global _synced
    local_dir = Path(local_dir)
    uri = uri or market_uri()
    if not uri:
        if any(local_dir.glob("market_*.parquet")):
            return local_dir          # tables produced locally by app.market.refresh: already the truth
        raise FileNotFoundError("no market cache and no market URI (AF_MARKET_S3 / build/graph.json)")
    with _sync_lock:                  # the warm-up thread and the first tool call share this
        if _synced:
            return local_dir
        from app.tools.curated_store import sync_from_s3

        shutil.rmtree(local_dir, ignore_errors=True)
        sync_from_s3(uri, local_dir)
        _fetch_manifest(uri, local_dir)
        _synced = True
        return local_dir


def _fetch_manifest(uri: str, local_dir: Path) -> None:
    """Download manifest_close.json (sync_from_s3 skips .json): its generated_at is the load time
    the 12h freshness rule keys off."""
    from urllib.parse import urlparse

    u = urlparse(uri)
    import boto3

    s3 = boto3.client("s3")
    key = f"{u.path.strip('/')}/manifest_close.json".strip("/")
    obj = s3.get_object(Bucket=u.netloc, Key=key)
    (local_dir / "manifest_close.json").write_bytes(obj["Body"].read())


def local_market_graph(curated_dir, market_dir, require_tables: bool = False):
    """The in-process graph for LOCAL tools, or None when the source should stay remote
    (AF_NEPTUNE_ENDPOINT set: the Gateway's NeptuneGraph is the source then).

    Best-effort cache sync first; when the sync fails but an older cache exists, serve it —
    the freshness rule decides honestly whether it may answer. With require_tables, None is
    also returned when only the curated fallback graph is available (callers that have an
    equivalent curated path, like esg_screen, then read their own source directly).
    """
    if os.environ.get("AF_NEPTUNE_ENDPOINT"):
        return None
    try:
        ensure_market_cache(market_dir)
    except Exception as e:  # noqa: BLE001  offline / no URI: use the cache as it stands
        log.warning("market cache sync unavailable (%s): serving data/market as it stands", e)
    from app.market.graph import backend

    g = backend(curated_dir, market_dir)
    if require_tables and str(getattr(g, "spec", {}).get("source", "")).startswith("curated"):
        return None                     # no synced market tables: caller's curated path is equivalent
    return g


# ---------- warm-up at intake ----------

_warm_lock = threading.Lock()
_warmed = False


def kick_off_market_warmup(quant_function: str | None = None) -> bool:
    """Fired when a request reaches the intaker: sync the local market cache and probe the quant
    Lambda once, on a daemon thread — so Neptune Serverless's wake (~9s after idle) and the
    container init happen during the intake LLM call instead of during the sub-agent forks.

    Idempotent per process (the first caller wins). Never blocks, never raises into the caller.
    Effective in long-lived processes (the API backend, the smoke script); in the intake Lambda
    the thread dies with the invocation, which is harmless.
    """
    global _warmed
    if _warmed:
        return False
    with _warm_lock:
        if _warmed:
            return False
        _warmed = True
    threading.Thread(target=_warm_now, kwargs={"quant_function": quant_function},
                     name="af-market-warmup", daemon=True).start()
    return True


def _warm_now(market_dir=None, quant_function: str | None = None) -> None:
    s = get_settings()
    try:
        ensure_market_cache(Path(market_dir) if market_dir else s.abs_path(s.market_dir))
        log.info("market cache warm")
    except Exception as e:  # noqa: BLE001  offline / no URI: the freshness rule judges later
        log.warning("market cache warm-up skipped: %s", e)
    try:
        import boto3

        fn = quant_function or os.environ.get("AF_QUANT_FUNCTION", "alphaforge-quant-tools")
        boto3.client("lambda").invoke(
            FunctionName=fn,
            Payload=json.dumps({"__tool": "market___screen", "limit": 1}))
        log.info("quant tools Lambda warm")
    except Exception as e:  # noqa: BLE001  no creds / function absent: sub-agents warm it themselves
        log.warning("quant Lambda warm-up skipped: %s", e)
