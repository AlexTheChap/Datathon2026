"""Amazon Neptune backend of the market graph + the graph-loader Lambda.

Neptune Serverless (private VPC, IAM auth, encrypted; scripts/deploy_graph.py). Queries are openCypher over
HTTPS, signed with SigV4 (service "neptune-db") using botocore only: no extra driver in the Lambda zip.

Every query is a FIXED openCypher text with parameters; the only interpolated pieces (sort column, filter
clauses) come from whitelists in app.market.graph, never from the agent's raw text.

Loading (handler, mode "close"): upsert every node, stamp the edges of this load with `load_id`, then delete
the edges and assets of older loads. Readers never see a half-empty graph (no delete-then-recreate gap).
Mode "midday": only the intraday quote properties of the Asset nodes change.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.market.graph import EDGE_TYPES, REPO_ROOT, SCREEN_COLUMNS, build_spec

BATCH = 250


class NeptuneGraph:
    def __init__(self, endpoint: str, region: str, port: int = 8182, session=None, timeout: int = 60):
        self.url = f"https://{endpoint}:{port}/openCypher"
        self.region = region
        self.timeout = timeout
        self._session = session

    @classmethod
    def from_env(cls) -> "NeptuneGraph":
        return cls(os.environ["AF_NEPTUNE_ENDPOINT"],
                   os.environ.get("AWS_REGION") or os.environ.get("AF_AWS_REGION") or "us-east-1",
                   int(os.environ.get("AF_NEPTUNE_PORT", "8182")))

    # ---------- transport ----------

    def _credentials(self):
        if self._session is None:
            import boto3

            self._session = boto3.Session()
        return self._session.get_credentials().get_frozen_credentials()

    def _post(self, cypher: str, params: dict | None = None) -> list[dict]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        body = urllib.parse.urlencode({"query": cypher, "parameters": json.dumps(params or {}, default=str)})
        req = AWSRequest(method="POST", url=self.url, data=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        SigV4Auth(self._credentials(), "neptune-db", self.region).add_auth(req)
        http = urllib.request.Request(self.url, data=body.encode(), headers=dict(req.headers.items()), method="POST")
        try:
            with urllib.request.urlopen(http, timeout=self.timeout) as r:
                return json.loads(r.read())["results"]
        except urllib.error.HTTPError as e:
            raise OSError(f"Neptune HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from e

    def query(self, cypher: str, params: dict | None = None, retries: int = 6) -> list[dict]:
        """One openCypher call; conflicting concurrent writes (ConcurrentModificationException) are retried
        with exponential backoff, as Neptune recommends."""
        for attempt in range(retries):
            try:
                return self._post(cypher, params)
            except OSError as e:
                if "ConcurrentModificationException" not in str(e) or attempt == retries - 1:
                    raise
                time.sleep(min(0.2 * 2 ** attempt, 5))
        return []

    def _batched(self, cypher: str, rows: list[dict], **params) -> int:
        for i in range(0, len(rows), BATCH):
            self.query(cypher, {"rows": rows[i:i + BATCH], **params})
        return len(rows)

    # ---------- load ----------

    def load(self, spec: dict) -> dict:
        load_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        t0 = time.time()
        stats: dict[str, Any] = {"load_id": load_id, "as_of": spec["as_of"]}
        assets = [{"ticker": a["ticker"], "props": {k: v for k, v in a.items() if v is not None}}
                  for a in spec["assets"]]
        stats["assets"] = self._batched(
            "UNWIND $rows AS r MERGE (a:Asset {ticker: r.ticker}) SET a += r.props, a.load_id = $load_id",
            assets, load_id=load_id)
        for label, names in (("Sector", spec["sectors"]), ("Country", spec["countries"]),
                             ("RiskClass", spec["risk_classes"]), ("ESGIndex", spec["esg_indexes"])):
            self._batched(f"UNWIND $rows AS r MERGE (:{label} {{name: r.name}})", [{"name": n} for n in names])
        self._batched("UNWIND $rows AS r MERGE (c:Controversy {id: r.id}) SET c += r",
                      spec["controversies"])

        cat = [{"ticker": a["ticker"], "sector": a.get("sector"), "country": a.get("listing_country"),
                "risk_class": a.get("risk_class")} for a in spec["assets"]]
        for rel, label, key in (("IN_SECTOR", "Sector", "sector"), ("LISTED_IN", "Country", "country"),
                                ("IN_RISK_CLASS", "RiskClass", "risk_class")):
            rows = [r for r in cat if r[key]]
            self._batched(f"UNWIND $rows AS r MATCH (a:Asset {{ticker: r.ticker}}) MATCH (n:{label} {{name: r.{key}}}) "
                          f"MERGE (a)-[e:{rel}]->(n) SET e.load_id = $load_id", rows, load_id=load_id)

        e = spec["edges"]
        self._batched("UNWIND $rows AS r MATCH (f:Asset {ticker: r.src}) MATCH (h:Asset {ticker: r.dst}) "
                      "CREATE (f)-[:HOLDS {weight: r.weight, load_id: $load_id}]->(h)", e["HOLDS"], load_id=load_id)
        for rel in ("MEMBER_OF", "EXCLUDED_FROM"):
            self._batched(f"UNWIND $rows AS r MATCH (a:Asset {{ticker: r.src}}) MATCH (i:ESGIndex {{name: r.dst}}) "
                          f"CREATE (a)-[:{rel} {{status: r.status, load_id: $load_id}}]->(i)", e[rel], load_id=load_id)
        self._batched("UNWIND $rows AS r MATCH (a:Asset {ticker: r.src}) MATCH (c:Controversy {id: r.dst}) "
                      "CREATE (a)-[:HAS_CONTROVERSY {load_id: $load_id}]->(c)", e["HAS_CONTROVERSY"], load_id=load_id)
        self._batched("UNWIND $rows AS r MATCH (a:Asset {ticker: r.src}) MATCH (b:Asset {ticker: r.dst}) "
                      "CREATE (a)-[:CORRELATED_WITH {rho: r.rho, load_id: $load_id}]->(b)",
                      e["CORRELATED_WITH"], load_id=load_id)

        # drop what the previous loads left behind (edges first, then assets that left the universe)
        rels = "|".join(EDGE_TYPES + ["IN_SECTOR", "LISTED_IN", "IN_RISK_CLASS"])
        self.query(f"MATCH ()-[e:{rels}]->() WHERE e.load_id <> $load_id DELETE e", {"load_id": load_id})
        self.query("MATCH (a:Asset) WHERE a.load_id <> $load_id DETACH DELETE a", {"load_id": load_id})
        self.query("MERGE (s:Snapshot {id: 'market'}) SET s.as_of = $as_of, s.load_id = $load_id, "
                   "s.loaded_at = $now, s.source = $source, s.n_assets = $n",
                   {"as_of": spec["as_of"], "load_id": load_id, "source": spec.get("source"),
                    "now": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": len(assets)})
        stats["edges"] = {k: len(v) for k, v in e.items()}
        stats["seconds"] = round(time.time() - t0, 1)
        return stats

    def update_intraday(self, rows: list[dict]) -> int:
        clean = [{"ticker": r["ticker"], "props": {k: r.get(k) for k in
                                                   ("last_price", "intraday_change", "quote_date", "stale")}}
                 for r in rows]
        n = self._batched("UNWIND $rows AS r MATCH (a:Asset {ticker: r.ticker}) SET a += r.props", clean)
        self.query("MERGE (s:Snapshot {id: 'market'}) SET s.intraday_at = $now",
                   {"now": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return n

    # ---------- reads (same answers as LocalGraph) ----------

    def snapshot_as_of(self) -> str | None:
        r = self.query("MATCH (s:Snapshot {id: 'market'}) RETURN s.as_of AS as_of")
        return r[0]["as_of"] if r else None

    def snapshot_freshness(self) -> dict:
        """When the graph was last written: the close load (loaded_at) and the midday quotes (intraday_at)."""
        r = self.query("MATCH (s:Snapshot {id: 'market'}) RETURN s.as_of AS as_of, s.loaded_at AS loaded_at, "
                       "s.intraday_at AS intraday_at")
        return r[0] if r else {}

    def esg_screen(self, tickers: list[str], as_of: str) -> tuple[list[dict], list[dict]]:
        """(membership rows, controversy rows) — same shape as LocalGraph.esg_screen."""
        tset = sorted(set(tickers))
        membership = self.query("MATCH (a:Asset)-[e:MEMBER_OF|EXCLUDED_FROM]->(i:ESGIndex) WHERE a.ticker IN $ts "
                                "RETURN a.ticker AS ticker, type(e) = 'MEMBER_OF' AS member, i.name AS esg_index, "
                                "e.status AS status ORDER BY ticker, esg_index", {"ts": tset})
        cont = self.query("MATCH (a:Asset)-[:HAS_CONTROVERSY]->(c:Controversy) WHERE a.ticker IN $ts "
                          "AND c.publication_date <= $as_of "
                          "RETURN a.ticker AS ticker, c.severity AS severity, c.status AS status, "
                          "c.category AS category, c.description AS description, c.source_doc AS source_doc "
                          "ORDER BY a.ticker, c.publication_date", {"ts": tset, "as_of": as_of})
        controversies = [{k: (str(r.get(k))[:220] if k == "description" and r.get(k) else r.get(k))
                          for k in ("ticker", "severity", "status", "category", "description", "source_doc")}
                         for r in cont]
        return membership, controversies

    def count(self) -> dict:
        r = self.query("MATCH (a:Asset) RETURN count(a) AS assets")
        e = self.query("MATCH ()-[e]->() RETURN type(e) AS type, count(e) AS n")
        return {"assets": r[0]["assets"] if r else 0, "edges": {x["type"]: x["n"] for x in e}}

    @staticmethod
    def screen_query(f: dict) -> tuple[str, dict]:
        where, p = [], {}
        for k, prop in (("sector", "sector"), ("country", "listing_country"), ("risk_class", "risk_class"),
                        ("asset_class", "asset_class"), ("esg", "esg_status"), ("in_sp500", "in_sp500")):
            if k in f:
                where.append(f"a.{prop} = ${k}")
                p[k] = f[k]
        if f.get("exclude_controversies"):
            where.append("a.n_controversies = 0")
        if "min_vol" in f:
            where.append("a.ann_vol >= $min_vol")
            p["min_vol"] = f["min_vol"]
        if "max_vol" in f:
            where.append("a.ann_vol <= $max_vol")
            p["max_vol"] = f["max_vol"]
        if "max_drawdown_limit" in f:
            where.append("a.max_drawdown >= $dd_floor")
            p["dd_floor"] = -abs(f["max_drawdown_limit"])
        sort = f["sort_by"]                       # whitelisted by normalize_filters (SORTABLE)
        where.append(f"a.{sort} IS NOT NULL")
        cols = ", ".join(f"a.{c} AS {c}" for c in SCREEN_COLUMNS)
        q = (f"MATCH (a:Asset) WHERE {' AND '.join(where)} RETURN {cols} "
             f"ORDER BY a.{sort} {'DESC' if f['descending'] else 'ASC'}, a.ticker ASC LIMIT $limit")
        p["limit"] = f["limit"]
        return q, p

    def screen(self, f: dict) -> list[dict]:
        q, p = self.screen_query(f)
        return [{c: r.get(c) for c in SCREEN_COLUMNS} for r in self.query(q, p)]

    def related(self, ticker: str, limit: int = 10) -> dict | None:
        cols = ", ".join(f"a.{c} AS {c}" for c in SCREEN_COLUMNS)
        a = self.query(f"MATCH (a:Asset {{ticker: $t}}) RETURN {cols}", {"t": ticker})
        if not a:
            return None
        asset = {c: a[0].get(c) for c in SCREEN_COLUMNS}
        held_by = self.query("MATCH (f:Asset)-[h:HOLDS]->(:Asset {ticker: $t}) "
                             "RETURN f.ticker AS fund, h.weight AS weight ORDER BY weight DESC, fund ASC", {"t": ticker})
        holdings = self.query("MATCH (:Asset {ticker: $t})-[h:HOLDS]->(x:Asset) RETURN x.ticker AS ticker, "
                              "h.weight AS weight ORDER BY weight DESC, ticker ASC LIMIT $n", {"t": ticker, "n": limit})
        corr = self.query("MATCH (:Asset {ticker: $t})-[c:CORRELATED_WITH]-(x:Asset) RETURN x.ticker AS ticker, "
                          "c.rho AS rho", {"t": ticker})
        corr = sorted(corr, key=lambda x: (-abs(x["rho"]), x["ticker"]))[:limit]
        peers = self.query("MATCH (a:Asset {ticker: $t})-[:IN_SECTOR]->(s:Sector)<-[:IN_SECTOR]-(p:Asset) "
                           "WHERE p.ticker <> $t AND p.ann_vol IS NOT NULL RETURN p.ticker AS ticker, "
                           "p.ann_vol AS ann_vol, p.risk_class AS risk_class ORDER BY ann_vol ASC, ticker ASC LIMIT 5",
                           {"t": ticker})
        esg = self.query("MATCH (:Asset {ticker: $t})-[e:MEMBER_OF|EXCLUDED_FROM]->(i:ESGIndex) "
                         "RETURN i.name AS esg_index, type(e) = 'MEMBER_OF' AS member ORDER BY esg_index", {"t": ticker})
        cont = self.query("MATCH (:Asset {ticker: $t})-[:HAS_CONTROVERSY]->(c:Controversy) RETURN "
                          "c.publication_date AS publication_date, c.category AS category, c.severity AS severity, "
                          "c.status AS status, c.description AS description ORDER BY publication_date", {"t": ticker})
        return {"asset": asset, "held_by": held_by, "holdings": holdings, "correlated": corr,
                "lower_vol_sector_peers": peers, "esg": esg, "controversies": cont}


# ---------- graph-loader Lambda (inside the VPC; invoked by the market refresh) ----------

def _sync_market(uri: str) -> Path:
    """Fresh copy of the market tables (a same-size file may have new content: never reuse the cache)."""
    from app.tools.curated_store import sync_from_s3

    d = Path("/tmp/market")
    shutil.rmtree(d, ignore_errors=True)
    sync_from_s3(uri, d)
    return d


def handler(event: dict | None, context: Any = None) -> dict:
    """{"mode": "close"} full upsert · {"mode": "midday"} quotes only · {"mode": "selftest"} sample reads."""
    import pandas as pd

    from app.market.graph import _clean, normalize_filters
    from app.tools.curated_store import ENGINE, curated_dir_from_env

    mode = (event or {}).get("mode", "close")
    g = NeptuneGraph.from_env()
    if mode == "selftest":
        f = normalize_filters({"sector": "Utilities", "sort_by": "ann_vol", "limit": 5})
        return {"snapshot": g.snapshot_as_of(), "count": g.count(), "screen_filters": f, "screen": g.screen(f),
                "related": g.related((event or {}).get("ticker", "NVDA"))}
    curated = curated_dir_from_env(REPO_ROOT / "data" / "curated")
    market = _sync_market(os.environ["AF_MARKET_S3"])
    if mode == "midday":
        p = market / "market_intraday.parquet"
        rows = pd.read_parquet(p, engine=ENGINE).to_dict("records") if p.exists() else []
        out = {"mode": mode, "updated": g.update_intraday([{k: _clean(v) for k, v in r.items()} for r in rows])}
    else:
        out = {"mode": mode, **g.load(build_spec(curated, market))}
    print(json.dumps(out, default=str))
    return out
