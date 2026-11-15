// AlphaForge frontend (box ① of docs/architecture.mmd): plain ES module, no framework, no build step.
// Talks to the box ② API (POST/GET /v1/runs, GET/PUT /v1/profile, GET /v1/me) and, when
// to Amazon Cognito for the login (settings served by the API as config.js, from backend/app/core/signin.json).
// Every piece of user or server text goes into the page as a text node (never as HTML).

import { renderPortfolio, onProfileSaved, syncPortfolio, needsStartChoice, startChoice } from "./portfolio.js";
import { PORTFOLIO_PROMPT, RISK_PROMPT_PREFIX } from "./portfolio-data.js";
import { buildProfileSteps } from "./profile-steps.js";
import { EXPERIENCE_BANDS, RISK_BANDS, scale } from "./scale.js";

const API_BASE = window.AF_API_BASE || ""; // same origin by default (the API serves this folder locally)
const POLL_EVERY_MS = 4000;
const POLL_MAX = 45; // ~3 minutes: a real analysis (agents + Explainer) takes about a minute

/* ---------------- Vocabulary: one plain name per thing, used everywhere ---------------- */

const AGENTS = {
  risk: { name: "Portfolio risk", desc: "Volatility, worst drop and concentration of your holdings." },
  macro_scenarios: { name: "Macro and rates", desc: "Interest rates, inflation, and how sensitive your holdings are to rate changes." },
  esg: { name: "ESG screening", desc: "Environmental, social and governance checks on the companies in your answers." },
  fundamentals: { name: "Company fundamentals", desc: "Facts from company filings, such as annual reports." },
  news: { name: "News", desc: "What the news says about the assets you mention." },
  allocation: { name: "Allocation proposal", desc: "A suggested split of your money that fits your profile." },
};
const AGENT_ORDER = ["risk", "macro_scenarios", "esg", "fundamentals", "news", "allocation"];

const TASKS = {
  risk_review: "risk review",
  scenario_analysis: "scenario question",
  allocation_proposal: "allocation proposal",
  esg_screen: "ESG screen",
  fundamentals_review: "fundamentals review",
  news_review: "news review",
  projection: "projection of possible outcomes",
  out_of_scope: "request outside the simulation",
};

const RISK = [
  { value: "low", title: "Low", desc: "You want to avoid losses, even if growth is small." },
  { value: "low_moderate", title: "Low to moderate", desc: "Protect your capital first, with some room to grow." },
  { value: "moderate", title: "Moderate", desc: "A balance between growth and stability." },
  { value: "high", title: "High", desc: "You aim for growth and accept large swings." },
];
const EXPERIENCE = [
  { value: "beginner", title: "Beginner", desc: "New to investing. Answers use plain words." },
  { value: "intermediate", title: "Intermediate", desc: "You know stocks, bonds and ETFs." },
  { value: "advanced", title: "Advanced", desc: "You follow the markets. Answers include the technical figures." },
];
// Hard limits. "esg" is one setting for two things: the ESG screening (agent toggle `esg`) and the exclusion of
// companies with a major controversy, which only that screening can check (box ④ forces it while the exclusion is set).
const LIMITS = [
  { key: "crypto", exclusion: { type: "asset_class", value: "crypto" }, title: "No cryptocurrencies",
    desc: "Bitcoin, Ether and other cryptocurrencies are never added to your portfolio or suggested to you." },
  { key: "esg", exclusion: { type: "controversy", min_severity: "major" }, toggle: "esg", title: "Avoid companies with major controversies",
    desc: "Turns on the ESG check (environmental, social and governance). A company with a serious, confirmed scandal is kept out of your portfolio. If one is inside a fund you hold, you get a warning instead." },
];
// The analyses a person switches on or off. ESG is not one of them: it is the hard limit above.
const ANALYSES = AGENT_ORDER.filter((a) => a !== "esg");
const EXAMPLE_QUESTIONS = [
  "Is my portfolio too risky compared to SPY?",
  "How exposed is my portfolio to a rate hike?",
  "I'm thinking of putting 20% in Nvidia, everyone is talking about it.",
  "Do Enbridge and Exxon pass an ESG screen?",
];

/* ---------------- Small utilities ---------------- */

const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: fine */ } },
};

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = Boolean(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const ICONS = {
  check: "M5 12.5l4.5 4.5L19 7.5",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-7.5 8a7.5 7.5 0 0 1 15 0",
  minus: "M6 12h12",
  alert: "M12 8v5m0 3.5v.01M10.3 3.9 2.6 17.3A2 2 0 0 0 4.3 20.3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  moon: "M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.66-5.66 1.41-1.41M4.93 19.07l1.41-1.41m11.32 0 1.41 1.41M4.93 4.93l1.41 1.41M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  send: "M12 19V5m0 0-6 6m6-6 6 6",
  plus: "M12 5v14M5 12h14",
  upload: "M12 16V4m0 0-5 5m5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zm0 0v5h5",
};
function icon(name, size = 16) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(k, v);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function announce(text) {
  const live = document.getElementById("live");
  live.textContent = "";
  window.setTimeout(() => { live.textContent = text; }, 50);
}

function money(n) {
  return new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD", maximumFractionDigits: 0 }).format(n);
}
function horizonText(hz) {
  if (!hz || (hz.min == null && hz.max == null)) return "Not set";
  if (hz.max == null) return `${hz.min} years or more`;
  if (hz.min == null || hz.min === hz.max) return `${hz.max} years`;
  return `${hz.min} to ${hz.max} years`;
}
const titleOf = (list, value) => (list.find((o) => o.value === value) || { title: value }).title;
const timeText = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" });
};

/* ---------------- Identity: Amazon Cognito sign-in. No account, no app. ----------------
 * The API serves the sign-in settings as config.js (backend/app/core/signin.json): sign-in is on by default.
 * DEV (no password, a "guest-…" id kept in this browser) only when config.js says {auth: "dev"}: the API serves that
 * only when started with AF_AUTH_MODE=dev, for local tests. Without sign-in settings the app does not run at all. */

const CONFIG = window.AF_CONFIG || { auth: "none" };
const COGNITO = CONFIG.auth === "cognito";
const DEV = CONFIG.auth === "dev";
const SESSION_KEY = "af.session";
const DEMO_EMAIL = /^(amira|mateo|robert)\.demo@alphaforge\.example$/i;

function readSession() { return store.get(SESSION_KEY); }
function claimsOf(jwt) {
  try {
    const b64 = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(b64.padEnd(b64.length + ((4 - (b64.length % 4)) % 4), "=")), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return {}; }
}

function actorId() {
  if (COGNITO) { const s = readSession(); return s ? claimsOf(s.idToken).sub : null; }
  if (!DEV) return null;  // never a guest without an explicit local test setup
  let id = store.get("af.actor");
  if (!id) {
    id = `guest-${Math.random().toString(36).slice(2, 7)}`;
    store.set("af.actor", id);
  }
  return id;
}

class CognitoError extends Error {
  constructor(type, message) { super(message); this.type = type; }
}

async function cognito(action, body, { withClient = true } = {}) {
  let res;
  try {
    res = await fetch(`https://cognito-idp.${CONFIG.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}` },
      body: JSON.stringify(withClient ? { ClientId: CONFIG.clientId, ...body } : body),
    });
  } catch {
    throw new CognitoError("Network", "AlphaForge can't reach the sign-in service. Check your connection, then try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new CognitoError(String(data.__type || "").split("#").pop(), data.message || "");
  return data;
}

const TOO_MANY = "Too many attempts. Wait a few minutes, then try again.";
function cognitoMessage(err) {
  if (err.type === "Network") return err.message;
  if (/attempts exceeded/i.test(err.message)) return TOO_MANY;
  return {
    NotAuthorizedException: "Incorrect email or password.",
    UserNotFoundException: "Incorrect email or password.",
    UsernameExistsException: "An account already uses this email. Sign in instead.",
    InvalidPasswordException: "The password needs at least 10 characters, with upper case, lower case and a number.",
    CodeMismatchException: "That code doesn't match. Check the latest email we sent you.",
    ExpiredCodeException: "That code has expired. Ask for a new one.",
    LimitExceededException: TOO_MANY,
    TooManyRequestsException: TOO_MANY,
    TooManyFailedAttemptsException: TOO_MANY,
    InvalidParameterException: "Check the email address, then try again.",
    AliasExistsException: "An account already uses this email.",
    CodeDeliveryFailureException: "The email with your code couldn't be sent. Check the address.",
  }[err.type] || "Sign-in didn't work this time. Try again in a moment.";
}

function setSession(result, email, { keepRefresh = null, reset = true, signedInAt = Date.now() } = {}) {
  store.set(SESSION_KEY, {
    idToken: result.IdToken,
    accessToken: result.AccessToken, // My profile settings: change email / password, delete the account
    refreshToken: result.RefreshToken || keepRefresh,
    expiresAt: Date.now() + (result.ExpiresIn || 3600) * 1000,
    signedInAt,
    email,
  });
  if (reset) { state.profile = undefined; state.thread = []; }
}

async function signIn(email, password) {
  const out = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: email, PASSWORD: password } });
  if (!out.AuthenticationResult) throw new CognitoError("Challenge", "");
  setSession(out.AuthenticationResult, email);
}

async function refreshSession() {
  const s = readSession();
  if (!s || !s.refreshToken) return false;
  try {
    const out = await cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", AuthParameters: { REFRESH_TOKEN: s.refreshToken } });
    setSession(out.AuthenticationResult, s.email, { keepRefresh: s.refreshToken, reset: false, signedInAt: s.signedInAt });
    return true;
  } catch { return false; }
}

/** The Cognito access token (account changes in My profile settings), refreshed when close to expiry. */
async function accessToken() {
  const s = readSession();
  if (!s) return null;
  if ((Date.now() > s.expiresAt - 60_000 || !s.accessToken) && !(await refreshSession())) return null;
  return readSession().accessToken;
}
async function idToken() {
  const s = readSession();
  if (!s) return null;
  if (Date.now() > s.expiresAt - 60_000 && !(await refreshSession())) return null;
  return readSession().idToken;
}

function signOut(message = "") {
  const s = readSession();
  try { localStorage.removeItem(SESSION_KEY); } catch { /* ignore */ }
  if (s && s.refreshToken) cognito("RevokeToken", { Token: s.refreshToken }).catch(() => {}); // best effort
  state.profile = undefined; state.thread = []; state.dirty = false;
  try { if (message) sessionStorage.setItem("af.signin.msg", message); } catch { /* ignore */ }
  setWho();
  if (window.location.hash === "#/signin") route(); else window.location.hash = "#/signin";
}

/* ---------------- API client ---------------- */

class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(method, path, body, retried = false) {
  const headers = { "Content-Type": "application/json" };
  if (COGNITO) {
    const token = await idToken();
    if (!token) { signOut("Your session has expired. Sign in again."); throw new ApiError(401, "Sign in to continue."); }
    headers.Authorization = `Bearer ${token}`;
  } else if (DEV) {
    headers["X-Actor-Id"] = actorId();
  } else {
    throw new ApiError(401, "Sign-in is not set up.");
  }
  let res;
  try {
    res = await fetch(API_BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "network");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && COGNITO) {
    // Several requests can be refused at once: only the first signs out, so its precise message stays on screen.
    if (!readSession()) throw new ApiError(401, (data && data.detail) || "Sign in to continue.");
    if (!retried && (await refreshSession())) return api(method, path, body, true);
    // A token refused minutes after signing in is not an expired session: say what really happened.
    const s = readSession();
    if (!s) throw new ApiError(401, (data && data.detail) || "Sign in to continue."); // another request signed out first
    const fresh = s && s.signedInAt && Date.now() - s.signedInAt < 120_000;
    signOut(fresh
      ? "AlphaForge couldn't confirm your sign-in. Please try signing in again."
      : "Your session has expired. Sign in again.");
  }
  if (!res.ok) throw new ApiError(res.status, data && data.detail);
  return { status: res.status, data };
}

/** POST a file (multipart): same sign-in as api(), the browser sets the multipart boundary. */
function errorText(err, what) {
  if (err.status === 0) return { title: "AlphaForge isn't responding", body: "The app can't reach its server. Check your connection, then try again." };
  if (err.status === 401) return { title: "You're not signed in", body: "Sign in again, then try again." };
  const detail = typeof err.detail === "string" ? err.detail : "";
  if (err.status === 502 && /expired|security token|credentials|UnrecognizedClient|AccessDenied/i.test(detail)) {
    return { title: "AlphaForge can't reach its AI service", body: "The service is unavailable right now. Your text is kept: try again in a few minutes." };
  }
  if (err.status === 502 && /could not be reached|unreachable|Throttl/i.test(detail)) {
    return { title: "AlphaForge can't reach its AI service right now", body: "The service didn't answer. Your text is kept: try again in a moment." };
  }
  if (err.status === 502) return { title: `AlphaForge couldn't read ${what}`, body: "AlphaForge didn't get a usable answer this time. Your text is kept: try again." };
  if (err.status === 422 && detail) return { title: "That didn't work", body: detail };
  if (err.status === 422) return { title: "Some details aren't valid", body: "Check the highlighted fields, then try again." };
  return { title: "Something went wrong", body: `The server answered with an error (${err.status}). Try again in a moment.` };
}

function alertBox(err, what, onRetry) {
  const { title, body } = errorText(err, what);
  return h("div", { class: "alert", role: "alert", tabindex: "-1" },
    icon("alert", 18),
    h("p", { class: "alert__title" }, title),
    h("p", { class: "alert__body" }, body),
    onRetry ? h("div", { class: "alert__actions" },
      h("button", { class: "btn btn--secondary", type: "button", onclick: onRetry }, "Try again")) : null);
}

/* ---------------- App state ---------------- */

const state = {
  profile: undefined, // undefined = not loaded, null = none yet
  thread: [],        // the conversation on the Ask page: one turn per question
  sessionId: null,   // the API's conversation id (AgentCore Memory), kept for the follow-up questions
  historyLoaded: false,
  dirty: false,
  pollTimer: null,
};

async function loadProfile(force = false) {
  if (state.profile !== undefined && !force) return state.profile;
  try {
    state.profile = (await api("GET", "/v1/profile")).data;
  } catch (err) {
    if (err.status === 404) state.profile = null;
    else throw err;
  }
  return state.profile;
}

function recentRuns() { return store.get(`af.runs.${actorId()}`, []); }
function rememberRun(id, question) {
  const list = [{ id, q: question, at: new Date().toISOString() }, ...recentRuns().filter((r) => r.id !== id)].slice(0, 8);
  store.set(`af.runs.${actorId()}`, list);
}

/* ---------------- Plain-language plan ---------------- */

function quoteFrom(reason) {
  const m = /user said: "([^"]*)"/.exec(reason || "");
  return m ? m[1] : null;
}

function whyEnabled(agent, task) {
  const r = agent.reason || "";
  const out = [];
  if (r.includes("required by task")) {
    const what = TASKS[task] || "question like yours";
    out.push(`Needed for ${/^[aeiou]/i.test(what) ? "an" : "a"} ${what}.`);
  }
  if (r.includes("include_rag")) out.push("You asked about news or what people are saying.");
  if (r.includes("preference mentions ESG")) out.push("Your profile says ESG matters to you.");
  if (r.includes("profile exclusion")) out.push("Your profile excludes controversial companies, so they must be checked.");
  if (agent.forced) {
    const q = quoteFrom(r);
    out.push(q ? `Kept on even though you said “${q}”, because this question can't be answered without it.`
      : "Kept on even though you turned it off, because this question can't be answered without it.");
  }
  return out.length ? out.join(" ") : "Part of this analysis.";
}

function whyDisabled(item) {
  const r = item.reason || "";
  if (r.includes("out_of_scope")) return "Not analysed: the request is outside what a simulation can do.";
  const q = quoteFrom(r);
  const asked = r.includes("overrides") ? " Your question touched on it, but your choice wins." : "";
  if (q) return `Off because you said “${q}”.${asked}`;
  if (r.includes("agent_toggles")) return `Off in your profile.${asked}`;
  return "Not needed for this question.";
}

// Plan warnings (box ④) in plain words. The ones about toggles are already said in the plan itself.
function plainNote(w) {
  if (/^no portfolio_ref/.test(w)) return "No portfolio file was attached: the analysis uses the assets named in your question.";
  if (/^as_of not set/.test(w)) return "No analysis date was given: the latest available data is used.";
  if (/^intake_confidence/.test(w)) return "Your question was a little unclear: the answer will state the assumptions it makes.";
  if (/enabled although|but disabled by/.test(w)) return null;
  if (/[_=]|\bprofile\.|\bas_of\b/.test(w)) return null; // an internal note: not for the reader
  return w;
}

function settingsOf(agent) {
  const p = agent.parameters || {};
  const out = [];
  if (p.benchmark) out.push(`Compared with ${p.benchmark}.`);
  return out.join(" ");
}

/* ---------------- Router ---------------- */

const routes = [
  { re: /^#\/signin$/, nav: null, title: "Sign in", view: renderSignIn, public: true },
  { re: /^#\/portfolio$/, nav: "portfolio", title: "Your portfolio", view: (main) => renderPortfolio(main, dashboardContext()) },
  { re: /^#\/ask$/, nav: "ask", title: "Ask", view: renderAsk },
  { re: /^#\/profile$/, nav: "profile", title: "Your profile", view: (main) => renderProfile(main) },
  { re: /^#\/profile\/form$/, nav: "profile", title: "Your profile", view: (main) => renderProfile(main) },
  { re: /^#\/profile\/assistant$/, nav: "profile", title: "Your profile", view: (main) => renderProfile(main) },
  { re: /^#\/settings$/, nav: "settings", title: "My profile settings", view: renderSettings },
  { re: /^#\/runs\/([\w-]+)$/, nav: "ask", title: "Analysis plan", view: renderRun },
];

let currentHash = null;
let routeSeq = 0; // one number per page shown: a view that loads asynchronously draws only while it is still current
const HOME = "#/portfolio";

/** What the portfolio dashboard (portfolio.js) needs from the app: same API client, components and state. */
function dashboardContext() {
  return { h, icon, api, band, alertBox, skeleton, announce, money, timeText, VERDICTS, actorId, store, route,
    loadProfile: async (force) => loadProfile(force) };
}
/** No sign-in settings (no config.js, or a server not started with Cognito): nothing of the app is shown. */
function renderNoSignIn(main) {
  document.querySelector(".nav").hidden = true;
  document.getElementById("ask-fab").hidden = true;
  document.title = "Sign-in unavailable, AlphaForge";
  main.replaceChildren(band("head", h("div", { class: "page-head" },
    h("h1", null, "Sign-in isn't available"),
    h("p", null, "AlphaForge can only be used with an account, and this server has no sign-in set up. Try again later."))),
  band("body", h("div", { class: "panel stack-sm" },
    h("p", { class: "small muted" }, "If this keeps happening, contact the team that runs AlphaForge."))));
}
function route() {
  if (!COGNITO && !DEV) { renderNoSignIn(document.getElementById("main")); setWho(); return; }
  const hash = window.location.hash || HOME;
  if (state.dirty && (currentHash || "").startsWith("#/profile") && !hash.startsWith("#/profile")) {
    if (!window.confirm("Your profile has unsaved changes. Leave without saving?")) {
      history.replaceState(null, "", currentHash);
      return;
    }
    state.dirty = false;
  }
  window.clearTimeout(state.pollTimer);
  let match = routes.find((r) => r.re.test(hash));
  if (!match) { match = routes.find((r) => r.nav === "portfolio"); history.replaceState(null, "", HOME); }
  const signedIn = !COGNITO || Boolean(readSession());
  if (COGNITO && !signedIn && !match.public) { // remember where they were going, then sign in
    try { sessionStorage.setItem("af.next", hash); } catch { /* ignore */ }
    history.replaceState(null, "", "#/signin");
    match = routes[0];
  } else if (match.public && signedIn) {
    history.replaceState(null, "", HOME);
    match = routes.find((r) => r.nav === "portfolio");
  }
  document.querySelector(".nav").hidden = !signedIn;
  document.getElementById("ask-fab").hidden = !signedIn || match.nav === "ask" || Boolean(match.public);
  setWho();
  currentHash = window.location.hash || HOME;
  for (const link of document.querySelectorAll(".nav__link, .usermenu__item")) {
    if ((link.dataset.route || "").split(" ").includes(match.nav)) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  closeUserMenu();
  placeNavIndicator();
  document.title = `${match.title}, AlphaForge`;
  const main = document.getElementById("main");
  routeSeq += 1;
  main.replaceChildren();
  const params = hash.match(match.re) || [];
  match.view(main, ...params.slice(1));
  const h1 = main.querySelector("h1");
  if (h1 && document.activeElement !== document.body) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true }); }
  window.scrollTo(0, 0);
}

/** The red bar under the current nav link slides from one link to the next (a single element, moved). */
function placeNavIndicator() {
  const nav = document.querySelector(".nav");
  let bar = nav.querySelector(".nav__indicator");
  if (!bar) { bar = h("span", { class: "nav__indicator", "aria-hidden": "true" }); nav.append(bar); }
  const current = nav.querySelector('.nav__link[aria-current="page"]');
  if (!current || nav.hidden) { bar.style.opacity = "0"; return; }
  const inset = parseFloat(getComputedStyle(current).paddingLeft) || 0;
  bar.style.opacity = "1";
  const left = current.getBoundingClientRect().left - nav.getBoundingClientRect().left; // the user button sits in its own box
  bar.style.transform = `translateX(${left + inset}px) scaleX(${(current.offsetWidth - 2 * inset) / 100})`;
  if (!bar.dataset.ready) requestAnimationFrame(() => { bar.dataset.ready = "1"; });
}
window.addEventListener("resize", () => placeNavIndicator());
if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => placeNavIndicator()); // widths change once the font loads

/* ---------------- Ask: a conversation with AlphaForge ---------------- */

// The pipeline's stages, shown in order while an analysis runs (POST /v1/runs answers when the run is done).
const STEPS = ["Reading your question", "Choosing what to analyse", "Running the analyses",
  "Checking against your profile", "Writing and checking the answer"];
const PLAN_ONLY = /stays at its plan|AF_DISPATCH=none/;
const isFinalRun = (r) => r.status === "DONE" || r.status === "FAILED" || (r.status === "QUEUED" && PLAN_ONLY.test(r.note || ""));

function renderAsk(main) {
  const portfolioReady = syncPortfolio(dashboardContext()).then(() => { state.profile = undefined; drawSide(); }).catch(() => {});
  const log = h("div", { class: "chat__log", role: "log", "aria-label": "Conversation with AlphaForge" });
  const textarea = h("textarea", {
    class: "chat__input", id: "question", name: "question", rows: "1", maxlength: "4000",
    placeholder: "Ask about your portfolio…", "aria-label": "Your question",
    "aria-describedby": "question-help question-error", autocomplete: "off",
  });
  const error = h("p", { class: "field__error", id: "question-error" });
  const submit = h("button", { class: "chat__send", type: "submit", "aria-label": "Ask" }, icon("send", 18));
  const form = h("form", { class: "chat__composer", novalidate: true, onsubmit: (e) => { e.preventDefault(); send(); } },
    h("div", { class: "chat__box" }, textarea, submit),
    error,
    h("p", { class: "chat__help", id: "question-help" },
      "Enter to send, Shift+Enter for a new line. Questions about “my portfolio” use the portfolio AlphaForge built for you."));

  const grow = () => { textarea.style.height = "auto"; textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`; };
  textarea.addEventListener("input", () => { clearError(); grow(); });
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !e.shiftKey)) { e.preventDefault(); send(); }
  });

  function clearError() { error.replaceChildren(); textarea.removeAttribute("aria-invalid"); }
  function showError(msg) {
    error.replaceChildren(icon("alert", 14), h("span", null, msg));
    textarea.setAttribute("aria-invalid", "true");
    textarea.focus();
  }

  const welcome = () => h("div", { class: "chat__welcome" },
    h("span", { class: "msg__avatar msg__avatar--lg", "aria-hidden": "true" }, "A"),
    h("h2", null, "What would you like to know?"),
    h("p", { class: "lede" }, "Ask in your own words. AlphaForge picks the analyses, checks the result against your profile, and shows every source."),
    h("div", { class: "examples", role: "group", "aria-label": "Example questions. Picking one fills the box; nothing is sent until you press Ask." },
      EXAMPLE_QUESTIONS.map((q) => h("button", { class: "example", type: "button",
        onclick: () => { textarea.value = q; grow(); clearError(); textarea.focus(); } }, q))));

  async function send(text = textarea.value.trim(), profileForm = undefined, replaceTurn = null) {
    if (!text) { showError("Write a question first."); return; }
    if (text.length > 4000) { showError("Keep the question under 4,000 characters."); return; }
    const turn = replaceTurn || { text };
    Object.assign(turn, { status: "pending", error: null, reply: null, run: null, fresh: true });
    if (!replaceTurn) state.thread.push(turn);
    if (!profileForm) { textarea.value = ""; grow(); }
    submit.disabled = true;
    drawThread();
    try {
      await portfolioReady; // a portfolio built since the last visit is saved first: "my portfolio" questions use it
      const body = { text };
      if (profileForm) body.profile_form = profileForm;
      if (state.sessionId) body.session_id = state.sessionId;
      const { data } = await api("POST", "/v1/runs", body);
      turn.reply = data;
      if (data.session_id) state.sessionId = data.session_id;
      if (data.kind === "run") {
        rememberRun(data.run_id, text);
        let run = (await api("GET", `/v1/runs/${encodeURIComponent(data.run_id)}`)).data;
        for (let i = 0; !isFinalRun(run) && i < POLL_MAX; i += 1) { // a background dispatcher: wait for the answer
          await new Promise((r) => { window.setTimeout(r, POLL_EVERY_MS); });
          run = (await api("GET", `/v1/runs/${encodeURIComponent(data.run_id)}`)).data;
        }
        turn.run = run;
      }
      turn.status = "done";
      state.profile = undefined; // the intake may have saved or changed the profile
      drawSide();
      // A detail asked in the thread (risk level, experience) is saved with the question: say so, never echo the answer.
      const savedWords = profileForm ? `${[profileForm.risk_band && "Risk level", profileForm.experience && "Experience"].filter(Boolean).join(" and ")} saved. ` : "";
      announce(savedWords + (data.kind === "run" ? "AlphaForge answered." : data.kind === "question" ? "AlphaForge needs one detail." : "AlphaForge replied."));
    } catch (err) {
      turn.status = "error";
      turn.error = err;
      turn.retry = () => send(text, profileForm, turn);
    } finally {
      submit.disabled = false;
      renderTurn(turn);
      scrollToEnd();
    }
  }

  function scrollToEnd() {
    const last = log.lastElementChild;
    if (last) last.scrollIntoView({ block: "end", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  function drawThread() {
    if (!state.thread.length) { log.replaceChildren(welcome()); return; }
    if (log.querySelector(".chat__welcome")) log.replaceChildren();
    for (const turn of state.thread) {
      if (!turn.el) { turn.el = h("div", { class: "turn" }); renderTurn(turn); }
      if (turn.el.parentNode !== log) log.append(turn.el);
    }
    scrollToEnd();
  }

  const you = (text) => h("div", { class: "msg msg--user" },
    h("div", { class: "msg__bubble" }, h("span", { class: "visually-hidden" }, "You asked: "), text));
  const ai = (...content) => h("div", { class: "msg msg--ai" },
    h("span", { class: "msg__avatar", "aria-hidden": "true" }, "A"),
    h("div", { class: "msg__body" }, h("span", { class: "visually-hidden" }, "AlphaForge: "), content));

  function renderTurn(turn) {
    window.clearInterval(turn.timer);
    if (!turn.el) return;
    if (turn.status === "pending") {
      const step = h("span", { class: "thinking__step" }, `${STEPS[0]}…`);
      let i = 0;
      turn.timer = window.setInterval(() => {
        i = Math.min(i + 1, STEPS.length - 1);
        step.textContent = `${STEPS[i]}…`;
        step.classList.remove("is-new"); void step.offsetWidth; step.classList.add("is-new");
        if (i === STEPS.length - 1) window.clearInterval(turn.timer);
      }, 7000);
      turn.el.replaceChildren(you(turn.text), ai(h("div", { class: "thinking", role: "status" },
        h("span", { class: "thinking__dots", "aria-hidden": "true" }, h("span"), h("span"), h("span")), step)));
      return;
    }
    if (turn.status === "error") {
      turn.el.replaceChildren(you(turn.text), ai(alertBox(turn.error, "your question", turn.retry)));
      return;
    }
    const r = turn.reply;
    const parts = [];
    if (r.kind === "question") parts.push(...drawQuestion(turn));
    if (r.kind === "message") {
      parts.push(h("p", null, r.message));
      if (r.intake_status !== "profile_updated" && /profile/i.test(r.message || "")) parts.push(h("div", { class: "notice" }, h("a", { class: "btn btn--secondary btn--sm", href: "#/profile" }, "Set up your profile")));
    }
    if (r.kind === "run") parts.push(...drawRunTurn(turn));
    // A settings-only message already says what changed: each card then only offers to undo it.
    for (const change of r.toggle_changes || []) parts.push(drawToggleChange(change, r.intake_status === "profile_updated"));
    const notes = (r.warnings || []).filter(Boolean);
    if (notes.length) parts.push(h("ul", { class: "warning-list" }, notes.map((w) => h("li", null, w))));
    turn.el.replaceChildren(you(turn.text), ai(parts));
    turn.fresh = false;
  }

  function drawRunTurn(turn) {
    const run = turn.run;
    const names = (turn.reply.enabled_agents || []).map((a) => (AGENTS[a] || { name: a }).name);
    if (!run || (!run.result && run.status !== "FAILED")) { // plan only: this server doesn't run the agents
      return [h("p", { class: "turn__label" }, "Analysis planned"),
        h("p", null, names.length ? `AlphaForge will look at: ${names.join(", ")}.`
          : "Nothing to analyse: this request asks for something a simulation can't do."),
        run ? planDetails(run) : null,
        run && run.note ? h("p", { class: "small muted" }, run.note) : null,
        h("p", { class: "small" }, h("a", { href: `#/runs/${turn.reply.run_id}` }, "Open as a page"))];
    }
    return [drawAnswer(run, { scope: run.run_id, chat: true, animate: turn.fresh }), planDetails(run),
      h("p", { class: "small" }, h("a", { href: `#/runs/${run.run_id}` }, "Open this analysis as a page"))];
  }

  /** "How AlphaForge worked on this": the plan, folded inside the message. */
  function planDetails(run) {
    const plan = run.execution_plan;
    const enabled = plan.enabled_agents || [];
    const userOff = (plan.disabled_agents || []).filter((d) => !/not needed/.test(d.reason));
    return h("details", { class: "fold" }, h("summary", null, "How AlphaForge worked on this"),
      h("div", { class: "stack-sm" },
        enabled.length ? h("ul", { class: "plan plan--compact" }, enabled.map((a) => h("li", { class: "plan__item" },
          h("span", { class: "plan__mark" }, icon("check", 20)),
          h("span", { class: "plan__name" }, (AGENTS[a.agent] || { name: a.agent }).name),
          h("p", { class: "plan__why" }, whyEnabled(a, plan.task))))) : null,
        userOff.length ? h("ul", { class: "plan plan--compact" }, userOff.map((d) => h("li", { class: "plan__item" },
          h("span", { class: "plan__mark plan__mark--off" }, icon("minus", 20)),
          h("span", { class: "plan__name" }, (AGENTS[d.agent] || { name: d.agent }).name),
          h("p", { class: "plan__why" }, whyDisabled(d))))) : null,
        h("p", { class: "small muted" }, `Explained for ${titleOf(EXPERIENCE, run.prompt_ref.experience).toLowerCase()} investors${run.prompt_ref.style === "learning" ? ", so you learn" : ""}. Data up to ${plan.as_of || "the latest date"}.`)));
  }

  function drawQuestion(turn) {
    const r = turn.reply;
    const missing = (r.missing_fields || []).filter((f) => f === "risk_band" || f === "experience");
    const out = [h("p", { class: "turn__label" }, "AlphaForge needs one detail"), h("p", null, r.question)];
    if (!missing.length) {
      out.push(h("div", { class: "notice" },
        h("button", { class: "link-btn", type: "button",
          onclick: () => { textarea.value = turn.text; grow(); textarea.focus(); } }, "Edit my question")));
      return out;
    }
    const answer = {};
    const groups = missing.map((field) => {
      const opts = field === "risk_band" ? RISK : EXPERIENCE;
      const legend = field === "risk_band" ? "Your risk level" : "Your experience";
      return h("fieldset", null, h("legend", { class: "visually-hidden" }, legend),
        h("div", { class: "choices choices--grid" }, opts.map((o) => h("label", { class: "choice" },
          h("input", { type: "radio", name: `${field}-${state.thread.indexOf(turn)}`, value: o.value,
            onchange: () => { answer[field] = o.value; } }),
          h("span", { class: "choice__title" }, o.title),
          h("span", { class: "choice__desc" }, o.desc)))));
    });
    const hint = h("p", { class: "field__error", role: "alert" });
    const go = h("button", { class: "btn btn--secondary", type: "submit" }, "Continue");
    out.push(h("form", { class: "inline-answer", novalidate: true, onsubmit: (e) => {
      e.preventDefault();
      const left = missing.filter((f) => !answer[f]);
      if (left.length) { hint.replaceChildren(icon("alert", 14), h("span", null, "Pick one option in each list.")); return; }
      go.disabled = true;
      send(turn.text, { ...answer, language: "en" }, turn);
    } }, groups, hint,
      h("div", { class: "notice" }, go, h("a", { class: "link-btn", href: "#/profile" }, "Fill in the full profile instead"))));
    return out;
  }

  function drawToggleChange(change, brief = false) {
    const name = (AGENTS[change.agent] || { name: change.agent }).name;
    const line = h("p", null, brief ? `${name}: you said “${change.quote}”.`
      : `${name} is now ${change.enabled ? "on" : "off"} in your profile, because you said “${change.quote}”.`);
    const undo = h("button", { class: "link-btn", type: "button", onclick: async () => {
      undo.disabled = true;
      try {
        await api("PUT", "/v1/profile", { agent_toggles: { [change.agent]: !change.enabled } });
        state.profile = undefined;
        line.textContent = `${name} is back ${change.enabled ? "off" : "on"}.`;
        undo.remove();
        drawSide();
        announce(`${name} change undone.`);
      } catch (err) {
        undo.disabled = false;
        line.textContent = `${errorText(err, "the change").title}. ${name} is still ${change.enabled ? "on" : "off"}.`;
      }
    } }, "Undo");
    return h("div", { class: "notice" }, line, undo);
  }

  /** The last few questions of this user, as the start of the conversation (once per visit). */
  async function loadHistory() {
    if (state.historyLoaded || state.thread.length) return;
    state.historyLoaded = true;
    let runs;
    try { runs = (await api("GET", "/v1/runs?limit=30")).data; } catch { return; }
    const own = runs.filter((r) => r.question !== PORTFOLIO_PROMPT && !r.question.startsWith(RISK_PROMPT_PREFIX)).slice(0, 5).reverse();
    const turns = (await Promise.all(own.map(async (r) => {
      try {
        const run = (await api("GET", `/v1/runs/${encodeURIComponent(r.run_id)}`)).data;
        return { text: r.question, status: "done", run, reply: { kind: "run", run_id: r.run_id, enabled_agents: r.enabled_agents } };
      } catch { return null; }
    }))).filter(Boolean);
    if (!turns.length || state.thread.length) return;
    state.thread.push(...turns);
    log.replaceChildren(h("p", { class: "chat__divider" }, "Earlier questions"));
    drawThread();
  }

  /* Side column: the profile behind the answers */
  const side = h("aside", { class: "stack chat__side", "aria-label": "Your profile" });
  async function drawSide() {
    side.replaceChildren(h("section", { class: "panel" }, skeleton(4)));
    let profile;
    try {
      profile = await loadProfile();
    } catch (err) {
      side.replaceChildren(alertBox(err, "your profile", drawSide));
      return;
    }
    side.replaceChildren(profile ? profileSummary(profile) : noProfile());
  }

  const newChat = h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: () => {
    state.thread.forEach((t) => window.clearInterval(t.timer));
    state.thread = []; state.sessionId = null; state.historyLoaded = true;
    drawThread(); textarea.focus(); announce("New conversation.");
  } }, icon("plus", 16), "New conversation");

  main.append(
    band("head", h("div", { class: "page-head page-head--split" },
      h("div", { class: "stack-sm" },
        h("h1", null, "Ask AlphaForge"),
        h("p", null, "Ask about your portfolio the way you would say it. Each answer shows what was analysed, the checks against your profile, and its sources.")),
      newChat)),
    band("body", h("div", { class: "columns chat" },
      h("section", { class: "chat__main", "aria-label": "Conversation" }, log, form), side)));
  drawThread();
  drawSide();
  loadHistory();
  textarea.focus({ preventScroll: true });
}

function profileSummary(p) {
  const off = ANALYSES.filter((a) => p.agent_toggles && p.agent_toggles[a] === false).map((a) => AGENTS[a].name);
  const limits = LIMITS.filter((l) => (p.exclusions || []).some((e) => e.type === l.exclusion.type)).map((l) => l.title);
  return h("section", { class: "panel stack-sm", "aria-labelledby": "summary-title" },
    h("h2", { id: "summary-title", class: "section-title" }, "Your profile"),
    h("dl", { class: "facts" },
      h("dt", null, "Risk level"), h("dd", null, titleOf(RISK, p.risk_band)),
      h("dt", null, "Horizon"), h("dd", null, horizonText(p.horizon_years)),
      h("dt", null, "Experience"), h("dd", null, titleOf(EXPERIENCE, p.experience)),
      h("dt", null, "Answers"), h("dd", null, p.wants_to_learn ? "Explained so you learn" : "Straight to the point"),
      p.capital ? [h("dt", null, "Capital"), h("dd", { class: "num" }, money(p.capital))] : null,
      h("dt", null, "Portfolio"), h("dd", null, p.portfolio
        ? Object.entries(p.portfolio).sort((a, b) => b[1] - a[1]).map(([t, w]) => `${t} ${Math.round(w * 100)}%`).join(", ")
        : "Not built yet"),
      p.holdings ? [h("dt", null, "What you hold"), h("dd", null,
        Object.entries(p.holdings).sort((a, b) => b[1] - a[1]).map(([t, w]) => `${t} ${Math.round(w * 100)}%`).join(", "))] : null,
      h("dt", null, "Limits"), h("dd", null, limits.length ? limits.join(", ") : "None"),
      h("dt", null, "Analyses off"), h("dd", null, off.length ? off.join(", ") : "None")),
    h("div", { class: "notice" }, h("a", { class: "link-btn", href: HOME }, "My portfolio"),
      h("a", { class: "link-btn", href: "#/profile" }, "Edit profile")));
}

function noProfile() {
  return h("section", { class: "panel empty", "aria-labelledby": "summary-title" },
    h("h2", { id: "summary-title", class: "section-title" }, "No profile yet"),
    h("p", { class: "lede" }, "AlphaForge needs your risk level and experience before it can analyse anything."),
    h("a", { class: "btn btn--secondary", href: "#/profile" }, "Set up your profile"));
}

// A full-width row: "head" (white, page title) or "body" (warm grey, the content), like a bank site's sections.
function band(kind, ...children) {
  return h("div", { class: `band band--${kind}` }, h("div", { class: "container" }, children));
}

function skeleton(lines = 3) {
  return h("div", { class: "skeleton", "aria-hidden": "true" },
    h("div", { class: "skeleton__bar skeleton__bar--title" }),
    Array.from({ length: lines }, (_, i) => h("div", { class: `skeleton__bar${i === lines - 1 ? " skeleton__bar--short" : ""}` })));
}

/* ---------------- Profile ---------------- */

function renderProfile(main) {
  const body = h("div", null, h("div", { class: "panel" }, skeleton(6)));
  const head = band("head", h("div", { class: "page-head" },
    h("div", { class: "stack-sm" },
      h("h1", null, "Your investor profile"),
      h("p", null, "One question at a time. AlphaForge uses your answers to decide what to analyse and how to explain the results. Only your risk level and experience are required."))));
  const seq = routeSeq;
  loadProfile(true).then((profile) => {
    if (seq !== routeSeq) return; // the person went elsewhere (or came back) meanwhile: never draw twice
    main.append(head, band("body", body));
    body.replaceChildren(profileForm(profile));
  }).catch((err) => {
    if (seq !== routeSeq) return;
    main.replaceChildren(head, band("body", alertBox(err, "your profile", () => { main.replaceChildren(); renderProfile(main); })));
  });
}

function profileForm(saved) {
  const recordKey = `af.profile-steps.${actorId()}`;   // which questions this browser has answered or skipped
  let baseline = saved;                                // the profile the last portfolio build was compared with
  const steps = buildProfileSteps({
    h, icon, alertBox, RISK, EXPERIENCE, LIMITS, ANALYSES, AGENTS,
    hasProfile: () => Boolean(state.profile),
    persist: (record) => store.set(recordKey, record),
    setDirty: (on) => { state.dirty = on; },
    async save(payload, { complete }) {
      // Send only the toggles that changed: the API forgets the chat quote of any toggle the form sets.
      const known = state.profile && state.profile.agent_toggles;
      if (known && payload.agent_toggles) {
        const changed = Object.fromEntries(Object.entries(payload.agent_toggles).filter(([a, on]) => known[a] !== on));
        if (Object.keys(changed).length) payload.agent_toggles = changed; else delete payload.agent_toggles;
      }
      const fresh = (await api("PUT", "/v1/profile", payload)).data;
      state.profile = fresh;
      // A new or changed profile gets a new portfolio, built in the background (portfolio.js), once every question is answered.
      // A blank profile (no portfolio yet) first asks how to start: build one, or enter the one the person already has.
      let building = null; let choose = false;
      if (complete) {
        if (needsStartChoice(dashboardContext(), fresh)) choose = true;
        else building = onProfileSaved(dashboardContext(), baseline, fresh);
        baseline = fresh;
      }
      return { profile: fresh, building: Boolean(building), choose };
    },
    startChoice: () => startChoice(dashboardContext(), { title: "Your profile is complete", headingLevel: "h2" }),
  }, { saved, record: store.get(recordKey, null) });
  // The challenge's three investors (Amira, Mateo, Robert) are offered as demo accounts at sign-in only.
  const root = h("div", { class: "stack" }, steps.el);
  state.dirty = false;
  return root;
}
/* ---------------- Run (the plan) ---------------- */

/** The orchestrator's working draft (markdown-lite) as plain paragraphs: bold markers and heading hashes
 * stripped, table rows joined into one line. A preview only — the checked answer replaces it. */
const draftLines = (md) => md.split("\n").map((l) => l.replace(/\*\*/g, "").replace(/^#+\s*/, "").trim())
  .filter(Boolean)
  .map((l) => l.startsWith("|")
    ? l.split("|").map((c) => c.trim()).filter((c) => c && !/^-+$/.test(c)).join(" \u00b7 ")
    : l)
  .map((l) => h("p", { class: "answer__text" }, l));

/* ---------------- The answer (box ⑤ Explainer) ---------------- */

const VERDICTS = {
  BLOCK: { word: "Goes against your profile", cls: "block", icon: "alert" }, PASS: { word: "Fits your profile", cls: "pass", icon: "check" },
  WARN: { word: "Worth a look", cls: "warn", icon: "alert" }, UNVERIFIED: { word: "Not confirmed", cls: "unverified", icon: "alert" },
};

/** The Explainer's answer. `scope` keeps ids unique when several answers share a page (the chat);
 * in the chat (`chat: true`) the evidence opens in place and loads when first needed. */
function drawAnswer(run, { scope = "page", chat = false, animate = false } = {}) {
  const a = run.result && run.result.answer;
  const titleId = `answer-title-${scope}`;
  const section = h(chat ? "div" : "section", { class: chat ? "answer answer--chat" : "panel answer", "aria-labelledby": titleId },
    h(chat ? "h3" : "h2", { id: titleId, class: chat ? "visually-hidden" : null }, "Answer"));
  if (!a) {
    const draft = run.result && run.result.summary;
    if (run.status === "DRAFTED" && draft) { // the analyses are done: show the working draft while it is checked
      section.append(
        h("p", { class: "muted small" }, "Working draft: every number below is being checked against its sources. The checked answer replaces it shortly."),
        h("div", { class: "answer__draft" }, draftLines(draft)));
      return section;
    }
    const waiting = run.status === "QUEUED" || run.status === "RUNNING";
    section.append(h("div", { class: chat ? "stack-sm" : "empty" },
      h("p", null, run.status === "FAILED" ? "No answer: the analysis stopped before the end." : "The answer will appear here."),
      run.status === "QUEUED" && run.note ? h("p", { class: "muted small" }, "Analyses aren't available right now, so AlphaForge stops at the plan.") : null,
      run.status === "RUNNING" ? h("p", { class: "muted small" }, "Working on it: AlphaForge gathers the figures, then writes and checks the answer.") : null,
      !waiting && run.result && run.result.answer_error ? h("p", { class: "muted small" }, "The explanation could not be written this time.") : null));
    return section;
  }

  // Number the evidence in order of first citation: [1], [2]... (ids stay in the evidence list)
  const order = [];
  const cite = (ids) => (ids || []).forEach((id) => { if (!order.includes(id)) order.push(id); });
  cite(a.short_answer_evidence); a.checks.forEach((c) => cite(c.evidence)); a.key_points.forEach((p) => cite(p.evidence));
  a.limitations.forEach((p) => cite(p.evidence));

  const evidenceBox = h("div", { class: "answer__section" });
  let loading = null;
  const chipEls = [];
  // Once the catalogue is in, number only what it shows (profile-rule verdicts are left out: the fit score explains them).
  let onShown = () => {};
  const renumber = (shown) => {
    const visible = order.filter((id) => shown.has(id));
    for (const { el, id } of chipEls) {
      if (!shown.has(id)) { const box = el.parentElement; el.remove(); if (box && !box.children.length) box.remove(); continue; }
      el.textContent = String(visible.indexOf(id) + 1);
      el.setAttribute("aria-label", `Source ${visible.indexOf(id) + 1}`);
    }
    onShown(visible.length);
  };
  const ensureEvidence = () => (loading ||= loadEvidence(run.run_id, order, evidenceBox, scope, renumber));
  const chips = (ids) => (ids || []).length ? h("span", { class: "chips" }, ids.map((id) => {
    const el = h("button", { class: "chip", type: "button", "aria-label": `Source ${order.indexOf(id) + 1}`,
      onclick: async () => { await ensureEvidence(); focusEvidence(id, scope); } }, String(order.indexOf(id) + 1));
    chipEls.push({ el, id });
    return el;
  })) : null;

  const shortText = h("span", { class: "answer__text" }, a.short_answer);
  section.append(h("p", { class: "answer__short" }, shortText, " ", chips(a.short_answer_evidence)));
  if (a.kind !== "explained" && a.note) section.append(h("p", { class: "notice" }, a.note));
  if (a.clarifying_question) section.append(h("p", { class: "notice" }, h("strong", null, "One question for you: "), a.clarifying_question));

  if (a.checks.length) {
    section.append(h("div", { class: "answer__section" }, h("h3", null, "Profile checks"),
      h("div", null, a.checks.map((c) => {
        const v = VERDICTS[c.verdict] || VERDICTS.UNVERIFIED;
        return h("div", { class: "check-row" },
          h("span", { class: `verdict verdict--${v.cls}` }, icon(v.icon, 14), v.word),
          h("span", { class: "check-row__what" }, c.allocation),
          h("p", { class: "check-row__text" }, c.text, " ", chips(c.evidence)));
      }))));
  }
  const list = (title, items) => items.length ? h("div", { class: "answer__section" }, h("h3", null, title),
    h("ul", { class: "answer-list" }, items.map((p) => h("li", null, h("span", null, p.text, " ", chips(p.evidence)))))) : null;
  section.append(...[list("Key points", a.key_points)].filter(Boolean));
  if (a.glossary && a.glossary.length) {
    section.append(h("div", { class: "answer__section" }, h("h3", null, "Words used in this answer"),
      h("dl", { class: "glossary" }, a.glossary.map((g) => [h("dt", null, g.term), h("dd", null, g.meaning)]))));
  }
  section.append(...[list("Limits to keep in mind", a.limitations)].filter(Boolean));
  if (a.not_analysed && a.not_analysed.length) {
    section.append(h("div", { class: "answer__section" }, h("h3", null, "Not analysed"),
      h("ul", { class: "answer-list" }, a.not_analysed.map((t) => h("li", null, t)))));
  }

  const v = a.verification;
  if (v) {
    const removed = v.removed.length;
    const grounded = v.output_guardrail === "passed" || v.output_guardrail === "flagged";
    section.append(h("div", { class: "verification" }, icon(removed ? "alert" : "check", 16), h("div", { class: "stack-sm" },
      h("p", null, removed
        ? `${v.claims_checked - removed} of ${v.claims_checked} statements were checked against the evidence. ${removed} could not be matched to it and ${removed === 1 ? "was" : "were"} removed.`
        : `All ${v.claims_checked} statements were checked against the evidence: every figure comes from a cited source${grounded ? ", and every statement is supported by it" : ""}.`),
      v.output_guardrail === "unavailable" ? h("p", { class: "small muted" }, "The grounding check could not run this time.") : null,
      removed ? h("details", null, h("summary", null, "What was removed"),
        h("ul", { class: "warning-list" }, v.removed.map((r) => h("li", null, `${r.text} (${r.reason})`)))) : null)));
  }
  section.append(h("p", { class: "small muted" }, a.disclaimer));

  if (chat) {
    evidenceBox.append(h("p", { class: "small muted" }, "Loading the sources…"));
    const summary = h("summary", null, "Sources");
    section.append(h("details", { class: "fold", ontoggle: (e) => { if (e.currentTarget.open) ensureEvidence(); } },
      summary, evidenceBox));
    // Loaded now, not on opening: the citation numbers depend on what the catalogue shows.
    onShown = (n) => { summary.textContent = n ? `Sources (${n} cited)` : "Sources"; };
    ensureEvidence();
  } else {
    evidenceBox.id = "evidence-section";
    evidenceBox.append(h("h3", { id: `evidence-title-${scope}` }, "Sources"), h("p", { class: "small muted" }, "Loading the sources…"));
    section.append(evidenceBox);
    ensureEvidence();
  }
  if (animate) streamIn(section);
  return section;
}

/** Write an answer out word by word, in reading order, like a model streaming it: each part appears when the text
 * reaches it. About 4-5 s whatever the length; instant with reduced motion; aria-busy until done so screen readers
 * read the finished answer once. Chips, verdict pills and folded parts are shown as they are. */
function streamIn(root) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const blocks = [...root.children].filter((el) => !el.classList.contains("visually-hidden"));
  const nodes = [];
  for (const block of blocks) {
    const walk = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, { acceptNode: (n) =>
      (!n.textContent.trim() || n.parentElement.closest(".chip, summary, details, .visually-hidden, .verdict"))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    while (walk.nextNode()) nodes.push({ node: walk.currentNode, text: walk.currentNode.textContent, block });
  }
  if (!nodes.length) return;
  const words = nodes.map((n) => n.text.split(/(\s+)/));
  const perTick = Math.max(2, Math.ceil(words.reduce((s, w) => s + w.length, 0) / 160));
  nodes.forEach((n) => { n.node.textContent = ""; });
  blocks.forEach((b) => b.classList.add("is-pending"));
  root.setAttribute("aria-busy", "true");
  root.classList.add("is-streaming");
  let i = 0; let j = 0;
  const finish = () => {
    nodes.forEach((n) => { n.node.textContent = n.text; });
    blocks.forEach((b) => b.classList.remove("is-pending"));
    root.removeAttribute("aria-busy");
    root.classList.remove("is-streaming");
  };
  const tick = () => {
    if ((i || j) && !root.isConnected) { finish(); return; }
    let budget = perTick;
    while (budget > 0 && i < nodes.length) {
      const n = nodes[i];
      n.block.classList.remove("is-pending");
      const take = words[i].slice(j, j + budget);
      n.node.textContent += take.join("");
      budget -= take.length; j += take.length;
      if (j >= words[i].length) { i += 1; j = 0; }
    }
    if (i < nodes.length) window.setTimeout(tick, 28); else finish();
  };
  tick();
}

/** The sources behind the answer: what each tool measured, from which data, over which period. `shown(ids)` gets the
 * cited ids it shows, so the citation chips are renumbered from them (also after a retry). */
async function loadEvidence(runId, order, box, scope = "page", shown = () => {}) {
  let items;
  // The page shows a "Sources" heading; in the chat the fold's summary names it. Never a null child (it prints "null").
  const title = () => (scope === "page" ? [h("h3", { id: `evidence-title-${scope}` }, "Sources")] : []);
  try {
    items = (await api("GET", `/v1/runs/${encodeURIComponent(runId)}/evidence/catalog`)).data;
  } catch (err) {
    box.replaceChildren(...title(), alertBox(err, "the sources", () => loadEvidence(runId, order, box, scope, shown)));
    return;
  }
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  const cited = order.filter((id) => byId[id]);
  // Name the mix a card is about only when the cited cards are about more than one.
  const mixes = new Set(cited.map((id) => byId[id].mix).filter(Boolean));
  const item = (id, n) => {
    const e = byId[id];
    const basis = [(e.sources || []).join(" · "), e.period].filter(Boolean).join(" · ");
    return h("li", { class: "evidence-item", id: `evidence-${scope}-${id}`, tabindex: "-1" },
      h("div", { class: "evidence-item__head" },
        n ? h("span", { class: "evidence-item__num" }, String(n)) : null,
        h("span", { class: "evidence-item__tool" }, e.title || e.tool)),
      mixes.size > 1 && e.mix ? h("p", { class: "evidence-item__meta" }, `For the mix ${e.mix}`) : null,
      h("p", null, e.say || "No summary sentence."),
      basis ? h("p", { class: "evidence-item__meta" }, `Based on: ${basis}`) : null,
      (e.assumptions || []).length ? h("details", null, h("summary", null, "Assumptions"),
        h("ul", { class: "warning-list" }, e.assumptions.map((x) => h("li", null, x)))) : null);
  };
  const rest = items.filter((i) => !cited.includes(i.id));
  box.replaceChildren(...title(),
    h("p", { class: "small muted" }, "What the analysis measured, and the data it used. The numbers match the answer above."),
    h("ol", { class: "evidence-list" }, cited.map((id, i) => item(id, i + 1))),
    ...(rest.length ? [h("details", null, h("summary", null, `Everything else this analysis measured (${rest.length})`),
      h("ol", { class: "evidence-list" }, rest.map((e) => item(e.id, null))))] : []));
  shown(new Set(cited));
}

function focusEvidence(id, scope = "page") {
  const el = document.getElementById(`evidence-${scope}-${id}`);
  if (!el) return;
  let d = el.closest("details");
  while (d) { d.open = true; d = d.parentElement && d.parentElement.closest("details"); }
  el.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  el.focus({ preventScroll: true });
  el.classList.add("is-focus");
  window.setTimeout(() => el.classList.remove("is-focus"), 2500);
}

function renderRun(main, runId) {
  const back = h("p", { class: "back-links" }, h("a", { class: "link-btn", href: HOME }, "My portfolio"),
    h("a", { class: "link-btn", href: "#/ask" }, "Ask a question"));
  const headSlot = h("div", { class: "stack-sm" }, h("div", { class: "skeleton" }, h("div", { class: "skeleton__bar skeleton__bar--title" })));
  const body = h("div", { class: "stack" }, h("div", { class: "panel" }, skeleton(5)));
  main.append(band("head", back, headSlot), band("body", body));
  let polls = 0;
  let shown = null; // what is on screen: re-render only when it changes (keeps "Details" open, keeps focus)

  async function load() {
    let run;
    try {
      run = (await api("GET", `/v1/runs/${encodeURIComponent(runId)}`)).data;
    } catch (err) {
      if (shown) return; // a failed poll keeps the last good view
      if (err.status === 404) {
        headSlot.replaceChildren(h("div", { class: "page-head" }, h("h1", null, "We can't find this analysis"),
          h("p", null, "The link may be wrong, or the analysis belongs to another user.")));
        body.replaceChildren(h("div", { class: "panel empty" }, h("a", { class: "btn btn--secondary", href: "#/ask" }, "Ask a new question")));
      } else {
        headSlot.replaceChildren(h("div", { class: "page-head" }, h("h1", null, "Analysis plan")));
        body.replaceChildren(alertBox(err, "this analysis", load));
      }
      return;
    }
    const waiting = run.status === "QUEUED" || run.status === "RUNNING" || run.status === "DRAFTED";
    const stopped = waiting && polls >= POLL_MAX;
    const key = JSON.stringify([run.status, run.result, run.updated_at, stopped]);
    if (key !== shown) {
      if (shown) announce(`Status: ${statusWord(run.status)}.`);
      const view = drawRun(run, stopped);
      headSlot.replaceChildren(view.head);
      body.replaceChildren(view.content);
      shown = key;
    }
    if (waiting && !stopped && window.location.hash === `#/runs/${runId}`) {
      polls += 1;
      state.pollTimer = window.setTimeout(load, POLL_EVERY_MS);
    }
  }

  function statusWord(s) { return { QUEUED: "Queued", RUNNING: "Running", DRAFTED: "Draft ready", DONE: "Done", FAILED: "Failed" }[s] || s; }

  function drawRun(run, stoppedPolling = false) {
    const plan = run.execution_plan;
    const enabled = plan.enabled_agents || [];
    const userOff = (plan.disabled_agents || []).filter((d) => !/not needed/.test(d.reason));
    const notNeeded = (plan.disabled_agents || []).filter((d) => /not needed/.test(d.reason));
    const statusLine = {
      QUEUED: "Waiting to start.",
      RUNNING: "AlphaForge is analysing and writing your answer. This takes about a minute; the page updates by itself.",
      DONE: "The analysis is complete.",
      FAILED: "The analysis stopped before the end. You can ask your question again.",
    }[run.status];

    const jump = (id) => () => { const el = document.getElementById(id); if (el) { el.scrollIntoView({ block: "start" }); el.setAttribute("tabindex", "-1"); el.focus({ preventScroll: true }); } };
    const head = h("div", { class: "page-head" },
      h("p", { class: "run-label" }, run.request.user_message === PORTFOLIO_PROMPT ? "How your portfolio was built"
        : run.request.user_message.startsWith(RISK_PROMPT_PREFIX) ? "Risk check of your portfolio" : "Your question"),
      h("h1", { class: "run-question" }, run.request.user_message),
      h("p", { class: "status" }, h("span", { class: "status__word" }, statusWord(run.status)), h("span", null, statusLine),
        stoppedPolling ? h("button", { class: "link-btn", type: "button", onclick: () => { polls = 0; shown = null; load(); } }, "Check again") : null),
      h("nav", { class: "on-page", "aria-label": "On this page" },
        h("span", { class: "on-page__label" }, "On this page"),
        h("button", { type: "button", onclick: jump("plan-title") }, "What AlphaForge will look at"),
        h("button", { type: "button", onclick: jump("left-title") }, "Left out"),
        h("button", { type: "button", onclick: jump("answer-title-page") }, "Answer"),
        h("button", { type: "button", onclick: jump("details-title") }, "Details")));

    const lookedAt = h("section", { class: "plan-panel", "aria-labelledby": "plan-title" },
      h("h2", { id: "plan-title" }, "What AlphaForge will look at"),
      enabled.length
        ? h("ul", { class: "plan" }, enabled.map((a) => h("li", { class: "plan__item" },
          h("span", { class: "plan__mark" }, icon("check", 28)),
          h("span", { class: "plan__name" }, (AGENTS[a.agent] || { name: a.agent }).name),
          h("p", { class: "plan__why" }, whyEnabled(a, plan.task)),
          settingsOf(a) ? h("p", { class: "plan__setting" }, settingsOf(a)) : null)))
        : h("div", { class: "panel empty" }, h("p", null, "Nothing to analyse. This request asks for something a simulation can't do, such as placing a real trade. The answer explains why.")));

    const leftOut = h("section", { class: "stack-sm", "aria-labelledby": "left-title" },
      h("h2", { id: "left-title" }, "Left out"),
      userOff.length ? h("ul", { class: "plan" }, userOff.map((d) => h("li", { class: "plan__item" },
        h("span", { class: "plan__mark plan__mark--off" }, icon("minus", 28)),
        h("span", { class: "plan__name" }, (AGENTS[d.agent] || { name: d.agent }).name),
        h("p", { class: "plan__why" }, whyDisabled(d))))) : null,
      notNeeded.length ? h("p", { class: "muted" }, `Not needed for this question: ${notNeeded.map((d) => (AGENTS[d.agent] || { name: d.agent }).name).join(", ")}. An analysis switched on in your profile runs only when a question needs it.`) : null,
      !userOff.length && !notNeeded.length ? h("p", { class: "muted" }, "Nothing was left out.") : null);

    const answer = drawAnswer(run, { scope: "page" });

    const req = run.request;
    const details = h("details", null,
      h("summary", { id: "details-title" }, "Details"),
      h("dl", { class: "tech" },
        h("dt", null, "Type of question"), h("dd", null, TASKS[plan.task] || plan.task),
        (req.positions || []).length ? [h("dt", null, "Assets mentioned"), h("dd", { class: "num" },
          req.positions.map((p) => p.weight == null ? p.ticker : `${p.ticker} ${Math.round(p.weight * 100)}%`).join(", "))] : null,
        h("dt", null, "Data up to"), h("dd", { class: "num" }, plan.as_of || "Latest available"),
        h("dt", null, "How sure the reading is"), h("dd", { class: "num" }, `${Math.round(plan.intake_confidence * 100)}%`),
        h("dt", null, "Explained for"), h("dd", null, `${titleOf(EXPERIENCE, run.prompt_ref.experience)} investors${run.prompt_ref.style === "learning" ? ", explained so you learn" : ""}`),
        (plan.warnings || []).map(plainNote).filter(Boolean).length ? [h("dt", null, "Notes"), h("dd", null,
          h("ul", { class: "warning-list" }, plan.warnings.map(plainNote).filter(Boolean).map((w) => h("li", null, w))))] : null,
        h("dt", null, "Created"), h("dd", null, timeText(run.created_at))));

    const p = run.profile;
    const basedOn = h("section", { class: "panel stack-sm", "aria-labelledby": "based-title" },
      h("h2", { id: "based-title", class: "section-title" }, "Based on your profile"),
      h("dl", { class: "facts" },
        h("dt", null, "Risk level"), h("dd", null, titleOf(RISK, p.risk_band)),
        h("dt", null, "Horizon"), h("dd", null, horizonText(p.horizon_years)),
        h("dt", null, "Experience"), h("dd", null, titleOf(EXPERIENCE, p.experience))),
      h("p", { class: "small muted" }, "As it was when you asked. Later changes to your profile don't rewrite this analysis."));

    return { head, content: h("div", { class: "columns" }, h("div", { class: "stack" }, lookedAt, leftOut, answer, details), h("aside", null, basedOn)) };
  }

  load();
}

/* ---------------- News alerts by text (GET / PUT /v1/notifications/sms) ---------------- */

const SMS_PENDING = "af.sms.pending"; // what older pages stored at sign-up (texts only); read once by flushNotifyChoice

/** Canadian or U.S. mobile number: 10 digits, or 11 starting with 1. The API checks it again. */
function phoneProblem(v) {
  const d = String(v || "").replace(/\D/g, "");
  if (d.length === 10 || (d.length === 11 && d[0] === "1")) return null;
  return "Enter a Canadian or U.S. mobile number, like 514 555 0123.";
}

/* ---------------- Notifications (phone number + news checkbox) and My profile settings ---------------- */

const NOTIFY_PENDING = "af.notify.pending"; // the sign-up choice, saved once the new account is signed in
const NOTIFY_WHAT = "Only about stocks you hold. Facts only, never advice. By text message to the phone number above. Standard text message rates may apply. You can change this at any time in My profile settings.";

/** Phone number + one checkbox, "Notify me on news related to my portfolio" (text message to that number).
 * read() -> { prefs, problem } where problem = { field, message }. Unchecking the box turns notifications off
 * (the API then deletes the number). */
function notifyChoiceFields(prefix) {
  const phoneInput = h("input", { class: "input", id: `${prefix}-phone`, name: "phone", type: "tel", inputmode: "tel", autocomplete: "tel-national",
    placeholder: "514 555 0123", "aria-describedby": `${prefix}-phone-help ${prefix}-phone-error` });
  const phoneError = h("p", { class: "field__error", id: `${prefix}-phone-error` });
  const phoneEl = h("div", { class: "field" }, h("label", { class: "field__label", for: `${prefix}-phone` }, "Phone number"), phoneInput,
    h("p", { class: "field__help", id: `${prefix}-phone-help` }, "Canadian or U.S. mobile number. Used only for the text alerts below."), phoneError);
  phoneInput.addEventListener("input", () => { phoneError.replaceChildren(); phoneInput.removeAttribute("aria-invalid"); });
  const box = h("input", { type: "checkbox", id: `${prefix}-notify`, name: `${prefix}-notify`, value: "news" });
  const boxError = h("p", { class: "field__error", id: `${prefix}-notify-error` });
  box.addEventListener("change", () => boxError.replaceChildren());
  const el = h("div", { class: "stack-sm notify" },
    phoneEl,
    h("div", { class: "choices" }, h("label", { class: "choice" }, box,
      h("span", { class: "choice__title" }, "Notify me on news related to my portfolio"))),
    boxError,
    h("p", { class: "field__help" }, NOTIFY_WHAT));
  return {
    el,
    refresh() {},
    set(prefs) {
      const on = Boolean(prefs && prefs.opt_in && prefs.channel === "sms" && (prefs.alerts || []).includes("news"));
      box.checked = on;
      phoneInput.value = (on && prefs.phone) || "";
    },
    read() {
      if (!box.checked) return { prefs: { opt_in: false }, problem: null };
      const problem = phoneProblem(phoneInput.value);
      if (problem) return { prefs: null, problem: { field: { input: phoneInput, error: phoneError }, message: problem } };
      return { prefs: { opt_in: true, channel: "sms", phone: phoneInput.value, alerts: ["news"] }, problem: null };
    },
  };
}

/** Saves the choice made on the sign-up form, once the new account has a session (older pages saved texts only). */
async function flushNotifyChoice() {
  let pending = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(NOTIFY_PENDING) || "null");
    const oldSms = JSON.parse(sessionStorage.getItem(SMS_PENDING) || "null");
    if (!pending && oldSms) pending = { opt_in: true, channel: "sms", phone: oldSms.phone, alerts: ["news"] };
    sessionStorage.removeItem(NOTIFY_PENDING); sessionStorage.removeItem(SMS_PENDING);
  } catch { /* ignore */ }
  if (!pending) return;
  try {
    await api("PUT", "/v1/notifications", pending);
    announce("Notifications are on.");
  } catch {
    announce("Your notification choice wasn't saved. You can turn notifications on in My profile settings.");
  }
}


/** My profile settings (#/settings): email, password, notifications, delete the account. */
function renderSettings(main) {
  const session = COGNITO ? readSession() : null;
  // Shared demo accounts: their password is public, so email, password and deletion stay locked for everyone.
  const demo = Boolean(session && DEMO_EMAIL.test(session.email));
  main.append(
    band("head", h("div", { class: "page-head" },
      h("h1", null, "My profile settings"),
      h("p", null, "Your account, your notifications, and deleting your account. Your investor profile is in \u201CSet up my profile\u201D."))),
    band("body", h("div", { class: "settings stack" },
      demo ? demoAccountNote(session) : (COGNITO ? emailSection(session) : localTestNote()),
      COGNITO && !demo ? passwordSection() : null,
      notifySection(session),
      demo ? null : deleteSection(session))));
}

function demoAccountNote(session) {
  return h("section", { class: "panel stack-sm" }, h("h2", { class: "section-title" }, "Account"),
    h("p", { class: "small" }, "Signed in as ", h("strong", null, session.email)),
    h("p", { class: "small muted" }, "This is a shared demo account: its email and password can't be changed and it "
      + "can't be deleted, so the demo keeps working for everyone. Create your own account to use these settings."));
}

function localTestNote() {
  return h("section", { class: "panel stack-sm" }, h("h2", { class: "section-title" }, "Account"),
    h("p", { class: "small muted" }, `Local test mode: there is no sign-in account to change. You are ${actorId()}.`));
}

/** One form of the settings page: its own fields, error box and status line. */
function settingsForm(prefix, title, intro, fields, submitLabel, onSubmit) {
  const status = h("p", { class: "small", role: "status" });
  const formError = h("div");
  const submit = h("button", { class: "btn btn--secondary", type: "submit" }, submitLabel);
  const form = h("form", { class: "stack-sm", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    status.textContent = ""; formError.replaceChildren();
    submit.disabled = true; submit.setAttribute("aria-busy", "true");
    try { await onSubmit({ status, formError, submit }); } finally { submit.disabled = false; submit.removeAttribute("aria-busy"); }
  } }, ...fields.map((f) => f.el), formError, h("div", null, submit), status);
  return { submit, status, formError, form, el: h("section", { class: "panel stack-sm", "aria-labelledby": `${prefix}-title` },
    h("h2", { id: `${prefix}-title`, class: "section-title" }, title), intro ? h("p", { class: "small" }, intro) : null, form) };
}

function settingsField(prefix, name, label, attrs, help = "") {
  const id = `${prefix}-${name}`;
  const input = h("input", { class: "input", id, name, "aria-describedby": `${id}-help ${id}-error`, ...attrs });
  const error = h("p", { class: "field__error", id: `${id}-error` });
  input.addEventListener("input", () => { error.replaceChildren(); input.removeAttribute("aria-invalid"); });
  return { input, error, el: h("div", { class: "field" }, h("label", { class: "field__label", for: id }, label), input,
    help ? h("p", { class: "field__help", id: `${id}-help` }, help) : null, error) };
}

function fieldProblem(f, message) {
  f.error.replaceChildren(icon("alert", 14), h("span", null, message));
  f.input.setAttribute("aria-invalid", "true");
  f.input.focus();
}

function alertLine(message) {
  return h("div", { class: "alert", role: "alert", tabindex: "-1" }, icon("alert", 18), h("p", { class: "alert__title" }, message));
}

function settingsCognitoMessage(err, wrongPassword = "The current password is incorrect.") {
  if (err instanceof CognitoError && err.type === "NotAuthorizedException" && !/token/i.test(err.message)) return wrongPassword;
  if (err instanceof CognitoError && err.type === "NotAuthorizedException") return "Your session has expired. Sign out and sign in again.";
  return err instanceof CognitoError ? cognitoMessage(err) : "Something went wrong. Try again.";
}

function emailSection(session) {
  const box = h("div");
  const current = () => (readSession() || {}).email || (session && session.email) || "";
  function drawAsk() {
    const email = settingsField("set-email", "new", "New email", { type: "email", autocomplete: "email", inputmode: "email" },
      "We send a 6-digit code to the new address to confirm it.");
    const f = settingsForm("set-email", "Email", `You sign in with ${current()}.`, [email], "Change email", async ({ formError }) => {
      const value = email.input.value.trim();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return fieldProblem(email, "Enter a valid email address, like name@example.com.");
      if (value.toLowerCase() === current().toLowerCase()) return fieldProblem(email, "That is already your email.");
      const token = await accessToken();
      if (!token) { formError.replaceChildren(alertLine("Your session has expired. Sign out and sign in again.")); return; }
      try {
        await cognito("UpdateUserAttributes", { AccessToken: token, UserAttributes: [{ Name: "email", Value: value }] }, { withClient: false });
        drawCode(value);
      } catch (err) { formError.replaceChildren(alertLine(settingsCognitoMessage(err))); }
    });
    box.replaceChildren(f.el);
  }
  function drawCode(value) {
    const code = settingsField("set-email", "code", "Code", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", class: "input code-input" },
      `Sent to ${value}.`);
    const f = settingsForm("set-email", "Email", `Enter the code we sent to ${value}.`, [code], "Confirm new email", async ({ formError }) => {
      if (!/^\d{6}$/.test(code.input.value.trim())) return fieldProblem(code, "Enter the 6 digits of the code.");
      const token = await accessToken();
      if (!token) { formError.replaceChildren(alertLine("Your session has expired. Sign out and sign in again.")); return; }
      try {
        await cognito("VerifyUserAttribute", { AccessToken: token, AttributeName: "email", Code: code.input.value.trim() }, { withClient: false });
        const s = readSession();
        if (s) store.set(SESSION_KEY, { ...s, email: value });
        await refreshSession();             // a new ID token carries the new address
        setWho();
        announce("Your email was changed.");
        drawAsk();
        box.querySelector('[role="status"]').textContent = `Done: you now sign in with ${value}.`;
      } catch (err) { formError.replaceChildren(alertLine(settingsCognitoMessage(err))); }
    });
    box.replaceChildren(f.el);
    code.input.focus();
  }
  drawAsk();
  return box;
}

function passwordSection() {
  const old = settingsField("set-pw", "current", "Current password", { type: "password", autocomplete: "current-password" });
  const next = settingsField("set-pw", "new", "New password", { type: "password", autocomplete: "new-password" }, PASSWORD_RULE);
  const f = settingsForm("set-pw", "Password", null, [old, next], "Change password", async ({ status, formError }) => {
    if (!old.input.value) return fieldProblem(old, "Enter your current password.");
    const problem = passwordProblem(next.input.value);
    if (problem) return fieldProblem(next, problem);
    const token = await accessToken();
    if (!token) { formError.replaceChildren(alertLine("Your session has expired. Sign out and sign in again.")); return; }
    try {
      await cognito("ChangePassword", { AccessToken: token, PreviousPassword: old.input.value, ProposedPassword: next.input.value }, { withClient: false });
      old.input.value = ""; next.input.value = "";
      status.textContent = "Your password was changed.";
      announce("Password changed.");
    } catch (err) { formError.replaceChildren(alertLine(settingsCognitoMessage(err))); }
  });
  return f.el;
}

function notifySection(session) {
  const choice = notifyChoiceFields("set-notify");
  const f = settingsForm("set-notify", "Notifications", null, [choice], "Save notifications", async ({ status, formError }) => {
    const { prefs, problem } = choice.read();
    if (problem) return fieldProblem(problem.field, problem.message);
    try {
      const r = (await api("PUT", "/v1/notifications", prefs)).data;
      choice.set(r);
      status.textContent = !r.opt_in ? "Notifications are off. Your phone number was deleted."
        : r.channel === "sms" ? `Saved: alerts by text message to ${r.phone_masked}.` : `Saved: alerts by email to ${r.email}.`;
      announce("Notifications saved.");
    } catch (err) {
      const detail = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join(" ") : null;
      formError.replaceChildren(detail ? alertLine(detail) : alertBox(err, "your notifications", null));
    }
  });
  api("GET", "/v1/notifications").then(({ data }) => choice.set(data))
    .catch(() => { f.status.textContent = "Your notification choice couldn't be loaded."; });
  return f.el;
}

function deleteSection(session) {
  const confirmWord = "DELETE";
  const typed = settingsField("set-delete", "confirm", `To confirm, type ${confirmWord}`, { type: "text", autocomplete: "off", spellcheck: "false" });
  const f = settingsForm("set-delete", "Delete my account",
    "This deletes your sign-in account and everything AlphaForge keeps about you: your profile, your portfolio, your analyses and your notification choice. It can't be undone.",
    [typed], "Delete my account", async ({ formError }) => {
      if (typed.input.value.trim() !== confirmWord) return fieldProblem(typed, `Type ${confirmWord} in capital letters.`);
      if (!window.confirm("Delete your account and all its data? This can't be undone.")) return;
      const token = COGNITO ? await accessToken() : null;
      if (COGNITO && !token) { formError.replaceChildren(alertLine("Your session has expired. Sign out and sign in again.")); return; }
      try {
        await api("DELETE", "/v1/account");
        if (COGNITO) await cognito("DeleteUser", { AccessToken: token }, { withClient: false });
      } catch (err) {
        formError.replaceChildren(err instanceof CognitoError
          ? alertLine(`Your data was deleted, but the sign-in account wasn't: ${settingsCognitoMessage(err)} Try again.`)
          : alertBox(err, "your account", null));
        return;
      }
      state.profile = undefined; state.thread = []; state.dirty = false;
      if (COGNITO) {
        try { localStorage.removeItem(SESSION_KEY); sessionStorage.setItem("af.signin.msg", "Your account and its data were deleted."); } catch { /* ignore */ }
        setWho();
        window.location.hash = "#/signin";
      } else {
        try { localStorage.removeItem("af.actor"); } catch { /* ignore */ }
        setWho();
        window.location.hash = "#/profile";
      }
      announce("Account deleted.");
    });
  f.submit.className = "btn btn--danger";
  f.el.classList.add("panel--danger");
  return f.el;
}

/* ---------------- The user menu (round user button): Set up my profile, My profile settings ---------------- */

function userMenuParts() {
  return { wrap: document.getElementById("usermenu"), btn: document.getElementById("usermenu-btn"), list: document.getElementById("usermenu-list") };
}
function closeUserMenu(focusButton = false) {
  const { btn, list } = userMenuParts();
  if (!btn || list.hidden) return;
  list.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  if (focusButton) btn.focus();
}
function openUserMenu(focusFirst = true) {
  const { btn, list } = userMenuParts();
  list.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  if (focusFirst) list.querySelector('[role="menuitem"]').focus();
}
function initUserMenu() {
  const { wrap, btn, list } = userMenuParts();
  if (!btn) return;
  btn.replaceChildren(icon("user", 20), h("span", { class: "visually-hidden" }, "Your account"));
  btn.addEventListener("click", () => (list.hidden ? openUserMenu(false) : closeUserMenu()));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") { e.preventDefault(); openUserMenu(true); }
  });
  list.addEventListener("keydown", (e) => {
    const items = [...list.querySelectorAll('[role="menuitem"]')];
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeUserMenu(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Home") { e.preventDefault(); items[0].focus(); }
    else if (e.key === "End") { e.preventDefault(); items[items.length - 1].focus(); }
    else if (e.key === "Tab") closeUserMenu();
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) closeUserMenu(); });
}

/* ---------------- Sign in (Amazon Cognito) ---------------- */

const PASSWORD_RULE = "At least 10 characters, with upper case, lower case and a number.";
function passwordProblem(pw) {
  if (pw.length < 10) return "Use at least 10 characters.";
  if (!/[a-z]/.test(pw) || !/[A-Z]/.test(pw) || !/\d/.test(pw)) return "Mix upper case, lower case and at least one number.";
  return null;
}

function renderSignIn(main) {
  if (!COGNITO) { if (DEV) window.location.hash = HOME; return; }
  const flow = { mode: "signin", email: "", password: "" }; // the password stays in memory only, for auto sign-in
  let notice = "";
  try { notice = sessionStorage.getItem("af.signin.msg") || ""; sessionStorage.removeItem("af.signin.msg"); } catch { /* ignore */ }
  const box = h("div", { class: "auth" });
  main.append(
    h("div", { class: "band band--hero" }, h("div", { class: "container" },
      h("h1", null, "Investment questions, explained."),
      h("p", null, "Ask about your portfolio and see exactly what AlphaForge analyses, and why. Simulation only."))),
    band("body", box));

  const fillBox = (...parts) => box.replaceChildren(...parts.filter(Boolean)); // replaceChildren would print "null"

  async function finish() {
    let next = HOME;
    try { next = sessionStorage.getItem("af.next") || HOME; sessionStorage.removeItem("af.next"); } catch { /* ignore */ }
    setWho();
    announce("Signed in.");
    await flushNotifyChoice();
    if (window.location.hash === next) route(); else window.location.hash = next;
  }

  function field(name, label, attrs, help = "") {
    const input = h("input", { class: "input", id: `auth-${name}`, name, "aria-describedby": `auth-${name}-help auth-${name}-error`, ...attrs });
    let control = input;
    if (attrs.type === "password") {
      const toggle = h("button", { class: "affix__btn", type: "button", "aria-controls": `auth-${name}`, "aria-pressed": "false" }, "Show");
      toggle.addEventListener("click", () => {
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        toggle.textContent = show ? "Hide" : "Show";
        toggle.setAttribute("aria-pressed", String(show));
      });
      control = h("div", { class: "affix affix--btn" }, input, toggle);
    }
    const error = h("p", { class: "field__error", id: `auth-${name}-error` });
    input.addEventListener("input", () => { error.replaceChildren(); input.removeAttribute("aria-invalid"); });
    return { input, error, el: h("div", { class: "field" },
      h("label", { class: "field__label", for: `auth-${name}` }, label), control,
      help ? h("p", { class: "field__help", id: `auth-${name}-help` }, help) : null, error) };
  }
  function fail(f, msg) {
    f.error.replaceChildren(icon("alert", 14), h("span", null, msg));
    f.input.setAttribute("aria-invalid", "true");
    f.input.focus();
  }
  function busy(btn, label) {
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    btn.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), label);
    return () => { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.replaceChildren(btn.dataset.label); };
  }
  function primary(label) { return h("button", { class: "btn btn--primary", type: "submit", "data-label": label }, label); }
  function go(mode) { flow.mode = mode; notice = ""; draw(); }
  const isEmail = (v) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);

  function draw() {
    const status = notice ? h("p", { class: "notice", role: "status" }, notice) : null;
    const formError = h("div");
    const showError = (err) => {
      const msg = err instanceof CognitoError ? cognitoMessage(err) : "Something went wrong. Try again.";
      formError.replaceChildren(h("div", { class: "alert", role: "alert" }, icon("alert", 18), h("p", { class: "alert__title" }, msg)));
    };

    if (flow.mode === "signin") {
      const email = field("email", "Email", { type: "email", autocomplete: "username", inputmode: "email", value: flow.email });
      const pw = field("password", "Password", { type: "password", autocomplete: "current-password" });
      const submit = primary("Sign in");
      const demos = (CONFIG.demoAccounts || []).map((a) => {
        const btn = h("button", { class: "demo-account", type: "button" },
          h("span", { class: "demo-account__name" }, `Sign in as ${a.name}`),
          h("span", { class: "demo-account__desc" }, a.summary));
        btn.addEventListener("click", async () => {
          for (const b of box.querySelectorAll(".demo-account")) b.disabled = true;
          btn.querySelector(".demo-account__name").prepend(h("span", { class: "spinner", "aria-hidden": "true" }));
          try { await signIn(a.email, a.password); await finish(); }
          catch (err) { showError(err); for (const b of box.querySelectorAll(".demo-account")) b.disabled = false; btn.querySelector(".spinner")?.remove(); }
        });
        return btn;
      });
      fillBox(
        h("div", { class: "page-head" }, h("h2", null, "Sign in to AlphaForge"),
          h("p", null, "Use your account, or explore with a demo investor. Simulation only: nothing here is investment advice.")),
        status,
        h("form", { class: "auth-form", novalidate: true, onsubmit: async (e) => {
          e.preventDefault();
          formError.replaceChildren();
          flow.email = email.input.value.trim();
          if (!isEmail(flow.email)) return fail(email, "Enter the email address of your account.");
          if (!pw.input.value) return fail(pw, "Enter your password.");
          const done = busy(submit, "Signing in");
          try { await signIn(flow.email, pw.input.value); await finish(); }
          catch (err) {
            done();
            if (err.type === "UserNotConfirmedException") { flow.password = pw.input.value; notice = "Your email isn't confirmed yet. Enter the code we sent you, or ask for a new one."; flow.mode = "confirm"; draw(); return; }
            showError(err);
          }
        } }, email.el, pw.el, formError, submit),
        h("div", { class: "auth-links" },
          h("button", { class: "link-btn", type: "button", onclick: () => go("signup") }, "Create an account"),
          h("button", { class: "link-btn", type: "button", onclick: () => go("forgot") }, "Forgot password?")),
        demos.length ? h("p", { class: "divider" }, "or try a demo investor") : null,
        demos.length ? h("section", { class: "stack-sm", "aria-labelledby": "demo-title" },
          h("h2", { id: "demo-title", class: "section-title" }, "Try a demo account"),
          h("p", { class: "small muted" }, "Each one comes with the profile of an investor from the challenge brief. Demo accounts are shared."),
          h("div", { class: "demo-accounts" }, demos)) : null);
    }

    if (flow.mode === "signup") {
      const email = field("email", "Email", { type: "email", autocomplete: "email", inputmode: "email", value: flow.email }, "We send a 6-digit code to confirm it.");
      const pw = field("password", "Password", { type: "password", autocomplete: "new-password" }, PASSWORD_RULE);
      const inform = notifyChoiceFields("auth");
      const submit = primary("Create account");
      fillBox(
        h("div", { class: "page-head" }, h("h2", null, "Create your account"),
          h("p", null, "Your profile and questions are kept under this account.")),
        h("form", { class: "auth-form", novalidate: true, onsubmit: async (e) => {
          e.preventDefault();
          formError.replaceChildren();
          flow.email = email.input.value.trim();
          if (!isEmail(flow.email)) return fail(email, "Enter a valid email address, like name@example.com.");
          const problem = passwordProblem(pw.input.value);
          if (problem) return fail(pw, problem);
          const pick = inform.read();
          if (pick.problem) return fail(pick.problem.field, pick.problem.message);
          const done = busy(submit, "Creating");
          try {
            await cognito("SignUp", { Username: flow.email, Password: pw.input.value, UserAttributes: [{ Name: "email", Value: flow.email }] });
            try {
              if (pick.prefs.opt_in) sessionStorage.setItem(NOTIFY_PENDING, JSON.stringify(pick.prefs));
              else sessionStorage.removeItem(NOTIFY_PENDING);
            } catch { /* ignore: the choice can be made in My profile settings */ }
            flow.password = pw.input.value;
            notice = "";
            flow.mode = "confirm";
            draw();
          } catch (err) { done(); showError(err); }
        } }, email.el, pw.el, inform.el, formError, submit),
        h("div", { class: "auth-links" }, h("button", { class: "link-btn", type: "button", onclick: () => go("signin") }, "I already have an account")));
    }

    if (flow.mode === "confirm") {
      const code = field("code", "Code", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", class: "input code-input" });
      const submit = primary("Confirm");
      const resend = h("button", { class: "link-btn", type: "button" }, "Send a new code");
      resend.addEventListener("click", async () => {
        resend.disabled = true;
        try { await cognito("ResendConfirmationCode", { Username: flow.email }); notice = `A new code is on its way to ${flow.email}.`; draw(); }
        catch (err) { resend.disabled = false; showError(err); }
      });
      fillBox(
        h("div", { class: "page-head" }, h("h2", null, "Check your email"),
          h("p", null, `We sent a 6-digit code to ${flow.email}. It can take a minute to arrive.`)),
        status,
        h("form", { class: "auth-form", novalidate: true, onsubmit: async (e) => {
          e.preventDefault();
          formError.replaceChildren();
          const value = code.input.value.replace(/\s/g, "");
          if (!/^\d{6}$/.test(value)) return fail(code, "Enter the 6 digits from the email.");
          const done = busy(submit, "Confirming");
          try {
            await cognito("ConfirmSignUp", { Username: flow.email, ConfirmationCode: value });
            if (flow.password) { await signIn(flow.email, flow.password); flow.password = ""; await finish(); return; }
            notice = "Your email is confirmed. Sign in to continue."; flow.mode = "signin"; draw();
          } catch (err) { done(); showError(err); }
        } }, code.el, formError, submit),
        h("div", { class: "auth-links" }, resend, h("button", { class: "link-btn", type: "button", onclick: () => go("signup") }, "Use another email")));
    }

    if (flow.mode === "forgot") {
      const email = field("email", "Email", { type: "email", autocomplete: "username", inputmode: "email", value: flow.email });
      const submit = primary("Send a code");
      fillBox(
        h("div", { class: "page-head" }, h("h2", null, "Reset your password"),
          h("p", null, "Enter the email of your account. We send a code to choose a new password.")),
        h("form", { class: "auth-form", novalidate: true, onsubmit: async (e) => {
          e.preventDefault();
          formError.replaceChildren();
          flow.email = email.input.value.trim();
          if (!isEmail(flow.email)) return fail(email, "Enter the email address of your account.");
          const done = busy(submit, "Sending");
          try {
            await cognito("ForgotPassword", { Username: flow.email });
            notice = `If an account uses ${flow.email}, a code is on its way.`; // never reveals whether it exists
            flow.mode = "reset"; draw();
          } catch (err) { done(); showError(err); }
        } }, email.el, formError, submit),
        h("div", { class: "auth-links" }, h("button", { class: "link-btn", type: "button", onclick: () => go("signin") }, "Back to sign in")));
    }

    if (flow.mode === "reset") {
      const code = field("code", "Code", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", class: "input code-input" });
      const pw = field("password", "New password", { type: "password", autocomplete: "new-password" }, PASSWORD_RULE);
      const submit = primary("Set new password");
      fillBox(
        h("div", { class: "page-head" }, h("h2", null, "Choose a new password"), h("p", null, `For ${flow.email}.`)),
        status,
        h("form", { class: "auth-form", novalidate: true, onsubmit: async (e) => {
          e.preventDefault();
          formError.replaceChildren();
          const value = code.input.value.replace(/\s/g, "");
          if (!/^\d{6}$/.test(value)) return fail(code, "Enter the 6 digits from the email.");
          const problem = passwordProblem(pw.input.value);
          if (problem) return fail(pw, problem);
          const done = busy(submit, "Saving");
          try {
            await cognito("ConfirmForgotPassword", { Username: flow.email, ConfirmationCode: value, Password: pw.input.value });
            await signIn(flow.email, pw.input.value);
            await finish();
          } catch (err) { done(); showError(err); }
        } }, code.el, pw.el, formError, submit),
        h("div", { class: "auth-links" }, h("button", { class: "link-btn", type: "button", onclick: () => go("forgot") }, "Send a new code")));
    }

    const first = box.querySelector("h2");
    if (first) { first.setAttribute("tabindex", "-1"); first.focus({ preventScroll: true }); }
  }
  draw();
}

/* ---------------- Shell: mode toggle, user, start ---------------- */

function currentMode() {
  const set = document.documentElement.dataset.mode;
  if (set) return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function drawModeButton() {
  const btn = document.getElementById("mode-toggle");
  const dark = currentMode() === "dark";
  btn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  btn.replaceChildren(icon(dark ? "sun" : "moon", 18), h("span", { id: "mode-label" }, dark ? "Light mode" : "Dark mode"));
}
function setWho() {
  const who = document.getElementById("who");
  const out = document.getElementById("sign-out");
  if (COGNITO) {
    const s = readSession();
    who.textContent = s ? s.email : "";
    out.hidden = !s;
  } else {
    who.textContent = DEV ? `Local test user ${actorId()}` : "";
    out.hidden = true;
  }
}
document.getElementById("sign-out").addEventListener("click", () => {
  if (state.dirty && !window.confirm("Your profile has unsaved changes. Sign out without saving?")) return;
  signOut("You're signed out.");
});

document.getElementById("mode-toggle").addEventListener("click", () => {
  const next = currentMode() === "dark" ? "light" : "dark";
  document.documentElement.dataset.mode = next;
  try { localStorage.setItem("af.mode", next); } catch { /* ignore */ } // raw string: read before first paint in index.html
  drawModeButton();
  announce(`${next === "dark" ? "Dark" : "Light"} mode.`);
});
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });
window.addEventListener("hashchange", route);

drawModeButton();
initUserMenu();
setWho();
route();
