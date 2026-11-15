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

## Sign-in

Amazon Cognito (user pool `alphaforge-users`), set up by `python scripts/setup_cognito.py` (`--delete` removes it).
That script writes the git-ignored `frontend/config.js` (shape in `config.example.js`) and the `AF_COGNITO_*`
settings in `.env`.

- **Demo accounts:** "Sign in as Amira / Mateo / Robert" buttons. Each account starts with that persona's
  profile from the brief (seeded by the API on first sign-in) and is shared by everyone trying the demo.
- **Your own account:** email + password (10+ characters, upper and lower case, a number), confirmed with a
  6-digit code sent by email; "Forgot password?" sends a code to set a new one. Your account starts with no
  profile.
- The browser talks to Cognito directly and sends the ID token to the API (`Authorization: Bearer`). The API
  accepts only tokens signed by this user pool for this app, of type ID, not expired; anything else is a 401
  and the app signs out with a message. Sessions refresh silently for 7 days.
- Without `config.js` the app runs in local dev mode (a demo user name kept in the browser, no password).

## Run it

```bash
.venv/bin/pip install -e ".[dev,api]"
cd backend && AF_AUTH_MODE=dev AF_INTAKE_MODE=mock ../.venv/bin/python -m uvicorn app.api.app:app --port 8000

# with the real sign-in (after scripts/setup_cognito.py): load .env, which sets AF_AUTH_MODE=cognito
(set -a; . ./.env; set +a; cd backend && AF_INTAKE_MODE=mock ../.venv/bin/python -m uvicorn app.api.app:app --port 8000)
```

Open <http://127.0.0.1:8000>. The API serves this folder at `/` (same origin, no CORS). With
`AF_INTAKE_MODE=bedrock` and the keys in `.env` loaded, questions are read by Claude Haiku on Bedrock.
Each question runs the analysis and the Explainer in a background thread (`AF_DISPATCH=local`, the default;
`none` keeps the plan only), so the answer appears in the analysis view (polled every 4 s for 3 min). `AF_AUTH_MODE=dev` lets the browser choose a demo user (`X-Actor-Id`); never enable it on a deployed API.
## Publish it on AWS (S3 + CloudFront)

```bash
.venv/bin/python scripts/deploy_frontend.py --dry-run     # what would be published and created; changes nothing
.venv/bin/python scripts/deploy_frontend.py               # private S3 bucket + CloudFront (HTTPS), prints the URL
.venv/bin/python scripts/deploy_frontend.py --api-url https://<id>.execute-api.us-east-1.amazonaws.com
```

Only `index.html`, `assets/` and a generated `config.js` (the Cognito settings of `frontend/config.js`) are
published; never this README. The bucket stays private: CloudFront reads it through an
Origin Access Control. With `--api-url`, CloudFront also sends `/v1/*` to the API Gateway, so the page calls the API
on its own URL (no CORS) and the Cognito token is forwarded to the API's authorizer. Re-run to update; `--delete`
removes everything. Not deployed yet: it waits for the API (box ②).

## Screens and the API calls behind them

| Screen | What the user does | API |
| --- | --- | --- |
| **My portfolio** (`#/portfolio`, home) | Sees the simulated portfolio AlphaForge built from the profile: whether it fits (verdict, fit score out of 10), what it holds (by asset class and by holding, with amounts), where the ups and downs come from, the sectors, each holding's history, the profile checks, ESG findings, the filings used and why this mix, plus every past analysis. Detail follows the experience (beginner → Simple, intermediate → Standard, advanced → Detailed; switchable and remembered); readers who want to learn get a "What this means" line under each part. Built automatically when a profile is saved (or first opened without a portfolio), rebuilt when the profile changes; if the run measured risk on another mix, one follow-up question measures this exact mix. The portfolio shown is saved in the profile, so questions about "my portfolio" (Ask) are analysed on it. Every chart has "Show the numbers". | `GET /v1/profile`, `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/evidence`, `POST /v1/runs` (build, risk follow-up) |
| **Ask** (`#/ask`) | A conversation, like an AI assistant: the question appears as a message on the right; AlphaForge answers on the left, first with a "thinking" message that walks through the pipeline's stages, then with the whole answer inside the message (short answer written out word by word, profile checks, key points, glossary for learners, limits, how the claims were checked, the evidence opening in place, and "How AlphaForge worked on this": the plan and why each analysis ran). A missing detail is answered with inline choices; a toggle said in chat is confirmed with an Undo; recent questions reload as the start of the conversation; "New conversation" starts over. The composer stays at the bottom (Enter sends, Shift+Enter for a new line) and the conversation id is kept between messages. | `POST /v1/runs`, `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/evidence/catalog`, `PUT /v1/profile` (undo), `GET /v1/profile` |
| **Sign in** (`#/signin`) | Signs in with a demo account (one click) or email + password; creates an account (code by email); resets a forgotten password. Returns to the page they were going to. | Amazon Cognito (from the browser), `GET /v1/me` |
| **Profile** (`#/profile`) | A new user sets the profile up in a conversation (`onboarding.js`): AlphaForge asks one question at a time (risk level and experience on 1-to-10 scales that land in a category, horizon, money and acceptable loss, goal, whether to explain so they learn, hard limits, and every analysis on or off); each answer appears as their message with a "Change" link. Or they drop a PDF or Word file: AlphaForge says what it found, what was irrelevant and ignored, and asks only what is missing. A review, then "Save my profile", which builds the portfolio. With a profile, `#/profile` is the form (scales for risk and experience, examples, analyses switches); `#/profile/assistant` redoes it as a conversation. | `POST /v1/profile/draft`, `POST /v1/profile/draft/file`, `GET /v1/personas`, `GET /v1/profile`, `PUT /v1/profile` |
| **Analysis** (`#/runs/<id>`) | Sees what AlphaForge looks at and why, what was left out and why, then the answer: a short answer, the profile checks (BLOCK / WARN / PASS), key points, a glossary for learners, limits, what was not analysed, and what the automatic check removed. Numbered chips after each sentence open the evidence it rests on (tool, period, sources). Technical details sit behind a disclosure. | `GET /v1/runs/{id}` (polled while running), `GET /v1/runs/{id}/evidence/catalog` |

Recent analyses are remembered in this browser (`localStorage`), per demo user.

The dashboard's code: `assets/portfolio.js` (page and background questions), `assets/portfolio-data.js` (the data,
no DOM: tested with node in `tests/js/`), `assets/charts.js` (bars, segmented bar, meter, heatmap in plain HTML, each
with a legend when needed, tooltips on hover and keyboard focus, and a table view), `assets/universe.json` (names and
plain descriptions of the 31 assets, generated by `scripts/build_frontend_universe.py`). Every figure comes from the
run's evidence or is arithmetic on its weights; chart colours are a validated palette for both modes (tokens
`--viz-*` in `app.css`). It replaces the former Streamlit dashboard.

Motion (all of it off with `prefers-reduced-motion`): pages, cards and messages fade and zoom in slightly, staggered;
bars grow from the left to their value, then their labels fade in; buttons lift on hover and press in on click; the
red bar under the main nav and the thumb of the Simple / Standard / Detailed switch slide to the new choice
(transform only); the fit score counts up; the dashboard cross-fades when the detail level changes; a new answer is
written out word by word in reading order, each part appearing as the text reaches it (the API answers in one piece:
real token streaming would need a streaming endpoint). A red "A" button, bottom right, opens Ask from any page.

## Design

Built under the [Hallmark](https://github.com/Nutlope/hallmark) floor (app-screen scope), checked with its
`sloplint` (0 FAIL, 0 REFLEX, 0 WARN on `index.html` + `app.css`). The stamp at the top of `app.css` records
the decisions.

- **Look:** closely inspired by a Canadian retail bank's public website, measured from the live pages
  (colours, bars, cards, buttons, type sizes): a dark utility bar above a white main bar, white header bands
  with the page title in red, content in white 6px cards on a warm grey page (#F5F4F2), the plan's cards in a
  light blue panel, red (#E41C23) and slate-outline pill buttons, blue links (#1572C5), a grey footer with link
  columns, and a red band on the sign-in page. Not used on purpose: the bank's logo, name, own typefaces or page
  content. The footer states that AlphaForge is a student prototype not affiliated with any bank.
- **Dark mode:** the same structure in near-black and red (the bank's site has none); every pair meets WCAG AA
  in both modes.
- **Type:** Outfit (open licence), the closest free match to the bank's licensed typefaces; weights 400, 500
  and 600.
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
| **8. Compatibility** | Investor vocabulary (risk level, horizon, capital in CAD, "rate hike"), no internal codes on screen; amounts formatted for Canada. |

## Tests

- `tests/unit/test_api.py`: personas endpoint, the frontend served at `/` (and switchable off).
- `tests/e2e/test_frontend_ui.py`: a real browser through the whole flow (inline answer, toggle from chat with
  undo, profile validation, example persona, dark mode persistence, unknown run, phone width without
  horizontal scroll). Skipped unless Playwright is installed:
  `pip install -e ".[ui]" && python -m playwright install chromium`.

## Not there yet

- **Deployment:** sign-in works against the real Cognito today, with the API running locally. On AWS, API
  Gateway's JWT authorizer will check the same tokens, and `scripts/setup_cognito.py` stays the source of the
  pool and demo accounts.
- **Answers:** the plan is shown, but runs stay queued until the dispatcher connects box ② to box ⑤.
- **Portfolio file upload** (presigned S3 URL) and clickable evidence need API endpoints that do not exist yet.
