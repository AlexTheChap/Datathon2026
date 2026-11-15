"""Curated data store (box ⑥ "S3 curated Parquet · prices · FX · macro · point-in-time", link L14).

One place that knows how each curated table is read, so the tools get IDENTICAL DataFrames whether the
data comes from the repo CSVs (tests, local runs) or from Parquet files on S3 (the Gateway Lambda):

    read(curated_dir, "returns_daily_cad")    -> <dir>/returns_daily_cad.parquet if present, else the .csv

Publishing (scripts/deploy_data_layer.py): csv_to_parquet() writes every table of TABLES as Parquet with
the same parsing as the CSV readers (DatetimeIndex, string columns kept as strings), then uploads them to
s3://<bucket>/curated/. The Lambda calls sync_from_s3() once per container (to /tmp) and reads locally.

Point-in-time: the files hold the full history; every tool applies its own `as_of` filter (no row dated
after as_of is used; macro series are release-aware: a value appears only from its publication date).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

ENGINE = "fastparquet"  # small enough for a Lambda zip (pyarrow is not)

# table -> pandas.read_csv kwargs that reproduce what the tools expect
TABLES: dict[str, dict[str, Any]] = {
    "returns_daily_cad": {"index_col": "date", "parse_dates": True},      # risk metrics, stress, optimizer
    "prices_cad": {"index_col": "date", "parse_dates": True},             # prices converted to CAD
    "fx_usdcad_filled": {"parse_dates": ["date"]},                        # Bank of Canada USD/CAD
    "macro_daily_asof": {"parse_dates": ["date"]},                        # release-aware macro (point-in-time)
    "macro_pit": {"parse_dates": ["date", "available_from"]},             # long format with publication dates
    "assets_universe": {},
    "etf_holdings": {"dtype": {"ticker": str, "cusip": str}, "keep_default_na": False},
    # market graph reference data (app.market.graph: ESG status, controversies, fallback metrics)
    "esg_index_screen": {"dtype": str, "keep_default_na": False},
    "controversies": {"dtype": str, "keep_default_na": False},
    "asset_risk_metrics": {},
    # projections (app.quant.projection): correlations + the GARCH job's output (sagemaker/garch_job)
    "correlation_full": {"index_col": "ticker"},
    "vol_forecast": {"dtype": {"ticker": str, "model": str, "fallback_reason": str, "job_name": str}},
}


def path_for(curated_dir: str | Path, name: str) -> Path:
    d = Path(curated_dir)
    pq = d / f"{name}.parquet"
    return pq if pq.exists() else d / f"{name}.csv"


def exists(curated_dir: str | Path, name: str) -> bool:
    return path_for(curated_dir, name).exists()


def read(curated_dir: str | Path, name: str) -> pd.DataFrame:
    p = path_for(curated_dir, name)
    if p.suffix == ".parquet":
        df = pd.read_parquet(p, engine=ENGINE)
        # text columns come back as `object` with None; give them the same string dtype as read_csv
        for col in df.columns[df.dtypes == object]:
            df[col] = df[col].astype("str")
        if df.index.dtype == object:          # e.g. correlation_full's ticker index
            df.index = df.index.astype("str")
        return df
    return pd.read_csv(p, **TABLES.get(name, {}))


def csv_to_parquet(src_dir: str | Path, out_dir: str | Path) -> dict[str, int]:
    """Convert every table of TABLES found in src_dir. Returns {table: rows}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = {}
    for name, kwargs in TABLES.items():
        src = Path(src_dir) / f"{name}.csv"
        if not src.exists():
            continue
        df = pd.read_csv(src, **kwargs)
        df.to_parquet(out / f"{name}.parquet", engine=ENGINE, index=df.index.name is not None)
        rows[name] = len(df)
    return rows


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"expected s3://bucket/prefix, got {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix.rstrip("/") + "/" if prefix else ""


def sync_from_s3(uri: str, local_dir: str | Path, client=None) -> list[str]:
    """Download s3://bucket/prefix/*.parquet into local_dir (skips files already there). Returns table names."""
    bucket, prefix = parse_s3_uri(uri)
    if client is None:
        import boto3

        client = boto3.client("s3")
    d = Path(local_dir)
    d.mkdir(parents=True, exist_ok=True)
    names = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet"):
                continue
            target = d / key.rsplit("/", 1)[-1]
            if not target.exists() or target.stat().st_size != obj["Size"]:
                client.download_file(bucket, key, str(target))
            names.append(target.stem)
    return names


def curated_dir_from_env(default: str | Path) -> Path:
    """AF_CURATED_S3 set -> sync the Parquet files to /tmp once and use them; otherwise `default` (repo CSVs)."""
    uri = os.environ.get("AF_CURATED_S3")
    if not uri:
        return Path(default)
    local = Path(os.environ.get("AF_CURATED_CACHE", "/tmp/curated"))
    sync_from_s3(uri, local)
    return local
