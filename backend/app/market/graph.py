"""Market graph: schema, builder and the two agent tools (market_screen, market_related).

Graph (Amazon Neptune in AWS, `LocalGraph` in memory for tests and offline runs: same data, same answers):

  (:Asset {ticker, name, asset_class, sector, listing_country, ann_vol, max_drawdown, var95_daily,
           beta_spy, total_return, risk_class, last_close, last_price, intraday_change, esg_status, …})
  (:Asset)-[:IN_SECTOR]->(:Sector)            (:Asset)-[:LISTED_IN]->(:Country)
  (:Asset)-[:IN_RISK_CLASS]->(:RiskClass)     (:Asset {fund})-[:HOLDS {weight}]->(:Asset)
  (:Asset)-[:MEMBER_OF | :EXCLUDED_FROM]->(:ESGIndex)
  (:Asset)-[:HAS_CONTROVERSY]->(:Controversy) (:Asset)-[:CORRELATED_WITH {rho}]->(:Asset)   |rho| >= 0.7
  (:Snapshot {id: "market", as_of})           date of the last close loaded

Built from data that already exists: the market tables of app.market.refresh (S&P 500 + curated universe,
Yahoo) and the curated reference files (ETF holdings, ESG index screen, controversies). Without market
tables (fresh clone, unit tests), metrics fall back to curated asset_risk_metrics (31 assets only).

Price series stay in Parquet (S3): the graph holds the latest metrics and the relationships.

Freshness: the graph is a LIVE snapshot, reliable within FRESH_WINDOW_HOURS (default 12) of its last
load (Snapshot.loaded_at / intraday_at; refreshes run 11:00 and 17:00 ET on business days). A stale
graph refuses to answer (it means the refresh chain did not run); historical runs keep using the
dated risk tools.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.market.metrics import risk_class
from app.market.universe import market_universe
from app.tools import curated_store as cs
from app.tools.risk_tools import _eid

REPO_ROOT = Path(__file__).resolve().parents[3]
ASSET_METRICS = ["ann_vol", "downside_vol", "max_drawdown", "var95_daily", "cvar95_daily", "beta_spy",
                 "total_return", "risk_class", "last_close", "metrics_as_of", "metrics_window"]
INTRADAY = ["last_price", "intraday_change", "quote_date", "stale"]
SORTABLE = {"ann_vol", "max_drawdown", "var95_daily", "beta_spy", "total_return", "sp500_weight", "last_price"}
ESG_FILTERS = {"in_index", "excluded", "not_screened"}
EDGE_TYPES = ["HOLDS", "MEMBER_OF", "EXCLUDED_FROM", "HAS_CONTROVERSY", "CORRELATED_WITH"]
MAX_LIMIT = 100


def _clean(v: Any) -> Any:
    """NaN / NaT / numpy scalars -> plain JSON values (None for missing)."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.date().isoformat()
    if hasattr(v, "item"):          # numpy scalar
        return _clean(v.item())
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() == "true"


def _read_market(market_dir: Path | None, name: str) -> pd.DataFrame | None:
    if market_dir is None:
        return None
    p = Path(market_dir) / f"{name}.parquet"
    return pd.read_parquet(p, engine=cs.ENGINE) if p.exists() else None


def _metrics(curated_dir: Path, market_dir: Path | None) -> pd.DataFrame:
    m = _read_market(market_dir, "market_metrics")
    if m is not None and not m.empty:
        m = m.assign(metrics_as_of=m["end"].astype(str), metrics_window=m["window"].astype(str))
        return m
    # fallback: curated metrics (31 assets, full window, CAD)
    if not cs.exists(curated_dir, "asset_risk_metrics"):
        return pd.DataFrame(columns=["ticker", *ASSET_METRICS])
    c = cs.read(curated_dir, "asset_risk_metrics")
    c = c.sort_values("n_obs").drop_duplicates("ticker", keep="last").copy()  # longest window per asset
    c["risk_class"] = c["ann_vol"].map(risk_class)
    c["last_close"] = None
    return c.assign(metrics_as_of=c["end"].astype(str), metrics_window=c["window"].astype(str) + " (curated, CAD)")


def _market_loaded_at(market_dir: str | Path | None) -> str | None:
    """When the market tables were last (re)written: the close manifest's generated_at, else the newest
    market_*.parquet mtime — the LocalGraph's substitute for the Neptune Snapshot.loaded_at stamp."""
    if market_dir is None:
        return None
    m = Path(market_dir) / "manifest_close.json"
    if m.exists():
        try:
            generated = json.loads(m.read_text(encoding="utf-8")).get("generated_at")
            if generated:
                return str(generated)
        except (OSError, ValueError):
            pass
    mtimes = [p.stat().st_mtime for p in Path(market_dir).glob("market_*.parquet")]
    if mtimes:
        return datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat(timespec="seconds")
    return None


def build_spec(curated_dir: str | Path, market_dir: str | Path | None = None) -> dict:
    """Everything the graph holds, as plain JSON-ready lists (loaded as-is into Neptune or LocalGraph)."""
    curated_dir = Path(curated_dir)
    market_dir = Path(market_dir) if market_dir else None
    uni = _read_market(market_dir, "market_universe")
    if uni is None or uni.empty:
        uni = market_universe(curated_dir)
    tickers = set(uni["ticker"])

    met = _metrics(curated_dir, market_dir)
    assets = uni.merge(met[["ticker", *[c for c in ASSET_METRICS if c in met]]], on="ticker", how="left")
    intra = _read_market(market_dir, "market_intraday")
    if intra is not None and not intra.empty:
        assets = assets.merge(intra[["ticker", *INTRADAY]], on="ticker", how="left")
    else:
        assets = assets.assign(last_price=assets.get("last_close"), intraday_change=None, quote_date=None, stale=None)

    # ESG index screen (SPY / XIU holdings vs the Vanguard ESGV FTSE index), only for graph assets
    esg_rows, member, excluded = [], [], []
    if cs.exists(curated_dir, "esg_index_screen"):
        s = cs.read(curated_dir, "esg_index_screen")
        s = s[s["ticker"].isin(tickers)].drop_duplicates(["ticker", "esg_index"])
        for r in s.itertuples():
            (member if _truthy(r.in_esg_fund) else excluded).append(
                {"src": r.ticker, "dst": r.esg_index, "status": r.status, "esg_fund": r.esg_fund})
        esg_rows = s
    esg_status = {e["src"]: "in_index" for e in member} | {e["src"]: "excluded" for e in excluded}
    assets["esg_status"] = assets["ticker"].map(esg_status).fillna("not_screened")
    esg_indexes = sorted({e["dst"] for e in member + excluded})

    # Controversies (filings / NBIM), with their review status
    controversies, has_c = [], []
    if cs.exists(curated_dir, "controversies"):
        c = cs.read(curated_dir, "controversies")
        c = c[c["ticker"].isin(tickers)]
        for r in c.itertuples():
            cid = f"{r.ticker}|{r.publication_date}|{r.category}"
            controversies.append({"id": cid, "ticker": r.ticker, "publication_date": r.publication_date,
                                  "category": r.category, "severity": r.severity, "status": r.status,
                                  "description": r.description[:300], "source_doc": r.source_doc})
            has_c.append({"src": r.ticker, "dst": cid})
    n_c = pd.Series([x["ticker"] for x in controversies], dtype=object).value_counts()
    verified = {x["ticker"] for x in controversies if x["status"] == "verified"}
    assets["n_controversies"] = assets["ticker"].map(n_c).fillna(0).astype(int)
    assets["has_verified_controversy"] = assets["ticker"].isin(verified)

    # ETF holdings between graph assets (SPY -> 500 stocks, QQQ, ESGV, XIU.TO -> TSX names in the universe)
    holds = []
    if cs.exists(curated_dir, "etf_holdings"):
        h = cs.read(curated_dir, "etf_holdings")
        h = h[h["fund"].isin(tickers) & h["ticker"].isin(tickers)]
        h = h.assign(weight=pd.to_numeric(h["weight"], errors="coerce")).dropna(subset=["weight"])
        holds = [{"src": r.fund, "dst": r.ticker, "weight": round(float(r.weight), 6)}
                 for r in h.drop_duplicates(["fund", "ticker"]).itertuples()]

    corr = _read_market(market_dir, "market_correlation")
    correlated = [] if corr is None else [{"src": r.a, "dst": r.b, "rho": float(r.rho)}
                                          for r in corr.itertuples() if r.a in tickers and r.b in tickers]

    as_of = None
    if "metrics_as_of" in assets and assets["metrics_as_of"].notna().any():
        as_of = str(assets["metrics_as_of"].dropna().max())
    records = [{k: _clean(v) for k, v in row.items()} for row in assets.to_dict("records")]
    return {
        "as_of": as_of,
        "loaded_at": _market_loaded_at(market_dir),
        "source": "market tables (app.market.refresh, Yahoo Finance)" if _read_market(market_dir, "market_metrics")
                  is not None else "curated asset_risk_metrics (fallback, 31 assets)",
        "assets": records,
        "sectors": sorted({a["sector"] for a in records if a.get("sector")}),
        "countries": sorted({a["listing_country"] for a in records if a.get("listing_country")}),
        "risk_classes": sorted({a["risk_class"] for a in records if a.get("risk_class")}),
        "esg_indexes": esg_indexes,
        "controversies": controversies,
        "edges": {"HOLDS": holds, "MEMBER_OF": member, "EXCLUDED_FROM": excluded,
                  "HAS_CONTROVERSY": has_c, "CORRELATED_WITH": correlated},
        "esg_rows": len(esg_rows),
    }


# ---------- query semantics (LocalGraph implements them in pandas, NeptuneGraph in openCypher) ----------

SCREEN_COLUMNS = ["ticker", "name", "asset_class", "sector", "listing_country", "risk_class", "ann_vol",
                  "max_drawdown", "var95_daily", "beta_spy", "total_return", "sp500_weight", "last_price",
                  "intraday_change", "esg_status", "n_controversies", "has_verified_controversy"]


def normalize_filters(a: dict) -> dict:
    """Validate and normalise screen arguments (raises ValueError with a message the agent can act on)."""
    f: dict[str, Any] = {}
    for k in ("sector", "country", "risk_class", "asset_class"):
        if a.get(k) not in (None, ""):
            f[k] = str(a[k])
    if a.get("esg") not in (None, "", "any"):
        if a["esg"] not in ESG_FILTERS:
            raise ValueError(f"esg must be one of {sorted(ESG_FILTERS | {'any'})}")
        f["esg"] = a["esg"]
    for k in ("in_sp500", "exclude_controversies"):
        if a.get(k) not in (None, ""):
            f[k] = a[k] if isinstance(a[k], bool) else _truthy(a[k])
    for k in ("min_vol", "max_vol", "max_drawdown_limit"):
        if a.get(k) not in (None, ""):
            f[k] = float(a[k])
    sort_by = a.get("sort_by") or "ann_vol"
    if sort_by not in SORTABLE:
        raise ValueError(f"sort_by must be one of {sorted(SORTABLE)}")
    f["sort_by"] = sort_by
    f["descending"] = a["descending"] if isinstance(a.get("descending"), bool) else _truthy(a.get("descending"))
    f["limit"] = max(1, min(int(a.get("limit") or 20), MAX_LIMIT))
    return f


class LocalGraph:
    """In-memory graph with the same answers as Neptune (tests, offline runs, Lambda without Neptune)."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.assets = pd.DataFrame(spec["assets"])
        self.by_ticker = {a["ticker"]: a for a in spec["assets"]}
        self.edges = spec["edges"]
        self.controversies = {c["id"]: c for c in spec["controversies"]}

    @classmethod
    def from_dirs(cls, curated_dir: str | Path, market_dir: str | Path | None = None) -> "LocalGraph":
        return cls(build_spec(curated_dir, market_dir))

    def snapshot_as_of(self) -> str | None:
        return self.spec.get("as_of")

    def snapshot_freshness(self) -> dict:
        return {"as_of": self.spec.get("as_of"), "loaded_at": self.spec.get("loaded_at"), "intraday_at": None}

    def esg_screen(self, tickers: list[str], as_of: str) -> tuple[list[dict], list[dict]]:
        """(membership rows, controversy rows) in the same shape as NeptuneGraph.esg_screen."""
        tset = set(tickers)
        membership = []
        for rel, member in (("MEMBER_OF", True), ("EXCLUDED_FROM", False)):
            membership += [{"ticker": x["src"], "member": member, "esg_index": x["dst"], "status": x.get("status")}
                           for x in self.edges[rel] if x["src"] in tset]
        rows = sorted((self.controversies[x["dst"]] for x in self.edges["HAS_CONTROVERSY"]
                       if x["src"] in tset and self.controversies[x["dst"]].get("publication_date", "") <= as_of),
                      key=lambda c: c.get("publication_date", ""))
        controversies = [{k: (str(c[k])[:220] if k == "description" and c.get(k) else c.get(k))
                          for k in ("ticker", "severity", "status", "category", "description", "source_doc")}
                         for c in rows]
        return membership, controversies

    def screen(self, f: dict) -> list[dict]:
        d = self.assets
        for k, col in (("sector", "sector"), ("country", "listing_country"), ("risk_class", "risk_class"),
                       ("asset_class", "asset_class"), ("esg", "esg_status")):
            if k in f:
                d = d[d[col] == f[k]]
        if "in_sp500" in f:
            d = d[d["in_sp500"] == f["in_sp500"]]
        if f.get("exclude_controversies"):
            d = d[d["n_controversies"] == 0]
        if "min_vol" in f:
            d = d[d["ann_vol"] >= f["min_vol"]]
        if "max_vol" in f:
            d = d[d["ann_vol"] <= f["max_vol"]]
        if "max_drawdown_limit" in f:
            d = d[d["max_drawdown"] >= -abs(f["max_drawdown_limit"])]
        d = d[d[f["sort_by"]].notna()]
        d = d.sort_values([f["sort_by"], "ticker"], ascending=[not f["descending"], True]).head(f["limit"])
        return [{c: _clean(r.get(c)) for c in SCREEN_COLUMNS} for r in d.to_dict("records")]

    def related(self, ticker: str, limit: int = 10) -> dict | None:
        a = self.by_ticker.get(ticker)
        if a is None:
            return None
        e = self.edges
        held_by = sorted(({"fund": x["src"], "weight": x["weight"]} for x in e["HOLDS"] if x["dst"] == ticker),
                         key=lambda x: (-x["weight"], x["fund"]))
        holdings = sorted(({"ticker": x["dst"], "weight": x["weight"]} for x in e["HOLDS"] if x["src"] == ticker),
                          key=lambda x: (-x["weight"], x["ticker"]))[:limit]
        corr = [{"ticker": x["dst"] if x["src"] == ticker else x["src"], "rho": x["rho"]}
                for x in e["CORRELATED_WITH"] if ticker in (x["src"], x["dst"])]
        corr = sorted(corr, key=lambda x: (-abs(x["rho"]), x["ticker"]))[:limit]
        peers = self.assets[(self.assets["sector"] == a.get("sector")) & (self.assets["ticker"] != ticker)
                            & self.assets["ann_vol"].notna()]
        peers = peers.sort_values(["ann_vol", "ticker"]).head(5)
        esg = ([{"esg_index": x["dst"], "member": True} for x in e["MEMBER_OF"] if x["src"] == ticker]
               + [{"esg_index": x["dst"], "member": False} for x in e["EXCLUDED_FROM"] if x["src"] == ticker])
        cont = sorted((self.controversies[x["dst"]] for x in e["HAS_CONTROVERSY"] if x["src"] == ticker),
                      key=lambda c: c["publication_date"])
        return {"asset": {c: _clean(a.get(c)) for c in SCREEN_COLUMNS},
                "held_by": held_by, "holdings": holdings, "correlated": corr,
                "lower_vol_sector_peers": [{"ticker": p["ticker"], "ann_vol": _clean(p["ann_vol"]),
                                            "risk_class": _clean(p["risk_class"])} for p in peers.to_dict("records")],
                "esg": sorted(esg, key=lambda x: x["esg_index"]),
                "controversies": [{k: c[k] for k in ("publication_date", "category", "severity", "status",
                                                     "description")} for c in cont]}


# ---------- backend selection + tool wrappers (shared by the Gateway Lambda and the local Strands tools) ----------

def backend(curated_dir: str | Path, market_dir: str | Path | None = None):
    """AF_NEPTUNE_ENDPOINT set -> Neptune (IAM-signed openCypher); otherwise LocalGraph."""
    endpoint = os.environ.get("AF_NEPTUNE_ENDPOINT")
    if endpoint:
        from app.market.neptune import NeptuneGraph

        return NeptuneGraph.from_env()
    market_dir = market_dir or os.environ.get("AF_MARKET_DIR") or REPO_ROOT / "data" / "market"
    return LocalGraph.from_dirs(curated_dir, market_dir)


ASSUMPTIONS = [
    "Metrics: trailing 252 U.S. trading days of daily adjusted closes, in each asset's LISTING currency "
    "(USD for the S&P 500, CAD for TSX listings); beta against SPY.",
    "Risk class = annualized volatility band (team assumption): low < 15%, moderate 15-25%, high 25-40%, "
    "very_high >= 40%.",
    "listing_country is the country of the listing exchange, not the legal domicile.",
    "ESG status = membership of the reference ESG index (Vanguard ESGV / FTSE US All Cap Choice): a "
    "preference signal, the exclusion reason is not published; not_screened = no comparison available.",
    "Live snapshot from Yahoo Finance (unofficial, delayed), reliable within the freshness window of its "
    "last load; a stale graph refuses to answer rather than serve outdated data.",
]

FRESH_WINDOW_ENV = "AF_GRAPH_FRESH_HOURS"


def _fresh_window_hours() -> int:
    try:
        return max(1, int(os.environ.get(FRESH_WINDOW_ENV, "12")))
    except ValueError:
        return 12


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness(g) -> dict:
    """Age of the graph's last load against the reliability window (12h by default).

    The load times are the Neptune Snapshot.loaded_at (close) / intraday_at (midday) — the LocalGraph
    answers from the spec's loaded_at stamp. No load time at all (curated fallback) counts as fresh but
    reports an unknown age. Stale = the refresh chain did not run: refuse, never serve.
    """
    window = _fresh_window_hours()
    if hasattr(g, "snapshot_freshness"):
        snap = g.snapshot_freshness()
    else:                                   # a bare spec/duck-typed graph (tests)
        snap = {"as_of": g.snapshot_as_of()}
    stamps = [t for t in (_parse_ts(snap.get("loaded_at")), _parse_ts(snap.get("intraday_at"))) if t]
    age = (datetime.now(timezone.utc) - max(stamps)).total_seconds() / 3600 if stamps else None
    out = {"as_of": snap.get("as_of"), "loaded_at": snap.get("loaded_at"),
           "intraday_at": snap.get("intraday_at"),
           "age_hours": round(age, 1) if age is not None else None,
           "window_hours": window, "fresh": age is None or age <= window}
    if not out["fresh"]:
        raise LookupError(f"The market graph is stale: its last load is {age:.1f}h old, past the "
                          f"{window}h reliability window (refreshes run 11:00 and 17:00 ET on business "
                          "days). Use the point-in-time tools instead.")
    return out


def _envelope(tool: str, inputs: dict, output: Any, say: str, fresh: dict) -> dict:
    snap = fresh.get("as_of")
    return {"evidence_id": _eid(tool, {**inputs, "snapshot": snap, "loaded_at": fresh.get("loaded_at")}),
            "tool": tool, "inputs": inputs,
            "output": output, "say": say, "as_of": snap, "currency": "listing currency",
            "freshness": {k: fresh.get(k) for k in ("age_hours", "loaded_at", "window_hours", "fresh")},
            "method": "Graph query (Amazon Neptune openCypher, or the identical in-memory graph offline).",
            "assumptions": ASSUMPTIONS,
            "data_sources": ["Yahoo Finance daily bars via yfinance (market refresh 11:00 / 17:00 ET)",
                             "curated/etf_holdings.csv (SPY holdings = S&P 500 constituents)",
                             "curated/esg_index_screen.csv", "curated/controversies.csv"]}


def esg_screen_data(g, tickers: list[str], as_of: str | None) -> dict:
    """Membership + controversies for the tickers, in the esg_screen output shape, from the LIVE graph.

    Freshness-gated (a stale graph raises). Point-in-time is kept for controversies: only lines
    published on or before `as_of` come back. Membership is the current index screen (no `wording`:
    the graph stores the edge status, not the published wording). Backend parity: both LocalGraph and
    NeptuneGraph implement `.esg_screen(tickers, as_of)`.
    """
    fresh = freshness(g)
    m_rows, c_rows = g.esg_screen(tickers, as_of)
    membership: dict[str, dict] = {}
    for r in m_rows:
        membership[r["ticker"]] = {"esg_index": r["esg_index"], "in_esg_index": bool(r["member"]),
                                   "status": r.get("status"), "wording": None}
    controversies = c_rows
    unverified = sorted({c["ticker"] for c in controversies if c["status"] != "verified"}
                        - {c["ticker"] for c in controversies if c["status"] == "verified"})
    out = {"membership": membership, "controversies": controversies, "unverified_only": unverified}
    excluded = sorted(t for t, m in membership.items() if not m["in_esg_index"])
    say = (f"Outside reference ESG index: {', '.join(excluded) if excluded else 'none'}. "
           f"Controversy lines: {len(controversies)}"
           + (f" (unverified-only for: {', '.join(unverified)})" if unverified else "") + ".")
    return out, say, fresh


ESG_ASSUMPTIONS = [
    "Live market graph, reliable within its freshness window of the last load; a stale graph refuses.",
    "Controversies are limited to those published on or before the given as_of (point-in-time).",
    "Index membership does not explain WHY a stock is excluded (reason not published); it is a "
    "discussion signal, not a verdict.",
    "Controversy rows with status != 'verified' are unreviewed extracts: absence of evidence is not "
    "evidence of absence.",
]


def esg_screen_tool(g, args: dict | None) -> dict:
    """The Gateway's esg___esg_screen: the esg_screen evidence envelope from the LIVE graph
    (same shape as the local curated tool, so prompts and validators are unchanged)."""
    args = dict(args or {})
    tickers = sorted({str(t).strip().upper() for t in (args.get("tickers") or []) if str(t).strip()})
    if not tickers:
        return {"error": "tickers is required", "note": "Fix the arguments and call the tool again."}
    as_of = str(args.get("as_of") or datetime.now(timezone.utc).date().isoformat())
    try:
        out, say, fresh = esg_screen_data(g, tickers, as_of)
    except LookupError as e:
        return {"error": str(e)}
    except OSError as e:  # Neptune unreachable
        return {"error": f"market graph unavailable: {e}"}
    envelope = _envelope("esg_screen", {"tickers": tickers, "as_of": as_of}, out, say, fresh)
    envelope["assumptions"] = ESG_ASSUMPTIONS
    envelope["data_sources"] = ["market graph (MEMBER_OF / EXCLUDED_FROM / HAS_CONTROVERSY edges, "
                                "Neptune openCypher; loaded from curated/esg_index_screen.csv and "
                                "curated/controversies.csv)"]
    return envelope


def screen_tool(g, args: dict | None) -> dict:
    args = dict(args or {})
    try:
        f = normalize_filters(args)
        fresh = freshness(g)
        rows = g.screen(f)
    except (ValueError, LookupError) as e:
        return {"error": str(e), "note": "Fix the arguments and call the tool again."}
    except OSError as e:  # Neptune unreachable
        return {"error": f"market graph unavailable: {e}"}
    crit = ", ".join(f"{k}={v}" for k, v in f.items() if k not in ("sort_by", "descending", "limit")) or "none"
    head = ", ".join(f"{r['ticker']} ({r[f['sort_by']]:.1%})" if isinstance(r.get(f["sort_by"]), float)
                     and f["sort_by"] != "beta_spy" else r["ticker"] for r in rows[:5])
    say = (f"{len(rows)} asset(s) match [{crit}], sorted by {f['sort_by']} "
           f"{'descending' if f['descending'] else 'ascending'} (snapshot {fresh['as_of']}): {head or 'none'}.")
    return _envelope("market_screen", f, {"count": len(rows), "rows": rows}, say, fresh)


def related_tool(g, args: dict | None) -> dict:
    args = dict(args or {})
    ticker = str(args.get("ticker") or "").strip().upper()
    if not ticker:
        return {"error": "ticker is required", "note": "Fix the arguments and call the tool again."}
    try:
        fresh = freshness(g)
        out = g.related(ticker)
    except LookupError as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": f"market graph unavailable: {e}"}
    if out is None:
        return {"error": f"Unknown ticker {ticker!r} in the market graph (S&P 500 + curated universe).",
                "note": "Fix the arguments and call the tool again."}
    a = out["asset"]
    vol = f"{a['ann_vol']:.1%}" if isinstance(a.get("ann_vol"), float) else "n/a"
    say = (f"{ticker}: {a.get('sector')}, {a.get('risk_class') or 'unclassified'} risk (vol {vol}); held by "
           f"{', '.join(h['fund'] for h in out['held_by']) or 'no fund in the graph'}; "
           f"{len(out['correlated'])} strongly correlated asset(s); ESG {a.get('esg_status')}; "
           f"{len(out['controversies'])} controversy line(s) (snapshot {fresh['as_of']}).")
    return _envelope("market_related", {"ticker": ticker}, out, say, fresh)
