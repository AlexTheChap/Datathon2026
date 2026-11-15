"""Run the GARCH volatility job (sagemaker/garch_job/processing.py) on SageMaker AI Processing, or locally.

    python scripts/run_garch_sagemaker.py --local --run-backtest     # no AWS: same code, same file layout
    python scripts/run_garch_sagemaker.py --run-backtest             # SageMaker Processing job (batch, wait=True)

SageMaker mode needs (environment only, nothing hard-coded):
    AF_SAGEMAKER_ROLE_ARN   execution role of the job (S3 read/write on s3://$AF_DATA_BUCKET/quant/garch/)
    AF_DATA_BUCKET          bucket for the input file and the outputs
    AWS_REGION              region of the job
    AF_GARCH_INSTANCE_TYPE  optional, default ml.m5.large (1 instance)
and the SageMaker Python SDK v2: pip install -e ".[sagemaker]".

Outputs land on s3://$AF_DATA_BUCKET/quant/garch/as_of=YYYY-MM-DD/ and are downloaded to data/curated/:
vol_forecast.csv, garch_backtest.csv (with --run-backtest) and manifest.json saved as garch_manifest.json
(the projection tool reads it to trace numbers back to the job). Batch job only: no real-time endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB_DIR = ROOT / "sagemaker" / "garch_job"
CURATED = ROOT / "data" / "curated"
BUILD = ROOT / "build" / "garch_job"
INPUT_FILE = "returns_daily_cad.csv"
OUTPUTS = {"vol_forecast.csv": "vol_forecast.csv", "garch_backtest.csv": "garch_backtest.csv",
           "manifest.json": "garch_manifest.json"}
S3_PREFIX = "quant/garch"
FRAMEWORK_VERSION = "1.4-2"
DEFAULT_INSTANCE = "ml.m5.large"


def _load_processing():
    sys.path.insert(0, str(JOB_DIR))
    import processing  # sagemaker/garch_job/processing.py

    return processing


def last_date(returns_csv: Path) -> str:
    with open(returns_csv, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 65536))
        tail = f.read().decode("utf-8").strip().splitlines()
    return tail[-1].split(",", 1)[0]


def copy_outputs(src: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name, target in OUTPUTS.items():
        if (src / name).is_file():
            shutil.copy(src / name, dest / target)
            copied.append(target)
    return copied


def run_local(as_of: str | None, run_backtest: bool, curated: Path = CURATED, dest: Path = CURATED) -> dict:
    """Same layout as the container: <tmp>/input/returns_daily_cad.csv -> <tmp>/output/*."""
    proc = _load_processing()
    with tempfile.TemporaryDirectory() as tmp:
        inp, out = Path(tmp) / "input", Path(tmp) / "output"
        inp.mkdir()
        shutil.copy(curated / INPUT_FILE, inp / INPUT_FILE)
        manifest = proc.run(inp, out, as_of, run_backtest)
        manifest["copied"] = copy_outputs(out, dest)
    return manifest


def build_source_dir(build: Path = BUILD) -> Path:
    """processing.py + requirements.txt + app/quant (the shared model code) + projection_assumptions.yaml."""
    shutil.rmtree(build, ignore_errors=True)
    (build / "app" / "quant").mkdir(parents=True)
    for f in ("processing.py", "requirements.txt"):
        shutil.copy(JOB_DIR / f, build / f)
    (build / "app" / "__init__.py").write_text("", encoding="utf-8")
    for f in ("__init__.py", "garch.py", "assumptions.py"):
        shutil.copy(ROOT / "backend" / "app" / "quant" / f, build / "app" / "quant" / f)
    shutil.copy(ROOT / "backend" / "app" / "core" / "projection_assumptions.yaml", build / "projection_assumptions.yaml")
    return build


def env_settings(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    missing = [k for k in ("AF_SAGEMAKER_ROLE_ARN", "AF_DATA_BUCKET", "AWS_REGION") if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)} (or run with --local).")
    return {"role": env["AF_SAGEMAKER_ROLE_ARN"], "bucket": env["AF_DATA_BUCKET"], "region": env["AWS_REGION"],
            "instance_type": env.get("AF_GARCH_INSTANCE_TYPE") or DEFAULT_INSTANCE}


def run_sagemaker(as_of: str | None, run_backtest: bool, settings: dict, curated: Path = CURATED,
                  dest: Path = CURATED, boto_session=None) -> dict:
    import boto3
    from sagemaker.processing import (
        FrameworkProcessor,
        ProcessingInput,
        ProcessingOutput,
    )
    from sagemaker.sklearn.estimator import SKLearn

    from sagemaker import Session

    as_of = as_of or last_date(curated / INPUT_FILE)
    boto_session = boto_session or boto3.Session(region_name=settings["region"])
    session = Session(boto_session=boto_session)
    prefix = f"{S3_PREFIX}/as_of={as_of}"
    s3_input = session.upload_data(str(curated / INPUT_FILE), bucket=settings["bucket"], key_prefix=f"{prefix}/input")
    s3_output = f"s3://{settings['bucket']}/{prefix}/output"
    processor = FrameworkProcessor(
        estimator_cls=SKLearn, framework_version=FRAMEWORK_VERSION, py_version="py3", role=settings["role"],
        instance_type=settings["instance_type"], instance_count=1, base_job_name="alphaforge-garch",
        sagemaker_session=session, env={"PYTHONUNBUFFERED": "1"}, max_runtime_in_seconds=3600,
        code_location=f"s3://{settings['bucket']}/{prefix}/code",      # keep the code on the same prefix
        tags=[{"Key": "project", "Value": "alphaforge"}, {"Key": "component", "Value": "garch"}])
    args = ["--as-of", as_of] + (["--run-backtest"] if run_backtest else [])
    processor.run(code="processing.py", source_dir=str(build_source_dir()),
                  inputs=[ProcessingInput(source=s3_input, destination="/opt/ml/processing/input")],
                  outputs=[ProcessingOutput(source="/opt/ml/processing/output", destination=s3_output,
                                            output_name="garch")],
                  arguments=args, wait=True, logs=True)
    job = processor.latest_job.job_name
    s3 = boto_session.client("s3")
    with tempfile.TemporaryDirectory() as tmp:
        for name in OUTPUTS:
            try:
                s3.download_file(settings["bucket"], f"{prefix}/output/{name}", str(Path(tmp) / name))
            except Exception as e:
                if name != "garch_backtest.csv":
                    raise RuntimeError(f"output {name} missing from {s3_output}: {e}") from e
        manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
        manifest["copied"] = copy_outputs(Path(tmp), dest)
    manifest.update(s3_output=s3_output, sagemaker_job=job)
    return manifest


def report(m: dict) -> str:
    lines = [f"job_name: {m['job_name']}", f"as_of:    {m['as_of']}",
             (f"models:   GARCH {m['models']['garch']} · EWMA fallback {m['models']['ewma']} · "
              f"error {m['models'].get('error', 0)}")]
    if "backtest" in m:
        b = m["backtest"]
        lines.append(f"backtest: GARCH beat 63-day historical volatility on {b['garch_better']} of {b['tickers']} "
                     f"tickers ({b['criterion']}); on RMSE: {b['garch_better_rmse']} of {b['tickers']}")
    if m.get("s3_output"):
        lines.append(f"S3:       {m['s3_output']}")
    lines.append(f"saved:    {', '.join(m.get('copied', []))} -> data/curated/")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--local", action="store_true", help="run processing.py on this machine (no AWS)")
    ap.add_argument("--as-of", default=None, help="forecast date (default: last date of returns_daily_cad.csv)")
    ap.add_argument("--run-backtest", action="store_true")
    a = ap.parse_args(argv)
    m = run_local(a.as_of, a.run_backtest) if a.local else run_sagemaker(a.as_of, a.run_backtest, env_settings())
    print(report(m))
    return m


if __name__ == "__main__":
    main()
