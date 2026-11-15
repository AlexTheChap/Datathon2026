"""Market refresh: Yahoo Finance -> market tables (Parquet) for the Neptune graph. No LLM, no agent.

Two runs per business day (EventBridge Scheduler, America/Toronto, scripts/deploy_graph.py):

  midday  11:00  today's (partial) daily bar -> market_intraday: last price and change vs previous close.
                 Delayed quote (~15 min on the TSX). It NEVER enters the daily return series.
  close   17:00  ~400 days of daily bars -> market_prices, market_metrics (trailing-252-day volatility,
                 drawdown, VaR, beta vs SPY, risk class), market_correlation (|rho| >= 0.7 pairs).
                 17:00 and not 16:00: the TSX/NYSE close at 16:00 and Yahoo finalises the bar a bit later.

The whole window is re-downloaded at every close (adjusted prices stay internally consistent after a
dividend); the S3 bucket is versioned, so every published version stays retrievable for audit.

Outputs (local directory, then s3://<bucket>/market/ in Lambda):
  market_universe.parquet  market_prices.parquet  market_metrics.parquet  market_correlation.parquet
  market_intraday.parquet  manifest_<mode>.json (what was fetched, missing tickers, quality warnings)

Local run (writes data/market/, git-ignored):
    python -m app.market.refresh --mode close          # from backend/, or with `pip install -e .`
    python -m app.market.refresh --mode midday --system-certs   # Windows behind a TLS-inspecting proxy

Yahoo Finance is an unofficial, unlicensed source (fine for a datathon prototype, not for production):
`download` is the only function that knows about it; swap it for a licensed feed with the same output.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from app.market.metrics import correlation_edges, daily_returns, market_metrics
from app.market.universe import market_universe
from app.tools.curated_store import ENGINE, curated_dir_from_env, parse_s3_uri

SOURCE = "Yahoo Finance via yfinance (unofficial API, delayed quotes, not licensed for commercial use)"
MARKET_TZ = ZoneInfo("America/Toronto")
HISTORY_DAYS = 400          # > 252 trading days for the trailing-year metrics
WINDOW_DAYS = 252
CORR_THRESHOLD = 0.7
MAX_DAILY_MOVE = 0.40       # flagged in the manifest (split or bad tick), never silently dropped
REPO_ROOT = Path(__file__).resolve().parents[3]
PRICE_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]

Downloader = Callable[..., pd.DataFrame]


def market_today() -> date:
    return datetime.now(MARKET_TZ).date()


# ---------- source (the only Yahoo-specific code) ----------

def _to_long(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    frames = []
    level0 = set(raw.columns.get_level_values(0)) if isinstance(raw.columns, pd.MultiIndex) else set()
    for t in tickers:
        if t not in level0:
            continue
        d = raw[t].dropna(subset=["Close"])
        if d.empty:
            continue
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(d.index).tz_localize(None).normalize(), "ticker": t,
            "open": d["Open"].to_numpy(), "high": d["High"].to_numpy(), "low": d["Low"].to_numpy(),
            "close": d["Close"].to_numpy(), "adj_close": d.get("Adj Close", d["Close"]).to_numpy(),
            "volume": d["Volume"].fillna(0).to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PRICE_COLUMNS)


def download(tickers: list[str], *, start: str | None = None, period: str | None = None,
             session: Any = None, retries: int = 3) -> pd.DataFrame:
    """Daily bars from Yahoo (one batched request, then retries on the missing tickers only)."""
    import yfinance as yf

    if Path("/tmp").is_dir() and not os.access(Path.home(), os.W_OK):  # Lambda: read-only home
        yf.set_tz_cache_location("/tmp/py-yfinance")
    got, todo = [], list(tickers)
    for attempt in range(retries):
        if not todo:
            break
        raw = yf.download(todo, start=start, period=None if start else period, interval="1d",
                          auto_adjust=False, actions=False, group_by="ticker", progress=False,
                          threads=True, session=session)
        part = _to_long(raw, todo)
        got.append(part)
        todo = sorted(set(todo) - set(part["ticker"]))
        if todo and attempt < retries - 1:
            time.sleep(5 * 2 ** attempt)  # Yahoo rate limits: back off before retrying the missing ones
    return pd.concat(got, ignore_index=True) if got else pd.DataFrame(columns=PRICE_COLUMNS)


# ---------- runs ----------

def _write(df: pd.DataFrame, out: Path, name: str) -> None:
    df = df.copy()
    for c in df.columns[[pd.api.types.is_datetime64_any_dtype(t) for t in df.dtypes]]:
        df[c] = df[c].astype("datetime64[ns]")  # yfinance gives datetime64[s]: fastparquet reads back ns only
    df.to_parquet(out / f"{name}.parquet", engine=ENGINE, index=False)


def quality_warnings(prices: pd.DataFrame) -> list[str]:
    w = []
    for t, g in prices.sort_values("date").groupby("ticker"):
        r = g["adj_close"].pct_change(fill_method=None).abs()
        for i in r[r > MAX_DAILY_MOVE].index:
            w.append(f"{t}: one-day adjusted move of {r[i]:.0%} on {g.loc[i, 'date']:%Y-%m-%d} (split or bad tick?)")
    return w


def run_close(curated_dir: str | Path, out_dir: str | Path, *, downloader: Downloader = download,
              today: date | None = None, session: Any = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = today or market_today()
    uni = market_universe(curated_dir)
    tickers = uni["ticker"].tolist()
    px = downloader(tickers, start=(today - timedelta(days=HISTORY_DAYS)).isoformat(), session=session)
    px = px[pd.to_datetime(px["date"]) <= pd.Timestamp(today)].copy()  # never a bar dated after the run
    px["date"] = pd.to_datetime(px["date"])
    metrics = market_metrics(px, WINDOW_DAYS)
    corr = correlation_edges(daily_returns(px).tail(WINDOW_DAYS), CORR_THRESHOLD)
    as_of = str(pd.Timestamp(metrics["end"].max()).date()) if not metrics.empty else None

    _write(uni, out, "market_universe")
    _write(px[PRICE_COLUMNS], out, "market_prices")
    _write(metrics, out, "market_metrics")
    _write(corr, out, "market_correlation")
    manifest = {"mode": "close", "as_of": as_of, "run_date": today.isoformat(),
                "generated_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"), "source": SOURCE,
                "tickers_requested": len(tickers), "tickers_with_prices": int(px["ticker"].nunique()),
                "tickers_with_metrics": len(metrics),
                "missing": sorted(set(tickers) - set(px["ticker"])),
                "correlation_edges": len(corr), "window_days": WINDOW_DAYS, "warnings": quality_warnings(px)}
    (out / "manifest_close.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def run_midday(curated_dir: str | Path, out_dir: str | Path, *, downloader: Downloader = download,
               today: date | None = None, session: Any = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = today or market_today()
    uni = market_universe(curated_dir)
    tickers = uni["ticker"].tolist()
    px = downloader(tickers, period="5d", session=session)
    rows = []
    for t, g in px.assign(date=pd.to_datetime(px["date"])).sort_values("date").groupby("ticker"):
        last = g.iloc[-1]
        fresh = last["date"].date() == today
        prev = g.iloc[-2]["close"] if fresh and len(g) > 1 else None
        rows.append({"ticker": t, "last_price": float(last["close"]), "quote_date": last["date"],
                     "prev_close": float(prev) if prev is not None else None,
                     "intraday_change": float(last["close"] / prev - 1) if prev else None,
                     "stale": not fresh})
    intraday = pd.DataFrame(rows, columns=["ticker", "last_price", "quote_date", "prev_close",
                                           "intraday_change", "stale"])
    intraday["fetched_at"] = datetime.now(MARKET_TZ).isoformat(timespec="seconds")
    _write(intraday, out, "market_intraday")
    manifest = {"mode": "midday", "run_date": today.isoformat(), "generated_at": intraday["fetched_at"].iloc[0]
                if len(intraday) else None, "source": SOURCE, "tickers_requested": len(tickers),
                "fresh_quotes": int((~intraday["stale"]).sum()), "stale_quotes": int(intraday["stale"].sum()),
                "missing": sorted(set(tickers) - set(intraday["ticker"]))}
    (out / "manifest_midday.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


RUNS = {"close": run_close, "midday": run_midday}


# ---------- Lambda ----------

def upload_dir(local: Path, uri: str, client=None) -> list[str]:
    import boto3

    client = client or boto3.client("s3")
    bucket, prefix = parse_s3_uri(uri)
    keys = []
    for f in sorted(local.iterdir()):
        if f.suffix in (".parquet", ".json"):
            client.upload_file(str(f), bucket, prefix + f.name)
            keys.append(prefix + f.name)
    return keys


def handler(event: dict | None, context: Any = None) -> dict:
    """EventBridge Scheduler target: {"mode": "midday" | "close"}. Publishes to AF_MARKET_S3, then invokes
    the graph loader (AF_GRAPH_LOADER, inside the VPC) asynchronously with the same mode."""
    import boto3

    mode = (event or {}).get("mode", "close")
    if mode not in RUNS:
        return {"error": f"unknown mode {mode!r}", "modes": sorted(RUNS)}
    curated = curated_dir_from_env(REPO_ROOT / "data" / "curated")
    out = Path("/tmp/market_out")
    shutil.rmtree(out, ignore_errors=True)
    manifest = RUNS[mode](curated, out)
    if mode == "close" and not manifest["tickers_with_prices"]:
        raise RuntimeError(f"no price downloaded, nothing published: {manifest}")  # Lambda retries / alarm
    manifest["published"] = upload_dir(out, os.environ["AF_MARKET_S3"])
    loader = os.environ.get("AF_GRAPH_LOADER")
    if loader and (event or {}).get("load", True):  # {"load": false}: the caller loads Neptune itself
        boto3.client("lambda").invoke(FunctionName=loader, InvocationType="Event",
                                      Payload=json.dumps({"mode": mode}).encode())
        manifest["graph_loader"] = loader
    print(json.dumps({k: v for k, v in manifest.items() if k != "warnings"}, default=str))
    return manifest


# ---------- CLI ----------

def system_cert_session():
    """curl_cffi session trusting the Windows certificate store (TLS-inspecting proxies / antivirus)."""
    import ssl
    import tempfile

    import certifi
    from curl_cffi import requests as curl_requests

    pems = [Path(certifi.where()).read_text()]
    for store in ("ROOT", "CA"):
        for der, enc, _trust in getattr(ssl, "enum_certificates", lambda s: [])(store):
            if enc == "x509_asn":
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    bundle = Path(tempfile.gettempdir()) / "alphaforge_ca_bundle.pem"
    bundle.write_text("\n".join(pems))
    return curl_requests.Session(impersonate="chrome", verify=str(bundle))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=sorted(RUNS), default="close")
    ap.add_argument("--curated", default=str(REPO_ROOT / "data" / "curated"))
    ap.add_argument("--out", default=str(REPO_ROOT / "data" / "market"))
    ap.add_argument("--system-certs", action="store_true", help="trust the OS certificate store (Windows)")
    a = ap.parse_args()
    session = system_cert_session() if a.system_certs else None
    m = RUNS[a.mode](a.curated, a.out, session=session)
    print(json.dumps({k: (v[:10] if isinstance(v, list) else v) for k, v in m.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
