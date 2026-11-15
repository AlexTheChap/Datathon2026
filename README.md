# AlphaForge AI — Datathon PolyFinances 2026

Self-explaining, agentic investment decision-support prototype for the
PolyFinances Datathon 2026 challenge: *"Artificial intelligence for investment
decision-making."* (brief: `Datathon - Case Officiel 2026 ENG.pdf`, kept outside
the repo)

**Simulation only — nothing here is investment advice.**

## Quick summary

One or more agents use public financial data (prices, FX, macro rates, corporate
actions) to inform investment analysis for different investor profiles, and
**explain their results**: sources, assumptions, key factors, and limitations.
The pipeline runs:

```
Intake (free text → validated JSON) → Plan resolver (task × profile → sub-agents)
  → Orchestrator → sub-agents (Risk · News · ESG · Fundamentals · Macro · Allocation)
  → Governance (rules: PASS / WARN / BLOCK) → Explainer (evidence-cited answer)
```

Every tool call is recorded as evidence, so each conclusion can be traced back
to observable data and execution steps.

### The three personas we serve

| Persona | Risk | Horizon | What they test |
| --- | --- | --- | --- |
| **Amira** (26, software engineer) | High | 15+ years | Aggressive growth incl. tech & crypto, *with* real diversification — high risk ≠ anything goes |
| **Mateo** (41, operations manager) | Moderate | 8–12 years | ESG constraints and controversy avoidance, not just return chasing |
| **Robert** (59, healthcare professional) | Low–moderate | 3–5 years | Capital preservation — the system must **push back** when a hyped stock conflicts with his profile |

## Architecture — single source of truth

**All design decisions live in [`docs/architecture.mmd`](docs/architecture.mmd).**
Before writing or reviewing any code, read it. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Run the app locally (boxes ① to ⑤)

```bash
.venv/bin/pip install -e ".[dev,api]"

# Sign-in (Amazon Cognito) is always on: settings in backend/app/core/signin.json, nothing to set in .env
cd backend && AF_INTAKE_MODE=mock ../.venv/bin/python -m uvicorn app.api.app:app --port 8000

```

Open http://127.0.0.1:8000. The first log line says which sign-in mode the API runs in, and the page follows it.
A new user sets up the profile in a short conversation (or by dropping a PDF / Word file AlphaForge reads), and
asks questions in a chat. The home page is **My portfolio**: once a profile is saved, AlphaForge builds a simulated portfolio from it (about a
minute) and shows it as a dashboard whose level of detail follows the user's experience.
`AF_INTAKE_MODE=bedrock` (with the `.env` keys) uses the real Claude Haiku intake. Each question runs the
analysis and the Explainer (Claude Sonnet on Bedrock) synchronously (`AF_DISPATCH=inline`, the default): the
live answer is already in the POST response — no queue. `AF_DISPATCH=local` runs them in a background thread
after the 202 (the page polls), and `AF_DISPATCH=none` keeps runs at the plan only.
The Explainer writes
for the reader's profile: plain and short for beginners, technical for advanced readers, and with a glossary for
beginners and intermediates who want to learn (the "Explain things so I learn" switch, or saying "I'm a student").
Every number it states is checked against the evidence it cites; what fails is retried once, then removed.
Details, design rules and the ergonomic checklist: [`frontend/README.md`](frontend/README.md).

## Run the pipeline (boxes ③④⑤⑥)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,bedrock,api]"
.venv/bin/pytest -q                                   # unit tests, no AWS needed

# Requires AWS credentials with Bedrock Claude access (default model + region
# overridable via AF_MODEL_ORCHESTRATOR / AF_AWS_REGION):
.venv/bin/python scripts/smoke_pipeline.py              # full chain: Intake → Plan → Orchestrator → sub-agents
.venv/bin/python scripts/smoke_orchestrator.py          # orchestrator only (plan resolved on the fly)
.venv/bin/python -m app.entrypoint                      # AgentCore Runtime entrypoint on :8080
.venv/bin/python scripts/show_run.py                    # pipeline trace of the latest run

# Optional long-term memory (L17): create once, then export AF_MEMORY_ID=<id>
.venv/bin/python scripts/setup_memory.py
```

Every tool call the agent makes is recorded as evidence in `data/runs/<run_id>.jsonl`
(evidence_id, inputs, assumptions) — the basis for the Explainer's citations.

## On AWS: intake, context resolution, web app (boxes ③ ④ ①)

```bash
.venv/bin/python scripts/deploy_intake_lambda.py --test     # ③ alphaforge-intake (Haiku + Guardrails), both operations tested
.venv/bin/python scripts/deploy_context_lambda.py --test    # ④ alphaforge-context + the explainer.*.md templates in S3
.venv/bin/python scripts/deploy_context_lambda.py --test-only  # is the deployed plan/template still the repo's? (drift check)
.venv/bin/python scripts/deploy_frontend.py --dry-run       # ① S3 + CloudFront (see frontend/README.md)
```

With `AF_INTAKE_MODE=lambda` and `AF_CONTEXT_MODE=lambda`, the API invokes the two deployed Lambdas instead of
running intake and context in-process: the deployed API then needs `lambda:InvokeFunction` on `alphaforge-intake`
and `alphaforge-context`, and neither Bedrock access for the intake nor read access to the template bucket.
Redeploy both after changing `backend/app/intake/`, `backend/app/context/` or `backend/app/prompts/explainer.*.md`,
from an up-to-date `main`, and run `--test-only` to confirm.

## Market graph: S&P 500 on Amazon Neptune (box ⑥)

A graph of the S&P 500 + the curated universe (523 assets): sector, listing country, volatility risk class,
ETF holdings, ESG index status, controversies and strong correlations (|ρ| ≥ 0.7). The agents query it with
`market___screen` (filter and sort by risk) and `market___related` (funds, peers, correlations, ESG of one asset).

```bash
pip install yfinance                                            # local refresh only
python -m app.market.refresh --mode close                       # from backend/: Yahoo -> data/market/ (git-ignored)
python -m app.market.refresh --mode midday --system-certs       # Windows behind a TLS-inspecting proxy

.venv/bin/python scripts/deploy_graph.py --run-now --test       # VPC + Neptune Serverless + refresh/loader + schedules
.venv/bin/python scripts/deploy_quant_gateway.py --test         # tools Lambda into the VPC + `market` Gateway target
.venv/bin/python scripts/deploy_graph.py --delete               # Neptune bills ~1 NCU around the clock: delete after
```

- **Refresh** (EventBridge Scheduler, America/Toronto, Mon-Fri): 11:00 = intraday quote only (never mixed into
  the daily returns); 17:00 = daily close, trailing-252-day metrics, correlations, then a full graph reload.
  17:00 rather than 16:00: Yahoo finalises the bar after the close.
- **Network**: Neptune and the Lambdas that query it sit in private subnets with no Internet route; the refresh
  Lambda (outside the VPC) reaches Yahoo and hands over through S3 (gateway endpoint), so no NAT gateway.
- **Point-in-time**: the graph is a live snapshot; the tools refuse a run dated before it (look-ahead), the
  dated `metrics` tools keep serving historical runs. Without Neptune (`AF_NEPTUNE_ENDPOINT` unset) the same
  graph is built in memory, from `data/market/` or, failing that, from the curated metrics.
- **Source**: Yahoo Finance via yfinance, an unofficial and unlicensed feed (prototype only); it is isolated in
  `app.market.refresh.download`, to be swapped for a licensed feed in production.

## Projections and volatility forecasts (box ⑥)

- **Range of outcomes, not a forecast**: `app/quant/projection.py` simulates monthly paths of a portfolio with the
  expected returns of the FP Canada / Institute of Financial Planning **2026 Projection Assumption Guidelines**
  (`backend/app/core/projection_assumptions.yaml`; gold and crypto use labelled team assumptions) and reports the
  P10–P90 range, nominal and after inflation, the chance of ending below the start and the drawdowns. Questions like
  "how much could my portfolio be worth in 5 years?" route to the `projection` task (risk sub-agent).
- **Risk forecast**: a GARCH(1,1) batch job on SageMaker AI Processing (`docs/garch_job.md`) forecasts each asset's
  volatility; the projection uses it for year 1 and `vol_forecast` says whether risk is rising, stable or easing.

```bash
.venv/bin/python scripts/run_garch_sagemaker.py --local --run-backtest     # data/curated/vol_forecast.csv
curl -X POST localhost:8000/v1/projection -H 'X-Actor-Id: robert' -H 'content-type: application/json' \
     -d '{"weights": {"XIU.TO": 0.6, "XBB.TO": 0.4}, "start_value": 100000, "horizon_years": 10}'   # AF_AUTH_MODE=dev
```

## News alerts by text message (Amazon SNS)

At sign-up, "Inform me when…" (optional) asks which alerts a person wants (a stock they hold goes up, goes down,
or has news) and how: by email or by text message, with a Canadian or U.S. mobile number only for texts. They change
it later in the user menu → **My profile settings** (also: email, password, delete the account). Alerts are only
ever about stocks the person holds. This text sender handles the news alerts of people who chose texts. On weekdays at 18:00 (Toronto), the news-alert agent
(`app.notify.alerts`, no language model) fetches the **SEC 8-K filings of the stocks each person holds** (their
saved portfolio, nothing else) and sends one short text with at most 3 items. The same filing is never texted
twice.

- **Never advice**: fixed wording ("NVDA: SEC 8-K filing on Nov 12 (financial results)."), the same closing line
  ("Info only, not investment advice. Reply STOP to opt out."), and a check that refuses any advice word.
- **Privacy**: the phone number is stored apart from the profile (`ACTOR#…/CONTACT#sms`), so it never reaches an
  agent prompt; opting out deletes it; logs show masked numbers only.
- **Limits**: TSX-only companies (SEDAR+, no free API) and ETFs file no 8-K, so they get no alert.

```bash
python -m app.notify.alerts --source corpus --since 2026-11-01 --until 2026-11-12 --dry-run   # from backend/
.venv/bin/python scripts/deploy_sms_alerts.py --sec-contact "AlphaForge Datathon you@example.com" --test
.venv/bin/python scripts/deploy_sms_alerts.py --verify-phone +15145550123    # SMS sandbox: verify each demo phone
.venv/bin/python scripts/deploy_sms_alerts.py --verify-phone +15145550123 --code 123456
.venv/bin/python scripts/deploy_sms_alerts.py --send-test +15145550123
```

A new AWS account is in the **SMS sandbox**: only verified numbers receive texts (enough for a demo). Texting real
customers needs leaving the sandbox (AWS support) and a registered origination number.

## Repository layout

```
docs/                     # architecture.mmd (source of truth) + future ARCHITECTURE.md, AWS.md
backend/app/
  api/                    # ② API (FastAPI): POST /v1/runs, GET /v1/runs/{id}
  intake/                 # ③ intake agent: free text → Profile + Request JSON
  context/                # ④ deterministic: plan resolver + prompt loader
  agents/                 # ⑤ orchestrator, sub-agents, governance, explainer
  prompts/                # versioned prompt templates (by experience level)
  tools/                  # ⑥ quant tools: metrics, risk, scenarios, optimizer, evidence
  store/                  # ⑥ runs + evidence ledger (DynamoDB analogue)
  core/, schemas/         # config/logging · Pydantic models (Profile, Request, Evidence…)
frontend/                 # ①/⑦ web app (HTML/CSS/JS, no build): ask, profile form, analysis plan — see frontend/README.md
data/{raw,curated,external}/
scripts/                  # e.g. build curated point-in-time datasets
tests/{unit,e2e}/
```
