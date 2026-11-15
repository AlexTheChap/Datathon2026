"""Market graph (app.market): S&P 500 universe, Yahoo refresh, metrics, graph tools, Neptune queries.

Offline: a fake downloader stands in for Yahoo, a recording stub stands in for Neptune.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.market import graph as mg
from app.market import refresh
from app.market.metrics import correlation_edges, daily_returns, market_metrics, risk_class
from app.market.neptune import NeptuneGraph
from app.market.universe import market_universe
from app.tools import quant_gateway as qg

CURATED = "data/curated"
TODAY = date(2026, 11, 13)  # a Friday


def fake_downloader(tickers, *, start=None, period=None, session=None):
    """Deterministic daily bars: one factor per sector (so same-sector stocks correlate) + noise.
    Adds a bar dated AFTER `TODAY` that the refresh must drop. Leaves one ticker out (missing)."""
    uni = market_universe(CURATED).set_index("ticker")
    days = pd.bdate_range(end=pd.Timestamp(TODAY) + pd.Timedelta(days=3),
                          periods=6 if period else 300)
    rng = np.random.default_rng(7)
    factors = {s: rng.normal(0, 0.012, len(days)) for s in uni["sector"].unique()}
    rows = []
    for i, t in enumerate(tickers):
        if t == "ZTS":
            continue
        vol = 0.004 + 0.02 * (i % 7) / 6
        r = factors[uni.loc[t, "sector"]] + rng.normal(0, vol, len(days))
        px = 100 * np.cumprod(1 + r)
        rows.append(pd.DataFrame({"date": days, "ticker": t, "open": px, "high": px, "low": px,
                                  "close": px, "adj_close": px, "volume": 1000.0}))
    return pd.concat(rows, ignore_index=True)


@pytest.fixture(scope="module")
def market_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("market")
    refresh.run_close(CURATED, out, downloader=fake_downloader, today=TODAY)
    return out


@pytest.fixture(scope="module")
def graph(market_dir):
    return mg.LocalGraph.from_dirs(CURATED, market_dir)


# ---------- universe + metrics ----------

def test_universe_is_the_curated_assets_plus_the_sp500():
    u = market_universe(CURATED)
    assert u["in_core_universe"].sum() == 31
    assert u["in_sp500"].sum() >= 500
    assert {"BRK-B", "NVDA", "RY.TO", "SPY", "BTC-USD"} <= set(u["ticker"])
    assert u["ticker"].is_unique
    assert u.loc[u["ticker"] == "RY.TO", "listing_country"].item() == "CA"
    assert u.loc[u["ticker"] == "NVDA", ["in_core_universe", "in_sp500"]].values.tolist() == [[True, True]]
    assert not u["ticker"].str.match(r"^\d").any()  # CUSIP-like lines of the holdings file are dropped


def test_risk_class_bands():
    assert [risk_class(v) for v in (0.05, 0.15, 0.30, 0.40, None)] == ["low", "moderate", "high", "very_high", None]


def test_returns_follow_the_us_session_calendar():
    days = pd.date_range("2026-01-02", periods=10, freq="D")  # includes a weekend
    rows = [{"date": d, "ticker": "BTC-USD", "adj_close": 100 + i} for i, d in enumerate(days)]
    rows += [{"date": d, "ticker": "SPY", "adj_close": 50 + i} for i, d in enumerate(days) if d.weekday() < 5]
    r = daily_returns(pd.DataFrame(rows))
    assert all(d.weekday() < 5 for d in r.index)              # no weekend row stretches the window
    monday = pd.Timestamp("2026-01-05")
    assert r.loc[monday, "BTC-USD"] == pytest.approx(103 / 100 - 1)  # Monday measured from Friday


def test_correlation_edges_keep_only_strong_pairs():
    rng = np.random.default_rng(0)
    f = rng.normal(size=300)
    rets = pd.DataFrame({"A": f, "B": f + rng.normal(0, 0.1, 300), "C": rng.normal(size=300)})
    e = correlation_edges(rets, threshold=0.7)
    assert e[["a", "b"]].values.tolist() == [["A", "B"]]


def test_market_metrics_spy_beta_is_one(market_dir):
    m = pd.read_parquet(market_dir / "market_metrics.parquet", engine="fastparquet")
    assert m.loc[m["ticker"] == "SPY", "beta_spy"].item() == 1.0
    assert m["risk_class"].notna().all()
    assert (m["end"] <= TODAY.isoformat()).all()


# ---------- refresh ----------

def test_close_refresh_drops_future_bars_and_reports_missing(market_dir):
    man = json.loads((market_dir / "manifest_close.json").read_text())
    assert man["as_of"] == TODAY.isoformat()
    assert man["missing"] == ["ZTS"]
    assert man["tickers_with_metrics"] == man["tickers_with_prices"] == man["tickers_requested"] - 1
    px = pd.read_parquet(market_dir / "market_prices.parquet", engine="fastparquet")
    assert px["date"].max() == pd.Timestamp(TODAY)
    assert man["correlation_edges"] > 0 and "Yahoo" in man["source"]


def test_midday_refresh_flags_stale_quotes(tmp_path):
    man = refresh.run_midday(CURATED, tmp_path, downloader=fake_downloader, today=date(2026, 11, 16))
    q = pd.read_parquet(tmp_path / "market_intraday.parquet", engine="fastparquet")
    fresh = q[~q["stale"]]
    assert man["fresh_quotes"] == len(fresh) > 0
    assert fresh["intraday_change"].notna().all()
    # on a day without a bar (Sunday), every quote is stale and has no intraday change
    refresh.run_midday(CURATED, tmp_path, downloader=fake_downloader, today=date(2026, 11, 15))
    q = pd.read_parquet(tmp_path / "market_intraday.parquet", engine="fastparquet")
    assert q["stale"].all() and q["intraday_change"].isna().all()


def test_refresh_handler_publishes_then_invokes_the_loader(monkeypatch, tmp_path):
    calls = {}

    class FakeLambda:
        def invoke(self, **kw):
            calls["invoke"] = kw

    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: FakeLambda())
    monkeypatch.setattr(refresh, "RUNS", {"close": lambda c, o: {"tickers_with_prices": 3}})
    monkeypatch.setattr(refresh, "upload_dir", lambda d, uri: calls.setdefault("upload", uri) and ["k"])
    monkeypatch.setenv("AF_MARKET_S3", "s3://b/market/")
    monkeypatch.setenv("AF_GRAPH_LOADER", "loader-fn")
    monkeypatch.delenv("AF_CURATED_S3", raising=False)
    out = refresh.handler({"mode": "close"})
    assert calls["upload"] == "s3://b/market/"
    assert calls["invoke"]["FunctionName"] == "loader-fn" and calls["invoke"]["InvocationType"] == "Event"
    assert json.loads(calls["invoke"]["Payload"]) == {"mode": "close"}
    assert out["graph_loader"] == "loader-fn"
    assert "error" in refresh.handler({"mode": "hourly"})


def test_close_without_any_price_publishes_nothing(monkeypatch):
    monkeypatch.setattr(refresh, "RUNS", {"close": lambda c, o: {"tickers_with_prices": 0}})
    monkeypatch.setattr(refresh, "upload_dir", lambda *a: pytest.fail("must not publish"))
    monkeypatch.setenv("AF_MARKET_S3", "s3://b/market/")
    monkeypatch.delenv("AF_CURATED_S3", raising=False)
    with pytest.raises(RuntimeError):
        refresh.handler({"mode": "close"})


# ---------- graph + tools ----------

def test_spec_links_funds_esg_controversies_and_correlations(graph):
    s = graph.spec
    assert len(s["assets"]) >= 520 and s["as_of"] == TODAY.isoformat()
    assert s["loaded_at"]           # the freshness stamp (checked precisely below)
    e = s["edges"]
    assert sum(1 for x in e["HOLDS"] if x["src"] == "SPY") >= 490
    assert {"low", "moderate", "high", "very_high"} >= set(s["risk_classes"])
    assert e["MEMBER_OF"] and e["EXCLUDED_FROM"] and e["CORRELATED_WITH"]
    assert any(x["src"] == "CNQ.TO" for x in e["HAS_CONTROVERSY"])
    statuses = {a["ticker"]: a["esg_status"] for a in s["assets"]}
    assert statuses["XOM"] == "excluded" and statuses["NVDA"] == "in_index"


def test_screen_filters_sorts_and_limits(graph):
    out = mg.screen_tool(graph, {"sector": "Utilities", "sort_by": "ann_vol", "limit": 5})
    rows = out["output"]["rows"]
    assert out["evidence_id"].startswith("ev-") and 0 < len(rows) <= 5
    assert all(r["sector"] == "Utilities" for r in rows)
    assert [r["ann_vol"] for r in rows] == sorted(r["ann_vol"] for r in rows)
    desc = mg.screen_tool(graph, {"in_sp500": True, "sort_by": "sp500_weight", "descending": True, "limit": 3})
    assert desc["output"]["rows"][0]["ticker"] in {"NVDA", "AAPL", "MSFT"}
    esg = mg.screen_tool(graph, {"esg": "in_index", "exclude_controversies": True, "max_vol": 0.3, "limit": 100})
    assert all(r["esg_status"] == "in_index" and r["n_controversies"] == 0 and r["ann_vol"] <= 0.3
               for r in esg["output"]["rows"])


def test_related_returns_funds_peers_and_esg(graph):
    out = mg.related_tool(graph, {"ticker": "xom"})["output"]
    assert {h["fund"] for h in out["held_by"]} >= {"SPY"}
    assert out["esg"] == [{"esg_index": "FTSE US All Cap Choice (Vanguard ESGV)", "member": False}]
    assert all(p["ticker"] != "XOM" for p in out["lower_vol_sector_peers"])
    spy = mg.related_tool(graph, {"ticker": "SPY"})["output"]
    assert len(spy["holdings"]) == 10 and spy["holdings"][0]["weight"] >= spy["holdings"][-1]["weight"]


def test_spec_stamps_loaded_at_from_the_manifest(graph, market_dir):
    man = json.loads((market_dir / "manifest_close.json").read_text())
    assert graph.spec["loaded_at"] == man["generated_at"]


def test_fresh_graph_serves_a_run_dated_before_the_snapshot(graph):
    """The look-ahead refusal is gone: freshness (not as_of) decides; a fresh graph always answers."""
    out = mg.screen_tool(graph, {"limit": 3, "as_of": "2026-01-02"})
    assert "evidence_id" in out and len(out["output"]["rows"]) <= 3
    assert out["freshness"]["fresh"] is True and out["freshness"]["window_hours"] == 12
    assert out["freshness"]["age_hours"] is not None and out["freshness"]["age_hours"] < 12


def test_stale_graph_refuses(graph):
    """Older than the reliability window = the refresh chain did not run: refuse, never serve."""
    stale = mg.LocalGraph({**graph.spec, "loaded_at": "2026-01-01T00:00:00+00:00"})   # own spec: no fixture mutation
    assert "stale" in mg.screen_tool(stale, {"limit": 3})["error"]
    assert "stale" in mg.related_tool(stale, {"ticker": "NVDA"})["error"]
    assert "stale" in mg.esg_screen_tool(stale, {"tickers": ["NVDA"]})["error"]


def test_graph_without_market_tables_serves_with_an_unknown_age():
    g = mg.LocalGraph.from_dirs(CURATED, None)                 # curated fallback: no load time at all
    out = mg.screen_tool(g, {"limit": 5})
    assert out["freshness"]["fresh"] is True and out["freshness"]["age_hours"] is None


def test_bad_arguments_still_return_an_error(graph):
    assert "sort_by" in mg.screen_tool(graph, {"sort_by": "DROP"})["error"]
    assert "esg" in mg.screen_tool(graph, {"esg": "great"})["error"]
    assert "Unknown ticker" in mg.related_tool(graph, {"ticker": "NOPE"})["error"]


def test_esg_screen_tool_from_the_graph(graph):
    out = mg.esg_screen_tool(graph, {"tickers": ["xom", "CNQ.TO"], "as_of": TODAY.isoformat()})
    assert out["tool"] == "esg_screen" and out["freshness"]["fresh"] is True
    assert out["output"]["membership"]["XOM"]["in_esg_index"] is False
    early = mg.esg_screen_tool(graph, {"tickers": ["CNQ.TO"], "as_of": "2000-01-01"})["output"]
    full = mg.esg_screen_tool(graph, {"tickers": ["CNQ.TO"], "as_of": "2026-12-31"})["output"]
    assert early["controversies"] == []                        # point-in-time filter on the graph rows
    assert full["controversies"] and len(full["controversies"]) >= len(early["controversies"])
    assert all(set(c) == {"ticker", "severity", "status", "category", "description", "source_doc"}
               for c in full["controversies"])


def test_esg_screen_tool_needs_tickers(graph):
    assert "tickers is required" in mg.esg_screen_tool(graph, {})["error"]


# ---------- local market cache wiring ----------

def test_agent_market_tools_serve_from_the_local_cache(graph, monkeypatch):
    """data/market populated: _market_graph serves screens in-process (no gateway, no cold start)."""
    from app.tools import agent_tools as at
    from app.tools import market_store

    monkeypatch.setattr(market_store, "local_market_graph", lambda c, m, require_tables=False: graph)
    monkeypatch.setattr(at, "_graph", None)
    out = at.market_screen(sector="Utilities", sort_by="ann_vol", limit=3)
    assert out["tool"] == "market_screen" and len(out["output"]["rows"]) <= 3
    assert out["freshness"]["fresh"] is True


def test_esg_screen_serves_from_the_local_cache_and_falls_back(graph, monkeypatch):
    """Fresh local cache: the graph envelope (freshness present). Stale or absent cache: the
    curated envelope (identical membership, no freshness key)."""
    from app.tools import analysis_tools as at
    from app.tools import market_store
    from app.market import graph as _mg

    monkeypatch.setattr(market_store, "local_market_graph", lambda c, m, require_tables=False: graph)
    ev = at.esg_screen(["XOM", "CNQ.TO"], as_of=TODAY.isoformat())
    assert ev["tool"] == "esg_screen" and ev["freshness"]["fresh"] is True
    assert ev["output"]["membership"]["XOM"]["in_esg_index"] is False

    stale = _mg.LocalGraph({**graph.spec, "loaded_at": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setattr(market_store, "local_market_graph", lambda c, m, require_tables=False: stale)
    cur = at.esg_screen(["XOM"], as_of=TODAY.isoformat())
    assert "freshness" not in cur and "error" not in cur      # curated fallback answered
    assert cur["output"]["membership"]["XOM"]["in_esg_index"] is False


def test_fallback_without_market_tables_uses_curated_metrics():
    g = mg.LocalGraph.from_dirs(CURATED, None)
    assert "fallback" in g.spec["source"]
    rows = mg.screen_tool(g, {"limit": 100})["output"]["rows"]
    assert 0 < len(rows) <= 31  # only the curated assets have metrics


def test_gateway_exposes_the_market_target(monkeypatch, graph):
    monkeypatch.setattr(qg, "_graph", graph)
    out = qg.call("market___screen", {"risk_class": "low", "limit": "3"})
    assert out["tool"] == "market_screen" and len(out["output"]["rows"]) <= 3
    assert qg.call("market___related", {"ticker": "NVDA"})["tool"] == "market_related"
    assert [t["name"] for t in qg.TARGETS["market"]] == ["screen", "related"]


def test_gateway_exposes_the_esg_target(monkeypatch, graph):
    monkeypatch.setattr(qg, "_graph", graph)
    out = qg.call("esg___esg_screen", {"tickers": ["XOM", "CNQ.TO"], "as_of": TODAY.isoformat()})
    assert out["tool"] == "esg_screen" and "error" not in out
    assert out["output"]["membership"]["XOM"]["in_esg_index"] is False
    assert [t["name"] for t in qg.TARGETS["esg"]] == ["esg_screen"]
    assert qg.call("esg___esg_screen", {"tickers": []})["error"].startswith("tickers is required")


# ---------- Neptune (recorded queries, no network) ----------

class RecordingNeptune(NeptuneGraph):
    def __init__(self, results=None):
        super().__init__("graph.example", "us-east-1")
        self.calls, self.results = [], results or {}

    def query(self, cypher, params=None):
        self.calls.append((cypher, params or {}))
        for key, value in self.results.items():
            if key in cypher:
                return value
        return []


def test_neptune_screen_query_is_parameterized():
    f = mg.normalize_filters({"sector": "Energy') DETACH DELETE (n", "max_vol": 0.2, "sort_by": "beta_spy",
                              "descending": True, "limit": 500})
    q, p = NeptuneGraph.screen_query(f)
    assert "DETACH" not in q and p["sector"].startswith("Energy")
    assert "ORDER BY a.beta_spy DESC" in q and p["limit"] == mg.MAX_LIMIT and p["max_vol"] == 0.2


def test_neptune_load_stamps_then_prunes_older_loads(graph):
    n = RecordingNeptune()
    stats = n.load(graph.spec)
    text = [c for c, _ in n.calls]
    assert stats["assets"] == len(graph.spec["assets"])
    assert any("MERGE (a:Asset {ticker: r.ticker})" in c for c in text)
    assert any("CREATE (f)-[:HOLDS" in c for c in text)
    prune = [i for i, c in enumerate(text) if "e.load_id <> $load_id DELETE e" in c]
    creates = [i for i, c in enumerate(text) if "CREATE (" in c]
    assert prune and max(creates) < prune[0]  # old edges are removed only after the new ones exist
    assert all(len(p.get("rows", [])) <= 250 for _, p in n.calls)
    assert any("Snapshot" in c for c in text)


def test_neptune_reads_map_to_the_tool_shape():
    n = RecordingNeptune({"s.as_of": [{"as_of": "2026-11-13"}],
                          "RETURN a.ticker AS ticker": [{"ticker": "FTS.TO", "ann_vol": 0.14, "sector": "Utilities"}]})
    out = mg.screen_tool(n, {"sector": "Utilities"})
    assert out["as_of"] == "2026-11-13" and out["output"]["rows"][0]["ticker"] == "FTS.TO"
    empty = RecordingNeptune({"s.as_of": [{"as_of": "2026-11-13"}]})
    assert "Unknown ticker" in mg.related_tool(empty, {"ticker": "ZZZ"})["error"]  # no asset row


def test_neptune_fresh_snapshot_carries_the_window(monkeypatch):
    now = datetime.now(tz=timezone.utc)
    n = RecordingNeptune({
        # key order matters: the stub matches the FIRST key contained in the query
        "s.loaded_at": [{"as_of": "2026-11-13", "loaded_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                         "intraday_at": None}],
        "MEMBER_OF|EXCLUDED_FROM": [{"ticker": "XOM", "member": False, "esg_index": "ESGV", "status": "PASS"}],
        "HAS_CONTROVERSY": [{"ticker": "XOM", "severity": "major", "status": "verified", "category": "environment",
                             "description": "d", "source_doc": "10-K"}],
        "RETURN a.ticker AS ticker": [{"ticker": "FTS.TO", "ann_vol": 0.14, "sector": "Utilities"}],
    })
    out = mg.screen_tool(n, {"sector": "Utilities"})
    assert out["freshness"]["fresh"] is True and out["freshness"]["age_hours"] <= 3
    esg = mg.esg_screen_tool(n, {"tickers": ["XOM"], "as_of": "2026-11-13"})
    assert esg["tool"] == "esg_screen" and esg["output"]["membership"]["XOM"]["in_esg_index"] is False
    assert esg["output"]["controversies"][0]["status"] == "verified"


def test_neptune_stale_snapshot_refuses_every_graph_tool():
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    n = RecordingNeptune({"s.loaded_at": [{"as_of": "2026-11-13", "loaded_at": old, "intraday_at": None}]})
    assert "stale" in mg.screen_tool(n, {"limit": 3})["error"]
    assert "stale" in mg.related_tool(n, {"ticker": "NVDA"})["error"]
    assert "stale" in mg.esg_screen_tool(n, {"tickers": ["NVDA"]})["error"]


def test_backend_switches_to_neptune_with_the_endpoint(monkeypatch):
    monkeypatch.setenv("AF_NEPTUNE_ENDPOINT", "graph.example")
    assert isinstance(mg.backend(CURATED), NeptuneGraph)
    monkeypatch.delenv("AF_NEPTUNE_ENDPOINT")
    assert isinstance(mg.backend(CURATED, "does-not-exist"), mg.LocalGraph)


def test_build_curated_uses_the_shared_metrics():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("build_curated", Path("scripts/build_curated.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from app.market import metrics

    assert mod.asset_metrics is metrics.asset_metrics


def test_neptune_retries_concurrent_modifications(monkeypatch):
    n = NeptuneGraph("graph.example", "us-east-1")
    calls = []

    def post(cypher, params=None):
        calls.append(cypher)
        if len(calls) < 3:
            raise OSError('Neptune HTTP 500: {"code":"ConcurrentModificationException"}')
        return [{"ok": 1}]

    monkeypatch.setattr(n, "_post", post)
    monkeypatch.setattr("app.market.neptune.time.sleep", lambda s: None)
    assert n.query("RETURN 1") == [{"ok": 1}] and len(calls) == 3
    monkeypatch.setattr(n, "_post", lambda c, p=None: (_ for _ in ()).throw(OSError("Neptune HTTP 400: bad")))
    with pytest.raises(OSError):
        n.query("RETURN 1")


def test_refresh_can_skip_the_async_load(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("loader must not be invoked"))
    monkeypatch.setattr(refresh, "RUNS", {"close": lambda c, o: {"tickers_with_prices": 3}})
    monkeypatch.setattr(refresh, "upload_dir", lambda d, uri: ["k"])
    monkeypatch.setenv("AF_MARKET_S3", "s3://b/market/")
    monkeypatch.setenv("AF_GRAPH_LOADER", "loader-fn")
    monkeypatch.delenv("AF_CURATED_S3", raising=False)
    assert "graph_loader" not in refresh.handler({"mode": "close", "load": False})
