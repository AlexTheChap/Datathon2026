"""SageMaker AI Processing job: GARCH(1,1) volatility forecasts for every ticker of returns_daily_cad.csv.

Forecasts RISK (volatility), never returns. The model code is app/quant/garch.py, the SAME file the backend and
the tests use: scripts/run_garch_sagemaker.py copies app/quant (+ projection_assumptions.yaml) next to this
file in the job's source_dir; in the repo it is imported from backend/.

    python processing.py [--as-of YYYY-MM-DD] [--run-backtest] [--input-dir DIR] [--output-dir DIR]

Input : <input-dir>/returns_daily_cad.csv   (default /opt/ml/processing/input)
Output: <output-dir>/vol_forecast.csv, garch_backtest.csv (with --run-backtest), manifest.json
        (default /opt/ml/processing/output)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _root in (HERE, HERE.parents[1] / "backend"):          # packaged job first, then the repo
    if (_root / "app" / "quant" / "garch.py").is_file():
        sys.path.insert(0, str(_root))
        break
if (HERE / "projection_assumptions.yaml").is_file():
    os.environ.setdefault("AF_PROJECTION_ASSUMPTIONS", str(HERE / "projection_assumptions.yaml"))

import pandas as pd
from app.quant import garch
from app.quant.assumptions import load_assumptions

INPUT_DIR = "/opt/ml/processing/input"
OUTPUT_DIR = "/opt/ml/processing/output"
INPUT_FILE = "returns_daily_cad.csv"
JOB_CONFIG = Path("/opt/ml/config/processingjobconfig.json")
FORECAST_COLUMNS = [*garch.COLUMNS, "job_name"]


def job_name() -> str:
    """The SageMaker job name (from the container's job config), else AF_JOB_NAME, else local-<UTC time>."""
    if JOB_CONFIG.is_file():
        try:
            return json.loads(JOB_CONFIG.read_text())["ProcessingJobName"]
        except (OSError, ValueError, KeyError):
            pass
    return os.environ.get("AF_JOB_NAME") or "local-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def versions() -> dict[str, str]:
    import arch
    import numpy
    import scipy

    return {"python": sys.version.split()[0], "arch": arch.__version__, "numpy": numpy.__version__,
            "pandas": pd.__version__, "scipy": scipy.__version__}


def run(input_dir: str | Path, output_dir: str | Path, as_of: str | None = None, run_backtest: bool = False,
        name: str | None = None) -> dict:
    src = Path(input_dir) / INPUT_FILE
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    returns = pd.read_csv(src, index_col="date", parse_dates=True)
    as_of = as_of or str(returns.index.max().date())
    name = name or job_name()
    lam = float(load_assumptions()["ewma_lambda"])

    fc = garch.fit_all(returns, as_of, ewma_lambda=lam)
    fc["job_name"] = name
    fc.reindex(columns=FORECAST_COLUMNS).to_csv(out / "vol_forecast.csv", index=False, float_format="%.8g")
    counts = {str(k): int(v) for k, v in fc["model"].value_counts().items()}

    manifest = {"as_of": as_of, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "job_name": name, "input_file": INPUT_FILE, "input_sha256": sha256(src),
                "input_last_date": str(returns.index.max().date()), "tickers": len(fc),
                "models": {"garch": counts.get("garch", 0), "ewma": counts.get("ewma", 0),
                           "error": counts.get("error", 0)},
                "ewma_lambda": lam, "min_obs": garch.MIN_OBS, "horizons": list(garch.HORIZONS),
                "forecast_vol": "annualized average volatility over the next 21 trading days",
                "trading_days_per_year": garch.TRADING_DAYS, "versions": versions(),
                "note": "Volatility (risk) forecast only. No return is forecast."}
    if run_backtest:
        # evaluation only: realized volatility AFTER each as_of is compared with what was forecast at as_of
        bt = garch.backtest(returns[returns.index <= pd.Timestamp(as_of)], ewma_lambda=lam)
        bt["job_name"] = name
        bt.to_csv(out / "garch_backtest.csv", index=False, float_format="%.8g")
        manifest["backtest"] = garch.backtest_summary(bt)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description="GARCH volatility forecasts (SageMaker Processing entry point).")
    ap.add_argument("--as-of", default=None, help="forecast date (default: last date of the input file)")
    ap.add_argument("--run-backtest", action="store_true", help="also write garch_backtest.csv")
    ap.add_argument("--input-dir", default=INPUT_DIR)
    ap.add_argument("--output-dir", default=OUTPUT_DIR)
    a = ap.parse_args(argv)
    m = run(a.input_dir, a.output_dir, a.as_of, a.run_backtest)
    print(json.dumps({k: m[k] for k in ("job_name", "as_of", "models")} | ({"backtest": m["backtest"]}
                                                                         if "backtest" in m else {})))
    return m


if __name__ == "__main__":
    main()
