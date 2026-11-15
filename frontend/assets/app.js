// AlphaForge frontend (box ① of docs/architecture.mmd): plain ES module, no framework, no build step.
// Talks only to the box ② API: POST/GET /v1/runs, GET/PUT /v1/profile, GET /v1/personas.
// Every piece of user or server text goes into the page as a text node (never as HTML).

const API_BASE = window.AF_API_BASE || ""; // same origin by default (the API serves this folder locally)
const POLL_EVERY_MS = 5000;
const POLL_MAX = 6;

/* ---------------- Vocabulary: one plain name per thing, used everywhere ---------------- */

const AGENTS = {
  risk: { name: "Portfolio risk", desc: "Volatility, worst drop and concentration of your holdings." },
  macro_scenarios: { name: "Crash scenarios", desc: "How the portfolio would have held up in a past crisis." },
  esg: { name: "ESG and controversies", desc: "Environmental, social and governance screens, and known controversies." },
  fundamentals: { name: "Company fundamentals", desc: "Facts from company filings, such as annual reports." },
  news: { name: "News", desc: "What the news says about the assets you mention." },
  allocation: { name: "Allocation proposal", desc: "A suggested split of your money that fits your profile." },
};
const AGENT_ORDER = ["risk", "macro_scenarios", "esg", "fundamentals", "news", "allocation"];

const TASKS = {
  risk_review: "risk review",
  scenario_analysis: "crash scenario",
  allocation_proposal: "allocation proposal",
  esg_screen: "ESG screen",
  fundamentals_review: "fundamentals review",
  news_review: "news review",
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
const LIMITS = [
  { key: "crypto", exclusion: { type: "asset_class", value: "crypto" }, title: "No crypto-assets",
    desc: "Bitcoin, Ether and similar are never part of an answer." },
  { key: "controversy", exclusion: { type: "controversy", min_severity: "major" }, title: "Avoid major controversies",
    desc: "Companies with serious environmental or social controversies are flagged." },
  { key: "fossil", exclusion: { type: "esg_activity", value: "fossil_fuels" }, title: "No fossil-fuel companies",
    desc: "Companies earning a large share of revenue from oil, gas or coal are flagged." },
];
const SCENARIOS = {
  "2022": "2022, when rates rose and stocks and bonds fell together",
  "2022_H1": "the first half of 2022",
};
const EXAMPLE_QUESTIONS = [
  "Is my portfolio too risky compared to SPY?",
  "What would happen to my portfolio in a crash like 2022?",
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
  minus: "M6 12h12",
  alert: "M12 8v5m0 3.5v.01M10.3 3.9 2.6 17.3A2 2 0 0 0 4.3 20.3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  moon: "M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.66-5.66 1.41-1.41M4.93 19.07l1.41-1.41m11.32 0 1.41 1.41M4.93 4.93l1.41 1.41M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
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

/* ---------------- Identity (development mode until the Cognito login exists) ---------------- */

function actorId() {
  let id = store.get("af.actor");
  if (!id) {
    id = `guest-${Math.random().toString(36).slice(2, 7)}`;
    store.set("af.actor", id);
  }
  return id;
}

/* ---------------- API client ---------------- */

class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers: { "Content-Type": "application/json", "X-Actor-Id": actorId() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new ApiError(res.status, data && data.detail);
  return { status: res.status, data };
}

function errorText(err, what) {
  if (err.status === 0) return { title: "AlphaForge isn't responding", body: "The app can't reach its server. Check that the API is running, then try again." };
  if (err.status === 401) return { title: "You're not signed in", body: "Sign in again, then try again." };
  if (err.status === 502) return { title: `AlphaForge couldn't read ${what}`, body: "The language model didn't give a usable answer this time. Your text is kept: try again." };
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
  personas: null,
  thread: [],
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
  if (r.includes("required by task")) out.push(`Needed for a ${TASKS[task] || "question like yours"}.`);
  if (r.includes("stress_tests") || r.includes("parameters.scenarios")) out.push("You asked about a crash or a stress test.");
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
  return w;
}

function settingsOf(agent) {
  const p = agent.parameters || {};
  const out = [];
  if (p.benchmark) out.push(`Compared with ${p.benchmark}.`);
  if (Array.isArray(p.scenarios) && p.scenarios.length) {
    out.push(`Scenario: ${p.scenarios.map((s) => SCENARIOS[s] || s).join(", ")}.`);
  }
  return out.join(" ");
}

/* ---------------- Router ---------------- */

const routes = [
  { re: /^#\/ask$/, nav: "ask", title: "Ask", view: renderAsk },
  { re: /^#\/profile$/, nav: "profile", title: "Your profile", view: renderProfile },
  { re: /^#\/runs\/([\w-]+)$/, nav: "ask", title: "Analysis plan", view: renderRun },
];

let currentHash = null;
function route() {
  const hash = window.location.hash || "#/ask";
  if (state.dirty && currentHash === "#/profile" && hash !== "#/profile") {
    if (!window.confirm("Your profile has unsaved changes. Leave without saving?")) {
      history.replaceState(null, "", currentHash);
      return;
    }
    state.dirty = false;
  }
  window.clearTimeout(state.pollTimer);
  const match = routes.find((r) => r.re.test(hash)) || routes[0];
  if (!routes.some((r) => r.re.test(hash))) history.replaceState(null, "", "#/ask");
  currentHash = window.location.hash || "#/ask";
  for (const link of document.querySelectorAll(".nav__link")) {
    if (link.dataset.route === match.nav) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${match.title}, AlphaForge`;
  const main = document.getElementById("main");
  main.replaceChildren();
  const params = hash.match(match.re) || [];
  match.view(main, ...params.slice(1));
  const h1 = main.querySelector("h1");
  if (h1 && document.activeElement !== document.body) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true }); }
  window.scrollTo(0, 0);
}

/* ---------------- Ask ---------------- */

function renderAsk(main) {
  const textarea = h("textarea", {
    class: "textarea", id: "question", name: "question", rows: "4", maxlength: "4000",
    "aria-describedby": "question-help question-error", autocomplete: "off",
  });
  const error = h("p", { class: "field__error", id: "question-error" });
  const submit = h("button", { class: "btn btn--primary", type: "submit" }, "Ask");
  const thread = h("div", { class: "thread", "aria-label": "Your questions and AlphaForge's replies" });

  const form = h("form", { class: "composer", novalidate: true, onsubmit: (e) => { e.preventDefault(); send(); } },
    h("div", { class: "field" },
      h("label", { class: "field__label", for: "question" }, "Your question"),
      textarea,
      h("p", { class: "field__help", id: "question-help" },
        "Mention amounts or percentages if you have them. Press Ctrl+Enter to send."),
      error),
    h("div", { class: "composer__actions" }, submit),
    h("div", { class: "stack-sm" },
      h("p", { class: "small muted", id: "examples-label" }, "Try an example. It fills the box; nothing is sent until you press Ask."),
      h("div", { class: "examples", role: "group", "aria-labelledby": "examples-label" },
        EXAMPLE_QUESTIONS.map((q) => h("button", {
          class: "example", type: "button",
          onclick: () => { textarea.value = q; clearError(); textarea.focus(); },
        }, q)))));

  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  });
  textarea.addEventListener("input", clearError);

  function clearError() { error.replaceChildren(); textarea.removeAttribute("aria-invalid"); }
  function showError(msg) {
    error.replaceChildren(icon("alert", 14), h("span", null, msg));
    textarea.setAttribute("aria-invalid", "true");
    textarea.focus();
  }

  async function send(text = textarea.value.trim(), profileForm = undefined, replaceTurn = null) {
    if (!text) { showError("Write a question first."); return; }
    if (text.length > 4000) { showError("Keep the question under 4,000 characters."); return; }
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    submit.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Asking");
    try {
      const body = { text };
      if (profileForm) body.profile_form = profileForm;
      const { data } = await api("POST", "/v1/runs", body);
      const turn = { text, reply: data };
      if (replaceTurn) state.thread[state.thread.indexOf(replaceTurn)] = turn; else state.thread.push(turn);
      if (data.kind === "run") rememberRun(data.run_id, text);
      if (!profileForm) textarea.value = "";
      state.profile = undefined; // the intake may have saved or changed the profile
      drawThread();
      drawSide();
      announce(data.kind === "run" ? "Analysis planned." : data.kind === "question" ? "AlphaForge needs one detail." : "AlphaForge replied.");
    } catch (err) {
      const turn = { text, error: err, retry: () => send(text, profileForm, turn) };
      if (replaceTurn) state.thread[state.thread.indexOf(replaceTurn)] = turn; else state.thread.push(turn);
      drawThread();
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.replaceChildren("Ask");
    }
  }

  function drawThread() {
    thread.replaceChildren(...state.thread.map((turn) => drawTurn(turn)));
    const last = thread.lastElementChild;
    if (last) last.scrollIntoView({ block: "nearest" });
  }

  function drawTurn(turn) {
    const you = h("p", { class: "turn__you" }, h("span", { class: "visually-hidden" }, "You asked: "), turn.text);
    if (turn.error) {
      return h("article", { class: "turn" }, you, alertBox(turn.error, "your question", turn.retry));
    }
    const r = turn.reply;
    const parts = [];
    if (r.kind === "question") parts.push(...drawQuestion(turn));
    if (r.kind === "message") {
      parts.push(h("p", { class: "turn__label" }, "AlphaForge"), h("p", null, r.message));
      if (/profile/i.test(r.message || "")) {
        parts.push(h("div", { class: "notice" },
          h("a", { class: "link-btn", href: "#/profile" }, "Set up your profile")));
      }
    }
    if (r.kind === "run") {
      const names = (r.enabled_agents || []).map((a) => (AGENTS[a] || { name: a }).name);
      parts.push(h("p", { class: "turn__label" }, "Analysis planned"));
      parts.push(h("p", null, names.length
        ? `AlphaForge will look at: ${names.join(", ")}.`
        : "Nothing to analyse: this request asks for something a simulation can't do. The answer will explain why."));
      parts.push(h("div", { class: "notice" }, h("a", { class: "link-btn", href: `#/runs/${r.run_id}` }, "See the plan")));
    }
    for (const change of r.toggle_changes || []) parts.push(drawToggleChange(change));
    if ((r.warnings || []).length) parts.push(h("ul", { class: "warning-list" }, r.warnings.map((w) => h("li", null, w))));
    return h("article", { class: "turn" }, you,
      h("div", { class: `turn__reply${r.kind === "run" ? " turn__reply--run" : ""}` }, parts));
  }

  function drawQuestion(turn) {
    const r = turn.reply;
    const missing = (r.missing_fields || []).filter((f) => f === "risk_band" || f === "experience");
    const out = [h("p", { class: "turn__label" }, "AlphaForge needs one detail"), h("p", null, r.question)];
    if (!missing.length) {
      out.push(h("div", { class: "notice" },
        h("button", { class: "link-btn", type: "button",
          onclick: () => { textarea.value = turn.text; textarea.focus(); } }, "Edit my question")));
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

  function drawToggleChange(change) {
    const name = (AGENTS[change.agent] || { name: change.agent }).name;
    const line = h("p", null, `${name} is now ${change.enabled ? "on" : "off"} in your profile, because you said “${change.quote}”.`);
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

  /* Side column: profile summary + recent analyses */
  const side = h("aside", { class: "stack", "aria-label": "Your profile and recent analyses" });
  async function drawSide() {
    side.replaceChildren(h("section", { class: "panel" }, skeleton(4)));
    let profile;
    try {
      profile = await loadProfile();
    } catch (err) {
      side.replaceChildren(alertBox(err, "your profile", drawSide), drawRecent());
      return;
    }
    side.replaceChildren(profile ? profileSummary(profile) : noProfile(), drawRecent());
  }

  main.append(
    h("div", { class: "page-head" },
      h("h1", null, "Ask about your portfolio"),
      h("p", null, "Write your question the way you would say it. AlphaForge works out what to analyse and shows you why.")),
    h("div", { class: "columns" }, h("section", { "aria-label": "Ask a question" }, form, thread), side));
  drawThread();
  drawSide();
}

function profileSummary(p) {
  const off = AGENT_ORDER.filter((a) => p.agent_toggles && p.agent_toggles[a] === false).map((a) => AGENTS[a].name);
  const limits = LIMITS.filter((l) => (p.exclusions || []).some((e) => e.type === l.exclusion.type)).map((l) => l.title);
  return h("section", { class: "panel stack-sm", "aria-labelledby": "summary-title" },
    h("h2", { id: "summary-title", class: "section-title" }, "Your profile"),
    h("dl", { class: "facts" },
      h("dt", null, "Risk level"), h("dd", null, titleOf(RISK, p.risk_band)),
      h("dt", null, "Horizon"), h("dd", null, horizonText(p.horizon_years)),
      h("dt", null, "Experience"), h("dd", null, titleOf(EXPERIENCE, p.experience)),
      p.capital ? [h("dt", null, "Capital"), h("dd", { class: "num" }, money(p.capital))] : null,
      h("dt", null, "Limits"), h("dd", null, limits.length ? limits.join(", ") : "None"),
      h("dt", null, "Analyses off"), h("dd", null, off.length ? off.join(", ") : "None")),
    h("a", { class: "link-btn", href: "#/profile" }, "Edit profile"));
}

function noProfile() {
  return h("section", { class: "panel empty", "aria-labelledby": "summary-title" },
    h("h2", { id: "summary-title", class: "section-title" }, "No profile yet"),
    h("p", { class: "lede" }, "AlphaForge needs your risk level and experience before it can analyse anything."),
    h("a", { class: "btn btn--secondary", href: "#/profile" }, "Set up your profile"));
}

function drawRecent() {
  const runs = recentRuns();
  return h("section", { class: "panel panel--plain", "aria-labelledby": "recent-title" },
    h("h2", { id: "recent-title", class: "section-title" }, "Recent analyses"),
    runs.length
      ? h("ul", { class: "plain-list" }, runs.map((r) => h("li", null, h("a", { href: `#/runs/${r.id}` },
        h("span", { class: "recent__q" }, r.q), h("span", { class: "small muted" }, timeText(r.at))))))
      : h("p", { class: "muted" }, "Your analyses will appear here once you ask a question."));
}

function skeleton(lines = 3) {
  return h("div", { class: "skeleton", "aria-hidden": "true" },
    h("div", { class: "skeleton__bar skeleton__bar--title" }),
    Array.from({ length: lines }, (_, i) => h("div", { class: `skeleton__bar${i === lines - 1 ? " skeleton__bar--short" : ""}` })));
}

/* ---------------- Profile ---------------- */

function renderProfile(main) {
  main.append(h("div", { class: "page-head" },
    h("h1", null, "Your investor profile"),
    h("p", null, "AlphaForge uses it to decide what to analyse and how to explain the results. Only your risk level and experience are required.")));
  const body = h("div", null, h("div", { class: "panel" }, skeleton(6)));
  main.append(body);

  Promise.all([loadProfile(true), loadPersonas()]).then(([profile]) => {
    body.replaceChildren(profileForm(profile));
  }).catch((err) => {
    body.replaceChildren(alertBox(err, "your profile", () => { body.replaceChildren(); renderProfile(main); }));
  });
}

async function loadPersonas() {
  if (state.personas) return state.personas;
  try { state.personas = (await api("GET", "/v1/personas")).data; } catch { state.personas = []; }
  return state.personas;
}

function profileForm(saved, initialStatus = null) {
  const form = h("form", { class: "stack", novalidate: true });
  const status = h("p", { class: "small muted", role: "status" }, initialStatus || (saved ? "" : "Not saved yet."));
  const errors = {};

  function radioGroup(name, legend, help, options) {
    return h("fieldset", { "aria-describedby": `${name}-help ${name}-error` },
      h("legend", null, legend, " ", h("span", { class: "optional" }, "(required)")),
      h("p", { class: "legend-help", id: `${name}-help` }, help),
      h("div", { class: "choices choices--grid" }, options.map((o) => h("label", { class: "choice" },
        h("input", { type: "radio", name, value: o.value, required: true, "aria-required": "true" }),
        h("span", { class: "choice__title" }, o.title),
        h("span", { class: "choice__desc" }, o.desc)))),
      errors[name] = h("p", { class: "field__error", id: `${name}-error` }));
  }
  function textField(name, label, { help, type = "text", inputmode, suffix, optional = true, attrs = {} } = {}) {
    const input = h("input", { class: "input", id: name, name, type, inputmode, autocomplete: "off",
      "aria-describedby": `${name}-help ${name}-error`, ...attrs });
    return h("div", { class: "field" },
      h("label", { class: "field__label", for: name }, label, optional ? [" ", h("span", { class: "optional" }, "(optional)")] : null),
      suffix ? h("div", { class: "affix" }, input, h("span", { class: "affix__text", "aria-hidden": "true" }, suffix)) : input,
      h("p", { class: "field__help", id: `${name}-help` }, help || ""),
      errors[name] = h("p", { class: "field__error", id: `${name}-error` }));
  }

  const personas = state.personas || [];
  const examples = personas.length ? h("section", { class: "panel stack-sm", "aria-labelledby": "examples-title" },
    h("h2", { id: "examples-title", class: "section-title" }, "Start from an example"),
    h("p", { class: "lede small" }, "The three investors from the challenge brief. Picking one fills the form; nothing is saved until you save."),
    h("div", { class: "examples" }, personas.map((p) => h("button", { class: "example", type: "button",
      onclick: () => { fill(p.profile_form); markDirty(); status.textContent = `Filled with ${p.name}'s profile. Review it, then save.`; announce(status.textContent); } },
      `${p.name}: ${p.summary}`)))) : null;

  const toggleRows = AGENT_ORDER.map((a) => {
    const quote = saved && saved.agent_toggle_reasons ? saved.agent_toggle_reasons[a] : null;
    return h("div", { class: "switch-row" },
      h("div", { class: "switch-row__text" },
        h("label", { for: `toggle-${a}`, class: "choice__title" }, AGENTS[a].name),
        h("span", { class: "small muted" }, AGENTS[a].desc),
        quote ? h("span", { class: "small" }, `Changed from chat: “${quote}”`) : null),
      h("input", { class: "switch", type: "checkbox", role: "switch", id: `toggle-${a}`, name: `toggle-${a}` }));
  });

  form.append(
    radioGroup("risk_band", "Risk level", "How much ups and downs you can live with.", RISK),
    radioGroup("experience", "Experience", "This changes how answers are explained, not what is analysed.", EXPERIENCE),
    h("fieldset", null,
      h("legend", null, "Investment horizon ", h("span", { class: "optional" }, "(optional)")),
      h("p", { class: "legend-help" }, "When you expect to need the money. Leave the second box empty for “or more”."),
      h("div", { class: "input-row" },
        textField("horizon_min", "From", { type: "number", inputmode: "numeric", suffix: "years", optional: false, attrs: { min: "0", max: "60", step: "1" } }),
        textField("horizon_max", "To", { type: "number", inputmode: "numeric", suffix: "years", optional: false, attrs: { min: "0", max: "60", step: "1" } }))),
    h("fieldset", null,
      h("legend", null, "Money"),
      h("p", { class: "legend-help" }, "Simulated amounts only. Nothing is invested."),
      h("div", { class: "input-row" },
        textField("capital", "Capital", { type: "number", inputmode: "numeric", suffix: "CAD", help: "Example: 150000", attrs: { min: "1", step: "1000" } }),
        textField("loss_capacity", "Largest loss you could accept", { type: "number", inputmode: "decimal", suffix: "%", help: "Example: 15", attrs: { min: "0", max: "100", step: "1" } }))),
    textField("objective", "Your goal", { help: "Example: grow my savings for a house in ten years." }),
    h("div", { class: "field" },
      h("label", { class: "field__label", for: "preferences" }, "Interests ", h("span", { class: "optional" }, "(optional)")),
      h("textarea", { class: "textarea", id: "preferences", name: "preferences", rows: "3", "aria-describedby": "preferences-help" }),
      h("p", { class: "field__help", id: "preferences-help" }, "One per line. Mentioning ESG turns on the ESG check for every question.")),
    h("fieldset", null,
      h("legend", null, "Hard limits ", h("span", { class: "optional" }, "(optional)")),
      h("p", { class: "legend-help" }, "Rules AlphaForge always checks, whatever the question."),
      h("div", { class: "choices" }, LIMITS.map((l) => h("label", { class: "choice" },
        h("input", { type: "checkbox", name: `limit-${l.key}` }),
        h("span", { class: "choice__title" }, l.title),
        h("span", { class: "choice__desc" }, l.desc))))),
    h("fieldset", null,
      h("legend", null, "Analyses"),
      h("p", { class: "legend-help" }, "Turn off what you don't want. You can also say it in a question, like “I don't care about the news”. If a question can't be answered without an analysis, it still runs and the answer says why."),
      h("div", { class: "panel panel--plain" }, toggleRows)),
  );

  const saveBtn = h("button", { class: "btn btn--primary", type: "submit" }, "Save profile");
  const resetBtn = h("button", { class: "btn btn--quiet", type: "button", onclick: () => { fill(saved || {}); state.dirty = false; status.textContent = "Changes discarded."; } }, "Discard changes");
  const topError = h("div");
  form.append(topError, h("div", { class: "form-actions" }, saveBtn, resetBtn, status));

  const account = h("section", { class: "stack-sm", "aria-labelledby": "account-title" },
    h("h2", { id: "account-title", class: "section-title" }, "Demo user"),
    h("p", { class: "small muted" }, `You are ${actorId()}. Until sign-in is added, this name is kept in this browser only.`),
    h("div", null, h("button", { class: "btn btn--secondary", type: "button", onclick: () => {
      if (!window.confirm("Start over as a new demo user? Your current profile stays saved under the old name.")) return;
      try { localStorage.removeItem("af.actor"); } catch { /* ignore */ }
      state.profile = undefined; state.thread = []; state.dirty = false;
      setWho(); window.location.hash = "#/profile"; route();
    } }, "Start over as a new user")));

  /* fill + read */
  function fill(p) {
    const set = (name, v) => { const el = form.elements.namedItem(name); if (el) el.value = v ?? ""; };
    for (const el of form.querySelectorAll('input[type="radio"]')) el.checked = (p[el.name] === el.value);
    set("horizon_min", p.horizon_years ? p.horizon_years.min : "");
    set("horizon_max", p.horizon_years ? p.horizon_years.max : "");
    set("capital", p.capital ?? "");
    set("loss_capacity", p.loss_capacity == null ? "" : Math.round(p.loss_capacity * 100));
    set("objective", p.objective || "");
    set("preferences", (p.preferences || []).join("\n"));
    for (const l of LIMITS) form.elements.namedItem(`limit-${l.key}`).checked = (p.exclusions || []).some((e) => e.type === l.exclusion.type);
    for (const a of AGENT_ORDER) form.elements.namedItem(`toggle-${a}`).checked = !p.agent_toggles || p.agent_toggles[a] !== false;
    form._otherExclusions = (p.exclusions || []).filter((e) => !LIMITS.some((l) => l.exclusion.type === e.type));
    clearErrors();
  }
  function num(name) { const v = form.elements.namedItem(name).value.trim(); return v === "" ? null : Number(v); }
  function read() {
    const v = (name) => { const el = form.querySelector(`input[name="${name}"]:checked`); return el ? el.value : null; };
    const out = {
      risk_band: v("risk_band"),
      experience: v("experience"),
      language: "en",
      horizon_years: { min: num("horizon_min"), max: num("horizon_max") },
      capital: num("capital"),
      loss_capacity: num("loss_capacity") === null ? null : num("loss_capacity") / 100,
      objective: form.elements.namedItem("objective").value.trim() || null,
      preferences: form.elements.namedItem("preferences").value.split("\n").map((s) => s.trim()).filter(Boolean),
      exclusions: [...LIMITS.filter((l) => form.elements.namedItem(`limit-${l.key}`).checked).map((l) => l.exclusion), ...(form._otherExclusions || [])],
      agent_toggles: Object.fromEntries(AGENT_ORDER.map((a) => [a, form.elements.namedItem(`toggle-${a}`).checked])),
    };
    return out;
  }

  /* validation */
  function clearErrors() {
    for (const el of Object.values(errors)) el.replaceChildren();
    for (const el of form.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
  }
  function setError(name, msg) {
    errors[name].replaceChildren(icon("alert", 14), h("span", null, msg));
    const el = form.elements.namedItem(name);
    const target = el instanceof RadioNodeList ? el[0] : el;
    if (target && !(el instanceof RadioNodeList)) target.setAttribute("aria-invalid", "true");
    return target;
  }
  function validate(data) {
    clearErrors();
    const bad = [];
    if (!data.risk_band) bad.push(setError("risk_band", "Pick the risk level that fits you best."));
    if (!data.experience) bad.push(setError("experience", "Pick your experience level."));
    const { min, max } = data.horizon_years;
    for (const [name, val] of [["horizon_min", min], ["horizon_max", max]]) {
      if (val !== null && (!Number.isInteger(val) || val < 0 || val > 60)) bad.push(setError(name, "Use whole years between 0 and 60."));
    }
    if (min !== null && max !== null && max < min) bad.push(setError("horizon_max", "The end can't be before the start."));
    if (max !== null && min === null) bad.push(setError("horizon_min", "Add the start of your horizon too."));
    if (data.capital !== null && !(data.capital > 0)) bad.push(setError("capital", "Use an amount above zero, without spaces."));
    if (data.loss_capacity !== null && !(data.loss_capacity >= 0 && data.loss_capacity <= 1)) bad.push(setError("loss_capacity", "Use a percentage between 0 and 100."));
    return bad;
  }

  function markDirty() {
    state.dirty = true;
    status.textContent = "Unsaved changes.";
  }
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  form.addEventListener("focusout", (e) => {
    if (!e.target.name || e.target.type === "radio" || e.target.type === "checkbox") return;
    const data = read();
    const before = Object.values(errors).some((el) => el.childNodes.length);
    if (before || e.target.value) { validate(data); for (const name of ["risk_band", "experience"]) if (!data[name]) errors[name].replaceChildren(); }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    topError.replaceChildren();
    const data = read();
    const bad = validate(data);
    if (bad.length) {
      const first = bad.find(Boolean);
      if (first) first.focus();
      announce("Some fields need attention.");
      return;
    }
    const payload = { ...data };
    if (data.horizon_years.min === null) delete payload.horizon_years;
    // Send only the toggles that changed: the API forgets the chat quote of any toggle the form sets.
    const before = (state.profile && state.profile.agent_toggles) || null;
    if (before) {
      const changed = Object.fromEntries(Object.entries(data.agent_toggles).filter(([a, on]) => before[a] !== on));
      if (Object.keys(changed).length) payload.agent_toggles = changed; else delete payload.agent_toggles;
    }
    saveBtn.disabled = true;
    saveBtn.setAttribute("aria-busy", "true");
    saveBtn.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving");
    try {
      const fresh = (await api("PUT", "/v1/profile", payload)).data;
      state.profile = fresh;
      state.dirty = false;
      // Rebuild from what the server saved (e.g. a chat quote it dropped), keeping the confirmation.
      const confirmation = h("span", { class: "saved" }, icon("check", 14),
        `Saved at ${new Date().toLocaleTimeString("en-CA", { timeStyle: "short" })}`);
      const next = profileForm(fresh, confirmation);
      root.replaceWith(next);
      next.querySelector('button[type="submit"]').focus();
      announce("Profile saved.");
      return;
    } catch (err) {
      const box = alertBox(err, "your profile");
      if (err.status === 422 && Array.isArray(err.detail)) {
        box.querySelector(".alert__body").textContent =
          err.detail.map((d) => `${(d.loc || []).slice(-1)[0] || "profile"}: ${d.msg}`).join(" ");
      }
      topError.replaceChildren(box);
      box.focus();
    } finally {
      saveBtn.disabled = false;
      saveBtn.removeAttribute("aria-busy");
      saveBtn.replaceChildren("Save profile");
    }
  });

  fill(saved || {});
  state.dirty = false;
  const root = h("div", { class: "columns" }, h("div", { class: "stack" }, examples, form), h("aside", { class: "stack" }, account));
  return root;
}

/* ---------------- Run (the plan) ---------------- */

function renderRun(main, runId) {
  const back = h("p", null, h("a", { class: "link-btn", href: "#/ask" }, "All questions"));
  const body = h("div", { class: "stack" }, h("div", { class: "panel" }, skeleton(5)));
  main.append(back, body);
  let polls = 0;
  let shown = null; // what is on screen: re-render only when it changes (keeps "Details" open, keeps focus)

  async function load() {
    let run;
    try {
      run = (await api("GET", `/v1/runs/${encodeURIComponent(runId)}`)).data;
    } catch (err) {
      if (shown) return; // a failed poll keeps the last good view
      if (err.status === 404) {
        body.replaceChildren(h("div", { class: "empty" },
          h("h1", null, "We can't find this analysis"),
          h("p", { class: "lede" }, "The link may be wrong, or the analysis belongs to another user."),
          h("a", { class: "btn btn--secondary", href: "#/ask" }, "Ask a new question")));
      } else {
        body.replaceChildren(h("h1", { class: "visually-hidden" }, "Analysis plan"), alertBox(err, "this analysis", load));
      }
      return;
    }
    const waiting = (run.status === "QUEUED" || run.status === "RUNNING") && !run.result;
    const stopped = waiting && polls >= POLL_MAX;
    const key = JSON.stringify([run.status, run.result, run.updated_at, stopped]);
    if (key !== shown) {
      if (shown) announce(`Status: ${statusWord(run.status)}.`);
      body.replaceChildren(drawRun(run, stopped));
      shown = key;
    }
    if (waiting && !stopped && window.location.hash === `#/runs/${runId}`) {
      polls += 1;
      state.pollTimer = window.setTimeout(load, POLL_EVERY_MS);
    }
  }

  function statusWord(s) { return { QUEUED: "Queued", RUNNING: "Running", DONE: "Done", FAILED: "Failed" }[s] || s; }

  function drawRun(run, stoppedPolling = false) {
    const plan = run.execution_plan;
    const enabled = plan.enabled_agents || [];
    const userOff = (plan.disabled_agents || []).filter((d) => !/not needed/.test(d.reason));
    const notNeeded = (plan.disabled_agents || []).filter((d) => /not needed/.test(d.reason));
    const statusLine = {
      QUEUED: "Waiting for the analysis agents.",
      RUNNING: "The agents are working on it. This page updates by itself.",
      DONE: "The analysis is complete.",
      FAILED: run.error || "The analysis stopped with an error.",
    }[run.status];

    const head = h("div", { class: "page-head" },
      h("p", { class: "small muted" }, "Your question"),
      h("h1", { class: "run-question" }, run.request.user_message),
      h("p", { class: "status" }, h("span", { class: "status__word" }, statusWord(run.status)), h("span", null, statusLine),
        stoppedPolling ? h("button", { class: "link-btn", type: "button", onclick: () => { polls = 0; shown = null; load(); } }, "Check again") : null));

    const lookedAt = h("section", { class: "stack-sm", "aria-labelledby": "plan-title" },
      h("h2", { id: "plan-title" }, "What AlphaForge will look at"),
      enabled.length
        ? h("ul", { class: "plan" }, enabled.map((a) => h("li", { class: "plan__item" },
          h("span", { class: "plan__mark" }, icon("check", 16)),
          h("span", { class: "plan__name" }, (AGENTS[a.agent] || { name: a.agent }).name),
          h("p", { class: "plan__why" }, whyEnabled(a, plan.task)),
          settingsOf(a) ? h("p", { class: "plan__setting" }, settingsOf(a)) : null)))
        : h("div", { class: "empty" }, h("p", null, "Nothing to analyse. This request asks for something a simulation can't do, such as placing a real trade. The answer explains why.")));

    const leftOut = (userOff.length || notNeeded.length) ? h("section", { class: "stack-sm", "aria-labelledby": "left-title" },
      h("h2", { id: "left-title" }, "Left out"),
      userOff.length ? h("ul", { class: "plan" }, userOff.map((d) => h("li", { class: "plan__item" },
        h("span", { class: "plan__mark plan__mark--off" }, icon("minus", 16)),
        h("span", { class: "plan__name" }, (AGENTS[d.agent] || { name: d.agent }).name),
        h("p", { class: "plan__why" }, whyDisabled(d))))) : null,
      notNeeded.length ? h("p", { class: "muted" }, `Not needed for this question: ${notNeeded.map((d) => (AGENTS[d.agent] || { name: d.agent }).name).join(", ")}.`) : null) : null;

    const answer = h("section", { class: "stack-sm", "aria-labelledby": "answer-title" },
      h("h2", { id: "answer-title" }, "Answer"),
      run.result && run.result.summary
        ? h("div", { class: "result-text" }, String(run.result.summary).split(/\n{2,}/).map((para) => h("p", null, para)),
          (run.result.evidence_ids || []).length ? h("p", { class: "small muted" }, `Backed by ${run.result.evidence_ids.length} pieces of evidence.`) : null)
        : h("div", { class: "empty" },
          h("p", null, run.status === "FAILED" ? "No answer: the analysis stopped before the end." : "The answer will appear here."),
          run.status === "QUEUED" ? h("p", { class: "muted small" }, "The analysis agents aren't connected to the app yet, so for now an analysis stops at this plan.") : null));

    const req = run.request;
    const details = h("details", null,
      h("summary", null, "Details"),
      h("dl", { class: "tech" },
        h("dt", null, "Type of question"), h("dd", null, TASKS[plan.task] || plan.task),
        (req.positions || []).length ? [h("dt", null, "Assets mentioned"), h("dd", { class: "num" },
          req.positions.map((p) => p.weight == null ? p.ticker : `${p.ticker} ${Math.round(p.weight * 100)}%`).join(", "))] : null,
        h("dt", null, "Data up to"), h("dd", { class: "num" }, plan.as_of || "Latest available"),
        h("dt", null, "How sure the reading is"), h("dd", { class: "num" }, `${Math.round(plan.intake_confidence * 100)}%`),
        h("dt", null, "Explained for"), h("dd", null, `${titleOf(EXPERIENCE, run.prompt_ref.experience)} investors (template ${run.prompt_ref.version})`),
        (plan.warnings || []).map(plainNote).filter(Boolean).length ? [h("dt", null, "Notes"), h("dd", null,
          h("ul", { class: "warning-list" }, plan.warnings.map(plainNote).filter(Boolean).map((w) => h("li", null, w))))] : null,
        h("dt", null, "Run"), h("dd", { class: "code" }, run.run_id),
        h("dt", null, "Created"), h("dd", null, timeText(run.created_at)),
        h("dt", null, "Raw reasons"), h("dd", { class: "code code--lines" },
          [...enabled, ...(plan.disabled_agents || [])].map((a) => `${a.agent}: ${a.reason}`).join("\n"))));

    const p = run.profile;
    const basedOn = h("section", { class: "panel stack-sm", "aria-labelledby": "based-title" },
      h("h2", { id: "based-title", class: "section-title" }, "Based on your profile"),
      h("dl", { class: "facts" },
        h("dt", null, "Risk level"), h("dd", null, titleOf(RISK, p.risk_band)),
        h("dt", null, "Horizon"), h("dd", null, horizonText(p.horizon_years)),
        h("dt", null, "Experience"), h("dd", null, titleOf(EXPERIENCE, p.experience))),
      h("p", { class: "small muted" }, "As it was when you asked. Later changes to your profile don't rewrite this analysis."));

    return h("div", { class: "stack" }, head,
      h("div", { class: "columns" }, h("div", { class: "stack" }, lookedAt, leftOut, answer, details), h("aside", null, basedOn)));
  }

  load();
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
  btn.replaceChildren(icon(dark ? "sun" : "moon", 20));
}
function setWho() { document.getElementById("who").textContent = `Demo user ${actorId()}`; }

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
setWho();
route();
