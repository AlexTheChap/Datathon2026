"""SageMaker GARCH job: processing.py on a 3-ticker fixture, and the launcher with SageMaker/boto3 mocked."""
import hashlib
import importlib
import json
import shutil
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("arch")

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "returns_daily_cad_3tickers.csv"
sys.path.insert(0, str(ROOT / "sagemaker" / "garch_job"))
sys.path.insert(0, str(ROOT / "scripts"))
processing = importlib.import_module("processing")
launcher = importlib.import_module("run_garch_sagemaker")


@pytest.fixture
def job_dirs(tmp_path):
    inp, out = tmp_path / "input", tmp_path / "output"
    inp.mkdir()
    shutil.copy(FIXTURE, inp / "returns_daily_cad.csv")
    return inp, out


def test_processing_writes_forecast_backtest_and_manifest(job_dirs):
    inp, out = job_dirs
    m = processing.run(inp, out, run_backtest=True, name="garch-test-job")
    fc = pd.read_csv(out / "vol_forecast.csv")
    assert list(fc.columns) == ["ticker", "as_of", "model", "forecast_vol", "vol_h1", "vol_h21", "vol_h63",
                                "vol_h252", "long_run_vol_ann", "hist_vol_63d_ann", "omega", "alpha", "beta", "nu",
                                "persistence", "n_obs", "fallback_reason", "job_name"]
    assert list(fc["ticker"]) == ["SPY", "XBB.TO", "BTC-USD"]
    assert set(fc["job_name"]) == {"garch-test-job"} and set(fc["as_of"]) == {m["as_of"]}
    assert (fc["forecast_vol"] == fc["vol_h21"]).all() and (fc["forecast_vol"] > 0).all()
    bt = pd.read_csv(out / "garch_backtest.csv")
    assert {"ticker", "rmse_garch", "rmse_hist", "qlike_garch", "qlike_hist", "garch_better"} <= set(bt.columns)
    man = json.loads((out / "manifest.json").read_text())
    assert man["job_name"] == "garch-test-job" and man["as_of"] == "2026-11-12"
    assert man["input_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert set(man["versions"]) >= {"arch", "numpy", "pandas"}
    assert sum(man["models"].values()) == 3 and man["backtest"]["tickers"] == 3


def test_processing_as_of_and_no_backtest(job_dirs):
    inp, out = job_dirs
    processing.main(["--input-dir", str(inp), "--output-dir", str(out), "--as-of", "2026-06-30"])
    fc = pd.read_csv(out / "vol_forecast.csv")
    assert set(fc["as_of"]) == {"2026-06-30"} and not (out / "garch_backtest.csv").exists()
    assert (fc["n_obs"] < 620).all()


def test_local_launcher_copies_outputs(tmp_path):
    curated, dest = tmp_path / "curated", tmp_path / "dest"
    curated.mkdir()
    shutil.copy(FIXTURE, curated / "returns_daily_cad.csv")
    m = launcher.run_local(None, False, curated=curated, dest=dest)
    assert sorted(p.name for p in dest.iterdir()) == ["garch_manifest.json", "vol_forecast.csv"]
    assert "GARCH" in launcher.report(m)


def test_source_dir_ships_the_same_model_code(tmp_path):
    build = launcher.build_source_dir(tmp_path / "build")
    same = ROOT / "backend" / "app" / "quant" / "garch.py"
    assert (build / "app" / "quant" / "garch.py").read_bytes() == same.read_bytes()
    assert any(line.startswith("arch") for line in (build / "requirements.txt").read_text().splitlines())
    assert (build / "projection_assumptions.yaml").is_file() and (build / "processing.py").is_file()


def test_env_settings_requires_env_vars():
    with pytest.raises(SystemExit):
        launcher.env_settings({"AWS_REGION": "x"})
    s = launcher.env_settings({"AF_SAGEMAKER_ROLE_ARN": "role", "AF_DATA_BUCKET": "b", "AWS_REGION": "r"})
    assert s["instance_type"] == "ml.m5.large"


def test_sagemaker_launcher_with_mocks(tmp_path, monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, boto_session=None):
            calls["session"] = boto_session

        def upload_data(self, path, bucket, key_prefix):
            calls["upload"] = (Path(path).name, bucket, key_prefix)
            return f"s3://{bucket}/{key_prefix}/returns_daily_cad.csv"

    class FakeProcessor:
        def __init__(self, **kw):
            calls["processor"] = kw
            self.latest_job = types.SimpleNamespace(job_name="alphaforge-garch-2026-11-15")

        def run(self, **kw):
            calls["run"] = kw

    class FakeS3:
        def download_file(self, bucket, key, target):
            name = key.rsplit("/", 1)[-1]
            if name == "manifest.json":
                Path(target).write_text(json.dumps({"job_name": "alphaforge-garch-2026-11-15", "as_of": "2026-11-12",
                                                    "models": {"garch": 3, "ewma": 0}}))
            elif name == "vol_forecast.csv":
                Path(target).write_text("ticker,as_of\nSPY,2026-11-12\n")
            else:
                raise FileNotFoundError(key)

    boto = types.SimpleNamespace(client=lambda name: FakeS3())
    sm = types.ModuleType("sagemaker")
    sm.Session = FakeSession
    sm_proc = types.ModuleType("sagemaker.processing")
    sm_proc.FrameworkProcessor = FakeProcessor
    sm_proc.ProcessingInput = lambda **kw: ("in", kw)
    sm_proc.ProcessingOutput = lambda **kw: ("out", kw)
    sm_sk = types.ModuleType("sagemaker.sklearn")
    sm_est = types.ModuleType("sagemaker.sklearn.estimator")
    sm_est.SKLearn = object
    for name, mod in {"sagemaker": sm, "sagemaker.processing": sm_proc, "sagemaker.sklearn": sm_sk,
                      "sagemaker.sklearn.estimator": sm_est}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(launcher, "BUILD", tmp_path / "build")
    curated, dest = tmp_path / "curated", tmp_path / "dest"
    curated.mkdir()
    shutil.copy(FIXTURE, curated / "returns_daily_cad.csv")
    settings = {"role": "arn:test-role", "bucket": "test-bucket", "region": "test-region", "instance_type": "ml.t3.medium"}
    m = launcher.run_sagemaker(None, True, settings, curated=curated, dest=dest, boto_session=boto)
    p = calls["processor"]
    assert p["role"] == "arn:test-role" and p["instance_type"] == "ml.t3.medium" and p["instance_count"] == 1
    assert p["estimator_cls"] is object and p["framework_version"] == launcher.FRAMEWORK_VERSION
    assert calls["upload"] == ("returns_daily_cad.csv", "test-bucket", "quant/garch/as_of=2026-11-12/input")
    run = calls["run"]
    assert run["code"] == "processing.py" and run["wait"] is True
    assert run["arguments"] == ["--as-of", "2026-11-12", "--run-backtest"]
    assert run["outputs"][0][1]["destination"] == "s3://test-bucket/quant/garch/as_of=2026-11-12/output"
    assert m["sagemaker_job"] == "alphaforge-garch-2026-11-15"
    assert sorted(p.name for p in dest.iterdir()) == ["garch_manifest.json", "vol_forecast.csv"]
