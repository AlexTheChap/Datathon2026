# GARCH volatility job (SageMaker AI Processing)

**What it does.** For every ticker of `data/curated/returns_daily_cad.csv`, the job fits a GARCH(1,1) model with
Student-t errors on the daily CAD returns dated on or before `as_of`, and writes a **volatility (risk)
forecast**. It forecasts risk, never returns: no expected return, no direction, no target price comes out of it.
The projection tool (`app/quant/projection.py`) uses the forecast for the volatility of year 1 of its Monte
Carlo paths. The `vol_forecast` tool also uses it, to say whether a holding's risk is rising, stable or easing.

The model code is `backend/app/quant/garch.py`, the same file the API, the tests and the job use. The job
entry point (`sagemaker/garch_job/processing.py`) only reads the input, calls it and writes the outputs.

| Output | Content |
| --- | --- |
| `vol_forecast.csv` | One row per ticker. Columns: `ticker, as_of, model` (`garch` or `ewma` fallback), `forecast_vol` (annualized average volatility over the next 21 trading days), `vol_h1, vol_h21, vol_h63, vol_h252, long_run_vol_ann, hist_vol_63d_ann, omega, alpha, beta, nu, persistence, n_obs, fallback_reason, job_name` |
| `garch_backtest.csv` | With `--run-backtest`. On a monthly grid from 2023-01, the 21-day GARCH forecast and the trailing 63-day historical volatility are each compared with the volatility realized over the next 21 days. Columns: RMSE, QLIKE, `garch_better` per ticker. This is an evaluation step only. |
| `manifest.json` | `as_of`, `created_at` (UTC), `job_name`, sha256 of the input file, package versions, GARCH versus EWMA counts, and the backtest summary. It is saved locally as `data/curated/garch_manifest.json`. |

**When a fit falls back to EWMA** (`model = ewma`, `fallback_reason` set). This happens with fewer than 500
observations, a constant series, a fit that fails or does not converge, `alpha + beta >= 0.999`, or a NaN
parameter. EWMA uses `ewma_lambda` from `backend/app/core/projection_assumptions.yaml` and gives the same
forecast at every horizon.

**252 trading days a year for every asset, crypto included.** The curated returns use one common trading
calendar (`data/curated/calendar.csv`).

## Run it

```bash
# Locally (no AWS): same code, same input/output layout, outputs copied to data/curated/
.venv/bin/python scripts/run_garch_sagemaker.py --local --run-backtest

# On SageMaker AI Processing (batch job, waits for the end, then downloads the outputs to data/curated/)
.venv/bin/pip install -e ".[sagemaker]"          # SageMaker Python SDK v2
export AF_SAGEMAKER_ROLE_ARN=...   AF_DATA_BUCKET=...   AWS_REGION=...
export AF_GARCH_INSTANCE_TYPE=ml.m5.large           # optional (default ml.m5.large, 1 instance)
.venv/bin/python scripts/run_garch_sagemaker.py --run-backtest [--as-of YYYY-MM-DD]
```

The launcher builds `build/garch_job/` (`processing.py`, `requirements.txt`, `app/quant/`,
`projection_assumptions.yaml`). It uploads the returns file and runs a `FrameworkProcessor` with the SKLearn
estimator class, which installs `requirements.txt` (arch, numpy, pandas, scipy, pyyaml) in the image. At the
end it prints the job name, the `as_of` and the backtest summary. Everything lands under one S3 prefix:

```
s3://$AF_DATA_BUCKET/quant/garch/as_of=YYYY-MM-DD/input/returns_daily_cad.csv
s3://$AF_DATA_BUCKET/quant/garch/as_of=YYYY-MM-DD/code/...        (source_dir uploaded by the SDK)
s3://$AF_DATA_BUCKET/quant/garch/as_of=YYYY-MM-DD/output/vol_forecast.csv, garch_backtest.csv, manifest.json
```

`scripts/deploy_quant_gateway.py` bundles `vol_forecast.csv` with the other curated CSVs when the file is
present, and `scripts/deploy_data_layer.py` converts it to Parquet like the other tables.

## IAM

The **job's execution role** (`AF_SAGEMAKER_ROLE_ARN`) is trusted by `sagemaker.amazonaws.com`. It needs:

- `s3:GetObject` and `s3:PutObject` on `arn:aws:s3:::$AF_DATA_BUCKET/quant/garch/*`, plus `s3:ListBucket` on
  the bucket with an `s3:prefix` condition of `quant/garch/*`.
- `logs:CreateLogGroup`, `logs:CreateLogStream` and `logs:PutLogEvents` on `/aws/sagemaker/ProcessingJobs*`.
- `ecr:GetAuthorizationToken`, `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` to pull the SKLearn image.

The **caller** (whoever runs the launcher) needs:

- `sagemaker:CreateProcessingJob` and `sagemaker:DescribeProcessingJob`.
- `iam:PassRole` on that role (condition `iam:PassedToService = sagemaker.amazonaws.com`).
- S3 read and write on the same prefix.
- `logs:GetLogEvents` to stream the job logs.

## No real-time endpoint

The forecast changes once a day at most, and the projection only needs the latest file. So the job is a batch
Processing job that stops when done. There is no SageMaker endpoint, which would bill around the clock and add
a live dependency to the demo.
