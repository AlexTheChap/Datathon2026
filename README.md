# Fitfolio — Datathon PolyFinances 2026

Fitfolio is an agentic investment decision-support prototype built for the PolyFinances Datathon 2026 challenge,
*"Artificial intelligence for investment decision-making"*. From an investor profile, it builds or analyses a
simulated portfolio, answers questions about it in a chat, and explains every result with its sources, assumptions
and limits.

> **Simulation only.** Nothing in this project is investment advice, and no real trade is ever placed.

- **Live demo:** https://d17ol60uy037nc.cloudfront.net (sign-in required)
- **Architecture (source of truth):** [`docs/architecture.mmd`](docs/architecture.mmd)
- **Contributor and coding-agent notes:** [`CLAUDE.md`](CLAUDE.md), [`CONTRIBUTING.md`](CONTRIBUTING.md)

---

## 1. What it does

```
Free text ─▶ Intake (validated JSON) ─▶ Plan resolver (question × profile ─▶ which analyses run)
          ─▶ Orchestrator ─▶ sub-agents: Risk · Macro & rates · ESG · Fundamentals · News · Allocation
          ─▶ Governance (profile rules: PASS / WARN / BLOCK) ─▶ Explainer (answer citing its evidence)
```

- **Every figure is traceable.** Each tool call is stored as an evidence record (inputs, output, assumptions, data
  sources). The Explainer may only state numbers that appear in the evidence it cites; anything else is retried
  once, then removed.
- **Three investor profiles from the brief:** Amira (high risk, 15+ years), Mateo (moderate, ESG constraints) and
  Robert (low-moderate, 3–5 years, capital preservation). They are defined in
  [`backend/app/core/personas.json`](backend/app/core/personas.json).
- **Web app:** step-by-step profile, a "My portfolio" dashboard (fit score out of 100, allocation, risk, range of
  possible values, what-if sliders), an "I already have a portfolio" entry, and the "Ask" chat.

## 2. Repository layout

```
backend/app/
  api/          ② FastAPI app (runs, evidence, profile, fit score, projection, report)
  intake/       ③ free text → Profile + Request (Claude Haiku, or an offline mock)
  context/      ④ deterministic plan resolver + versioned prompt loader
  agents/       ⑤ orchestrator, sub-agents, governance, fit score, explainer
  tools/        ⑥ quant tools: metrics, risk, optimizer, projection, ESG, macro, news
  quant/        ⑥ projection (Monte Carlo) and GARCH volatility forecasts
  market/       ⑥ S&P 500 market graph (Amazon Neptune, or in memory)
  notify/       news alerts by SMS (Amazon SNS)
  core/         settings, rules.yaml, personas.json, projection_assumptions.yaml, signin.json
  prompts/      prompt templates (by experience level)
frontend/       ① web app: plain HTML/CSS/JS, no build step
data/raw/       official starting dataset of the challenge (do not modify)
data/curated/   cleaned, point-in-time tables used by the tools (generated)
data/external/  other public sources, hand-maintained mappings, provenance.csv
scripts/        data builders and AWS deploy scripts
tests/          unit, js and e2e tests (all offline: no AWS account, no model call)
docs/           architecture.mmd, GARCH job notes
```

## 3. Dependencies

| What | Version / notes |
|---|---|
| Python | 3.10+ (developed on 3.12–3.13; the AWS Lambda images use 3.12) |
| Python packages | Declared in [`pyproject.toml`](pyproject.toml): pandas, numpy, scipy, cvxpy, arch (GARCH), pydantic, strands-agents, bedrock-agentcore, boto3, fastparquet, pyyaml |
| Optional extras | `api` (FastAPI, Mangum, PyJWT, pypdf, fpdf2) · `bedrock` (anthropic SDK, truststore) · `dev` (pytest, ruff, httpx, requests) · `scripts` (requests, beautifulsoup4, lxml, openpyxl) · `market` (yfinance) · `ui` (Playwright, uvicorn) · `sagemaker` |
| Node.js | 18+, only to run the frontend's JS unit tests (`node --test`) |
| AWS (for the full pipeline and deployment) | Amazon Bedrock (Claude Sonnet, Haiku), Bedrock AgentCore (Gateway, optional Memory), Bedrock Guardrails, Lambda, S3, CloudFront, DynamoDB, Cognito, SNS, EventBridge Scheduler, SageMaker AI Processing (GARCH), Neptune Serverless (optional market graph), CodeBuild + ECR (API image) |

Install everything used locally:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[api,bedrock,dev,scripts,ui]"     # Windows: .venv\Scripts\pip
.venv/bin/python -m playwright install chromium               # only for the browser tests
```

## 4. Data sources

The full table, with retrieval dates, coverage, processing, limitations and terms of use, is in
[`data/external/provenance.csv`](data/external/provenance.csv) (curated and external files) and
[`data/raw/sources_metadata.csv`](data/raw/sources_metadata.csv) (official starting dataset).

| Data | Source | Used for |
|---|---|---|
| Daily prices, adjusted close, dividends and splits of the 31 starting assets (Nov 2021 – Nov 2026) | Yahoo Finance via yfinance (official starting dataset, `data/raw/`) | Returns, risk metrics, optimizer, projections |
| USD/CAD daily rate | Bank of Canada Valet API (FXUSDCAD) | Conversion of every return to CAD |
| Policy rates, 2y/10y yields, CPI, unemployment (Canada, U.S.) | Bank of Canada Valet API; FRED | Macro and rate-sensitivity analyses (release-aware) |
| S&P 500 extension (~490 companies, prices only) | Yahoo Finance, current SPY constituents | More investable companies (strong survivorship bias) |
| ETF holdings (SPY, XIU.TO, QQQ, ESGV) | State Street, iShares Canada, SEC N-PORT | Looking inside funds (concentration, sectors, exclusions) |
| ESG index screen | Derived from ETF holdings: SPY vs ESGV, XIU vs XCSR / XESG | ESG preference signal |
| Exclusion list | NBIM, observation and exclusion of companies (via the OpenSanctions dataset `no_nbim_exclusions`), plus manual additions with their own public source (e.g. ExxonMobil's 2025 10-K, Note 7) | `esg_flags.json`: shown in the chat, and excluded for profiles with an ESG limit |
| Controversies | NBIM conduct criteria; 10-K excerpts checked word for word, then human-reviewed | Controversy exclusion rule |
| Company financials | SEC EDGAR XBRL company facts (10-K, 10-Q, 40-F) | Fundamentals sub-agent |
| News | SEC 8-K filings and central bank rate decisions | News sub-agent and SMS alerts |
| Expected returns for projections | FP Canada / IQPF Projection Assumption Guidelines 2026 | Range of possible values (never estimated from past prices) |

All sources are public. Yahoo Finance is an unofficial, unlicensed feed and the exclusion lists are for
non-commercial use: both are acceptable for this datathon prototype only.

## 5. Configuration

| File | Role |
|---|---|
| `.env` (copy [`.env.example`](.env.example); git-ignored) | AWS credentials and the optional `AF_*` overrides below. Real environment variables always win over it. |
| [`backend/app/core/signin.json`](backend/app/core/signin.json) | Cognito user pool and web client ids (not secrets; written by `scripts/setup_cognito.py`) |
| [`backend/app/core/rules.yaml`](backend/app/core/rules.yaml) | Profile rules (risk bands, position/sector/crypto caps, horizon rules, exclusions) and the fit score weights |
| [`backend/app/core/projection_assumptions.yaml`](backend/app/core/projection_assumptions.yaml) | Planning-guideline returns, inflation, fees, asset categories |
| [`backend/app/core/personas.json`](backend/app/core/personas.json) | The three demo investors |

Main settings (all optional, read by [`backend/app/core/config.py`](backend/app/core/config.py)):

| Variable | Values | Effect |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` | | Bedrock and AWS access (region `us-east-1`) |
| `AF_INTAKE_MODE` | `mock` (default, offline) · `bedrock` · `lambda` | How free text is read |
| `AF_DISPATCH` | `inline` (default) · `local` · `none` | `none` stops each question at its plan (no model call) |
| `AF_AUTH_MODE` | `cognito` (default) · `dev` | `dev` = pick a demo user without a password (local tests only) |
| `AF_ORCHESTRATION` | `graph` (default, parallel) · `tools` | How the sub-agents run |
| `AF_MODEL_ORCHESTRATOR`, `AF_INTAKE_MODEL` | Bedrock model ids | Override the default models |
| `AF_GUARDRAIL_ID`, `AF_GUARDRAIL_VERSION` | from `scripts/deploy_guardrail.py` | Input filter and answer grounding check |
| `AF_DEMO_PASSWORD` | from `scripts/setup_cognito.py` | Shows the one-click Amira / Robert / Mateo accounts at sign-in |
| `AF_GATEWAY_URL` | from `scripts/deploy_quant_gateway.py` | Quant tools through the AgentCore Gateway instead of in-process |
| `AF_DEFAULT_AS_OF` | `YYYY-MM-DD` | Analysis date when a question gives none (default: last date of the data) |

## 6. Getting started

### a) Offline (no AWS account)

Runs the web app with the mock intake and stops each analysis at its plan. Profile, dashboard figures that need no
model (fit score, projection, what-if) and the tests all work.

```bash
cd backend
AF_INTAKE_MODE=mock AF_AUTH_MODE=dev AF_DISPATCH=none ../.venv/bin/python -m uvicorn app.api.app:app --port 8000
# open http://127.0.0.1:8000
```

### b) Full pipeline (AWS credentials with Bedrock access)

```bash
cp .env.example .env            # paste the AWS credentials
cd backend
AF_INTAKE_MODE=bedrock AF_AUTH_MODE=dev ../.venv/bin/python -m uvicorn app.api.app:app --port 8000
```

Each question then runs the orchestrator, the sub-agents and the Explainer (Claude on Bedrock) before the response
returns, about a minute. Command-line checks of the same chain:

```bash
.venv/bin/python scripts/smoke_pipeline.py      # Intake → Plan → Orchestrator → sub-agents
.venv/bin/python scripts/show_run.py            # trace of the latest run (evidence in data/runs/<run_id>.jsonl)
```

### c) Tests

```bash
.venv/bin/pytest -q tests/unit                  # ~1,300 tests, offline (includes the JS tests if Node is installed)
node --test tests/js/                           # frontend data logic only
.venv/bin/pytest -q tests/e2e                   # browser tests (Playwright + Chromium), ~2 minutes
```

## 7. Reproducing the data

The curated tables are committed, so this is only needed to rebuild or refresh them. Run from the repository root,
in this order (scripts that call SEC EDGAR need a contact e-mail in their User-Agent):

```bash
# 1. Point-in-time tables from the official dataset (prices in CAD, returns, macro, risk metrics, correlations)
.venv/bin/python scripts/build_curated.py --starter data/raw --out data/curated

# 2. Fund holdings, then the ESG index screen and the S&P 500 price extension
.venv/bin/python scripts/etf_holdings.py --as-of 2026-11-14 --email you@example.com
.venv/bin/python scripts/esg_index_screen.py
.venv/bin/python scripts/build_extended_universe.py
.venv/bin/python scripts/build_frontend_universe.py          # investment list used by the web app

# 3. Exclusions and controversies
.venv/bin/python scripts/nbim_exclusions.py                  # NBIM list + conduct criteria → controversies.csv
.venv/bin/python scripts/build_esg_flags.py --refresh        # data/curated/esg_flags.json (+ esg_manual_flags.csv)

# 4. Company financials and news
.venv/bin/python scripts/ingest_fundamentals.py --email you@example.com
.venv/bin/python scripts/ingest_news.py --sources macro sec8k --email you@example.com

# 5. Volatility forecasts (GARCH): locally, or as a SageMaker job
.venv/bin/python scripts/run_garch_sagemaker.py --local --run-backtest
```

`data/raw/` is the challenge's official starting dataset and is never regenerated.

## 8. Deploying on AWS

Each script is idempotent (re-running updates in place) and most accept `--test`. Typical order:

```bash
.venv/bin/python scripts/setup_cognito.py --test             # user pool, web client, demo accounts (writes AF_DEMO_PASSWORD)
.venv/bin/python scripts/deploy_guardrail.py --write-env     # Bedrock Guardrails
.venv/bin/python scripts/deploy_data_layer.py --test         # curated tables to S3 (Parquet)
.venv/bin/python scripts/deploy_quant_gateway.py --test      # quant tools Lambda + AgentCore Gateway
.venv/bin/python scripts/deploy_api.py --test                # API + agents (container image built by CodeBuild)
.venv/bin/python scripts/deploy_frontend.py --api-url <API URL printed above>   # S3 + CloudFront
# optional
.venv/bin/python scripts/deploy_intake_lambda.py --test
.venv/bin/python scripts/deploy_context_lambda.py --test
.venv/bin/python scripts/deploy_graph.py --run-now --test    # Neptune market graph (bills around the clock: --delete after)
.venv/bin/python scripts/deploy_sms_alerts.py --sec-contact "Fitfolio Datathon you@example.com" --test
```

The site's demo accounts only appear if `AF_DEMO_PASSWORD` is set in `.env` when `deploy_frontend.py` runs.

## 9. Known limits

- Five years of daily data (Nov 2021 – Nov 2026); past results do not predict future returns.
- The investable list is the 31 starting assets plus the S&P 500; most Canadian companies are not covered.
- The S&P 500 extension uses today's constituents (survivorship bias) and has no fundamentals or ESG detail.
- Projections are a range of simulated scenarios under planning guidelines, not forecasts.
- Exclusion lists cover only the companies that could be matched to the investable list.
