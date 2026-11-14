# Contributing

## Rule #1 — always check the architecture first

Before opening an issue, a PR, or writing any code, read
**[`docs/architecture.mmd`](docs/architecture.mmd)**. It is the single source of
truth for every component, link, and data flow in this project.

- Every piece of code must map to a node or edge in the diagram. If it doesn't
  belong anywhere on it, propose the diagram change **before** implementing.
- Any change that adds, moves, or removes a component, tool, or data flow must
  update the diagram **in the same commit**. Keep the `%%` comments (AWS tooling
  notes per link) in sync too.
- Detailed explanations belong in `docs/ARCHITECTURE.md` (link-by-link) and
  `docs/AWS.md` (service glossary) — link them from the diagram rather than
  duplicating content.

### Diagram conventions

Node colors in `architecture.mmd`: **orange** = AWS service · **green** = LLM
agent · **blue** = deterministic code (no LLM) · **violet** = JSON data ·
**white** = UI.

## Respect the challenge constraints

Read the datathon brief before designing features:

- **Simulation only.** No real transactions, no presentation as personalized
  financial advice.
- **Self-explaining.** Results must connect to sources, assumptions, observable
  steps, and limitations. Every tool call becomes an evidence entry — never
  invent justifications.
- **Traceability & point-in-time.** Any new data source must document: source,
  retrieval date, processing, limitations, and licence terms (follow the pattern
  in the starter dataset's `sources_metadata.csv`). Don't use information in a
  simulation before it was publicly available (`as_of` discipline).

## Workflow

- Keep commits and PRs small and scoped to one node/edge of the architecture.
- New dependencies go in `pyproject.toml` with a short justification.
- Personas (Amira, Mateo, Robert) are reference test cases: describe how a
  change behaves for each profile when relevant.
