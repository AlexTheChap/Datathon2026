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
frontend/                 # ①/⑦ Streamlit UI: chat, profile form, results + evidence
data/{raw,curated,external}/
scripts/                  # e.g. build curated point-in-time datasets
tests/{unit,e2e}/
```
