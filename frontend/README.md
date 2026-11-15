# ① Frontend — AlphaForge web app

Plain HTML, CSS and JavaScript (an ES module), no framework and no build step. It talks only to the box ②
API. Simulation only: nothing here is investment advice.

```
frontend/
  index.html          shell: top bar (Ask, Profile, light/dark), main region, footer disclaimer
  assets/app.css      design tokens (light + dark), base rules, components and their states
  assets/app.js       router, API client, the three screens
  assets/favicon.svg
```

## Run it

```bash
.venv/bin/pip install -e ".[dev,api]"
cd backend && AF_AUTH_MODE=dev AF_INTAKE_MODE=mock ../.venv/bin/python -m uvicorn app.api.app:app --port 8000
```

Open <http://127.0.0.1:8000>. The API serves this folder at `/` (same origin, no CORS). With
`AF_INTAKE_MODE=bedrock` and the keys in `.env` loaded, questions are read by Claude Haiku on Bedrock.
`AF_AUTH_MODE=dev` lets the browser choose a demo user (`X-Actor-Id`); never enable it on a deployed API.
On AWS the folder is meant for S3 + CloudFront behind the Cognito login (not deployed yet).

## Screens and the API calls behind them

| Screen | What the user does | API |
| --- | --- | --- |
| **Ask** (`#/ask`) | Writes a question, or picks an example that fills the box. Gets one of three replies: a missing detail to choose inline (risk level, experience), a message (e.g. create a profile first), or an analysis plan. A toggle said in chat ("I don't care about the news") is confirmed with an Undo. | `POST /v1/runs`, `PUT /v1/profile` (undo), `GET /v1/profile` (side summary) |
| **Profile** (`#/profile`) | Fills the investor profile, or starts from Amira, Mateo or Robert. Risk level and experience are required. Analyses can be switched off; a switch changed from chat shows the user's words. | `GET /v1/personas`, `GET /v1/profile`, `PUT /v1/profile` |
| **Analysis plan** (`#/runs/<id>`) | Sees what AlphaForge will look at and why, what was left out and why, the settings (benchmark, scenario), and the answer once box ⑤ writes it. Technical details sit behind a disclosure. | `GET /v1/runs/{id}` (polled while queued) |

Recent analyses are remembered in this browser (`localStorage`), per demo user.

## Design

Built under the [Hallmark](https://github.com/Nutlope/hallmark) floor (app-screen scope), checked with its
`sloplint` (0 FAIL, 0 REFLEX, 0 WARN on `index.html` + `app.css`). The stamp at the top of `app.css` records
the decisions.

- **Palette:** warm off-white paper and near-black ink, one red signal (primary action, current tab, focus,
  selected choice). Dark mode is a second rendering of the same tokens, not an inversion: near-black paper,
  a softer red, the button text switching to dark. Every pair meets WCAG AA in both modes (body text 5:1 to
  17:1, field edges and focus rings 3:1 or more). Inspired by a bank's red-and-white identity and by calm
  consumer-finance apps; no brand's logo, name or exact colours are used.
- **Type:** Source Serif 4 for headings, Public Sans for everything else; two weights.
- **Signature:** the analysis plan, which says in plain words what is analysed and why, and quotes the user
  when their own choice left something out.
- **Every data region has its states:** loading (skeleton), empty (what goes here and the one action that
  fills it), error (what broke, what to do, a retry), full.

## Bastien & Scapin ergonomic criteria

| Criterion | How the app meets it |
| --- | --- |
| **1. Guidance** — prompting | Every field has a visible label and a help line with an example; required fields say "(required)"; example questions show what can be asked. |
| — grouping / distinction | Profile split into named groups (risk, experience, horizon, money, limits, analyses); plan split into "looked at", "left out", "answer", "details". |
| — immediate feedback | Buttons show a spinner and "Asking"/"Saving"; "Saved at 10:03" from the server's answer; toggle changes confirmed in the thread; screen-reader announcements (`aria-live`). |
| — legibility | 16px body, AA contrast in both modes, sentence case, no tracked capitals, line lengths capped. |
| **2. Workload** — brevity | Two required profile fields; one-click example personas; a missing detail is answered with one click and the question is re-sent for you. |
| — information density | The plan shows names and one-line reasons; raw reasons and IDs are folded under "Details"; analyses not needed are listed on one line. |
| **3. Explicit control** | Nothing is sent or saved without a button press (examples only fill the box); Undo for toggles changed from chat; "Discard changes" on the profile. |
| **4. Adaptability** | Profile by form or by chat; keyboard: Ctrl/⌘+Enter to send, skip link, visible focus; light/dark follows the system or the toggle; answers adapt to the experience level. |
| **5. Error management** — protection | Validation before saving (horizon order, positive amounts, 0–100 %); a warning before leaving a profile with unsaved changes. |
| — quality of messages | Each error says what broke and what to do ("Use whole years between 0 and 60"), next to the field, with an icon and text (never colour alone). |
| — correction | The question text is kept when sending fails; "Try again" on errors; "Edit my question" when the task was unclear. |
| **6. Consistency** | One name per analysis everywhere (Ask, Profile, plan); one primary button style; same field recipe and states on every input. |
| **7. Significance of codes** | Status in words ("Queued", "Done"); on/off shown by an icon and a word; red is reserved for the action and the current selection, errors always carry an icon and text. |
| **8. Compatibility** | Investor vocabulary (risk level, horizon, capital in CAD, "crash like 2022"), no internal codes on screen; amounts formatted for Canada. |

## Tests

- `tests/unit/test_api.py`: personas endpoint, the frontend served at `/` (and switchable off).
- `tests/e2e/test_frontend_ui.py`: a real browser through the whole flow (inline answer, toggle from chat with
  undo, profile validation, example persona, dark mode persistence, unknown run, phone width without
  horizontal scroll). Skipped unless Playwright is installed:
  `pip install -e ".[ui]" && python -m playwright install chromium`.

## Not there yet

- **Sign-in:** the Cognito login comes with the AWS deployment of box ②; until then a demo user name is kept
  in the browser.
- **Answers:** the plan is shown, but runs stay queued until the dispatcher connects box ② to box ⑤.
- **Portfolio file upload** (presigned S3 URL) and clickable evidence need API endpoints that do not exist yet.
