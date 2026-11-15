/* "My portfolio" (#/portfolio): the dashboard of the portfolio AlphaForge builds from the user's profile.
 *
 * Flow: after the profile is saved (or on the first visit with a profile and no portfolio yet), the app asks the
 * pipeline PORTFOLIO_PROMPT; the run's evidence becomes this page. Detail follows the user's experience
 * (beginner → simple, intermediate → standard, advanced → detailed; the user can switch), and readers who want to
 * learn get a one-line "What this means" under each part. Every figure is read from the run's evidence or is plain
 * arithmetic on its weights (portfolio-data.js); every chart has a "Show the numbers" table.
 *
 * The app's helpers (h, api, band...) are passed in as `ctx` by app.js. */

import * as D from "./portfolio-data.js";
import * as PD from "./projection-data.js";
import { fanChart, meter, pairedBars, rankedBars, segmentedBar, heatmap } from "./charts.js";

let universe = null;
async function loadUniverse() {
  if (!universe) {
    try { universe = (await (await fetch("assets/universe.json")).json()).assets; } catch { universe = {}; }
  }
  return universe;
}

/* ---------- Background jobs: one question at a time per kind, survives navigation inside the app ---------- */

const jobs = new Map(); // `${actor}:${kind}` -> { question, promise, run, error }
const listeners = new Set();
const sleep = (ms) => new Promise((r) => { window.setTimeout(r, ms); });

async function ask(ctx, question) {
  const { data } = await ctx.api("POST", "/v1/runs", { text: question });
  if (data.kind !== "run") {
    const err = new Error(data.message || data.question || "AlphaForge could not start the analysis.");
    err.reply = data;
    throw err;
  }
  let run = (await ctx.api("GET", `/v1/runs/${encodeURIComponent(data.run_id)}`)).data;
  // Inline dispatch (the API's default) answers when the run is done; a background dispatcher needs polling.
  for (let i = 0; !D.isFinal(run.status) && i < 60; i += 1) {
    if (run.status === "QUEUED" && /stays at its plan|AF_DISPATCH=none/.test(run.note || "")) break;
    await sleep(4000);
    run = (await ctx.api("GET", `/v1/runs/${encodeURIComponent(data.run_id)}`)).data;
  }
  return run;
}

export function jobFor(ctx, kind) { return jobs.get(`${ctx.actorId()}:${kind}`) || null; }

/** Start (or join) a question in the background; the dashboard redraws when it ends. */
export function startJob(ctx, kind, question) {
  const key = `${ctx.actorId()}:${kind}`;
  const running = jobs.get(key);
  if (running && !running.done) return running;
  const job = { question, done: false, run: null, error: null };
  job.promise = ask(ctx, question)
    .then(async (run) => {
      job.run = run;
      if (kind === "portfolio" && run.status === "DONE") await syncPortfolio(ctx).catch(() => {}); // usable in the chat at once
    })
    .catch((err) => { job.error = err; })
    .finally(() => { job.done = true; listeners.forEach((fn) => fn(kind, job)); });
  jobs.set(key, job);
  return job;
}

/** Save the latest built portfolio into the profile (so "my portfolio" questions use it), once per portfolio run.
 * Runs when a build finishes, when the dashboard shows a portfolio and before the chat sends a question.
 * A mix the user picked (their holdings, a what-if mix) stays theirs until a newer build finishes. */
export async function syncPortfolio(ctx) {
  const profile = await ctx.loadProfile(true);
  if (!profile) return null;
  const runs = (await ctx.api("GET", "/v1/runs?limit=100")).data;
  const done = D.findBuild(runs);
  if (!done || profile.portfolio_run_id === done.run_id || D.chosenIsCurrent(profile, done)) return profile.portfolio || null;
  const [run, records] = await Promise.all([
    ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}`).then((r) => r.data),
    ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}/evidence`).then((r) => r.data)]);
  const p = D.extractPortfolio(run, records);
  if (!p.weights) return profile.portfolio || null;
  await ctx.api("PUT", "/v1/profile", { portfolio: p.weights, portfolio_run_id: p.runId });
  await ctx.loadProfile(true);
  return p.weights;
}

/** Called by the profile page after a save: rebuild when the profile changed in a way that shapes a portfolio. */
export function onProfileSaved(ctx, before, after) {
  if (!after || !after.risk_band || !after.experience) return null;
  // Someone who entered what they hold keeps it: the fit is measured again live, "Optimize my portfolio" is theirs to press.
  if (Object.keys(D.cleanWeights(after.holdings)).length) return null;
  if (before && !D.profileChanged(before, after)) return null;
  return startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT);
}

/* ---------- Starting points: build from the profile, or the portfolio the person already has ---------- */

/** A profile just completed for the first time, with no portfolio yet: the person chooses how to start. */
export function needsStartChoice(ctx, profile) {
  const job = jobFor(ctx, "portfolio");
  return Boolean(profile) && !profile.portfolio && !Object.keys(D.cleanWeights(profile.holdings)).length && !(job && !job.done);
}

/** Build a portfolio from the profile (the usual start). */
export function startBuild(ctx) { return startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT); }

/** Make `weights` the user's portfolio (their holdings, or a mix from the what-if): saved in the profile, analysed by
 * the run riskPrompt(weights). It stays their portfolio until a newer build finishes (D.chosenIsCurrent). */
export async function adoptWeights(ctx, weights, { asHoldings = false } = {}) {
  const w = Object.fromEntries(D.sortedWeights(weights).map(([t, v]) => [t, Math.round(v * 10000) / 10000]));
  const runs = (await ctx.api("GET", "/v1/runs?limit=100")).data;
  const body = { portfolio: w, portfolio_run_id: D.chosenMarker(D.findBuild(runs)) };
  if (asHoldings) body.holdings = w;
  await ctx.api("PUT", "/v1/profile", body);
  await ctx.loadProfile(true);
  ctx.store.set(VIEW_KEY(ctx), null); // show the portfolio just chosen
  startJob(ctx, "risk", D.riskPrompt(w));
}

/** "I already have a portfolio": a window to enter each holding and its share, then "Generate portfolio". */
export async function openHoldingsDialog(ctx, { initial = null } = {}) {
  const { h } = ctx;
  const uni = await loadUniverse();
  const start = initial && Object.keys(initial).length
    ? D.sortedWeights(initial).map(([t, v]) => ({ ticker: t, percent: String(Math.round(v * 1000) / 10) }))
    : [{ ticker: "", percent: "" }, { ticker: "", percent: "" }];
  const rows = h("div", { class: "holdings__rows" });
  const total = h("p", { class: "small holdings__total", "aria-live": "polite" });
  const errors = h("div", { class: "holdings__errors" });
  const options = h("datalist", { id: "holdings-assets" }, Object.entries(uni).map(([t, a]) =>
    h("option", { value: t }, `${a.name || t}${a.plain && a.plain !== a.name ? ` (${a.plain})` : ""}`)));
  const read = () => [...rows.children].map((r) => ({ ticker: r.querySelector(".holdings__ticker").value, percent: r.querySelector(".holdings__pct").value }));
  const showTotal = () => {
    const t = Math.round(read().reduce((a, r) => a + (Number(r.percent) || 0), 0) * 10) / 10;
    const off = Math.abs(t - 100) > 0.5;
    total.textContent = `Total: ${t}%${off ? " (needs to be 100%)" : ""}`;
    total.classList.toggle("is-off", off);
  };
  let n = 0;
  const row = ({ ticker, percent }) => {
    n += 1;
    const t = h("input", { class: "input holdings__ticker", id: `holding-t-${n}`, list: "holdings-assets", value: ticker, autocomplete: "off",
      placeholder: "Ticker, e.g. XIU.TO", "aria-label": `Investment ${n}` });
    const p = h("input", { class: "input holdings__pct", id: `holding-p-${n}`, type: "number", min: "0", max: "100", step: "0.1",
      inputmode: "decimal", value: percent, placeholder: "0", "aria-label": `Share of your money for investment ${n}, in %` });
    p.addEventListener("input", showTotal);
    const el = h("div", { class: "holdings__row" }, t,
      h("span", { class: "holdings__pct-wrap" }, p, h("span", { "aria-hidden": "true" }, "%")),
      h("button", { class: "link-btn", type: "button", "aria-label": `Remove investment ${n}`,
        onclick: () => { if (rows.children.length > 1) { el.remove(); showTotal(); } } }, "Remove"));
    return el;
  };
  start.forEach((r) => rows.append(row(r)));
  const generate = h("button", { class: "btn btn--primary", type: "submit" }, "Generate portfolio");
  const dialog = h("dialog", { class: "dialog", "aria-labelledby": "holdings-title" });
  const form = h("form", { class: "stack", novalidate: true },
    h("div", { class: "dialog__head" },
      h("h2", { id: "holdings-title", class: "section-title" }, "The portfolio you already have"),
      closeButton(ctx, "Close", () => dialog.close())),
    h("p", { class: "small muted" }, "Add each stock, fund or bond fund you hold and its share of your money. AlphaForge analyses this mix as it is and scores how well it fits your profile. It builds nothing new until you ask it to."),
    rows, options,
    h("div", { class: "holdings__foot" },
      h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: () => {
        const r = row({ ticker: "", percent: "" }); rows.append(r); r.querySelector("input").focus();
      } }, "Add a holding"),
      total),
    errors,
    h("div", { class: "dialog__actions" },
      h("button", { class: "btn btn--secondary", type: "button", onclick: () => dialog.close() }, "Cancel"), generate));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = D.holdingsFromRows(read(), uni);
    if (res.errors.length) {
      errors.replaceChildren(h("ul", { class: "warning-list", role: "alert" }, res.errors.map((x) => h("li", null, x))));
      return;
    }
    errors.replaceChildren();
    generate.disabled = true; generate.textContent = "Saving…";
    try {
      await adoptWeights(ctx, res.weights, { asHoldings: true });
      dialog.close();
      if (location.hash === "#/portfolio") ctx.route(); else location.hash = "#/portfolio";
    } catch (err) {
      generate.disabled = false; generate.textContent = "Generate portfolio";
      errors.replaceChildren(ctx.alertBox(err, "your portfolio"));
    }
  });
  dialog.append(form);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  showTotal();
  dialog.showModal();
  const first = rows.querySelector("input");
  if (first) first.focus();
}

/** A small "×" button that closes a bar or a window. */
function closeButton(ctx, label, onclick) {
  return ctx.h("button", { class: "close-btn", type: "button", "aria-label": label, title: label, onclick },
    ctx.h("span", { "aria-hidden": "true" }, "×"));
}

/* ---------- Page ---------- */

const DETAIL_KEY = (ctx) => `af.detail.${ctx.actorId()}`;
const VIEW_KEY = (ctx) => `af.view.${ctx.actorId()}`; // "actual": the holdings are on screen instead of the current portfolio
const AUTO_KEY = (ctx, kind) => `af.auto.${kind}.${ctx.actorId()}`;
function autoOnce(ctx, kind) { // auto-start a question at most once per browser session (a failure is not retried in a loop)
  try {
    if (sessionStorage.getItem(AUTO_KEY(ctx, kind))) return false;
    sessionStorage.setItem(AUTO_KEY(ctx, kind), "1");
  } catch { /* storage off: still once per page load via jobs */ }
  return true;
}

const pct = (v, digits = 0) => `${(v * 100).toFixed(digits)}%`;

/** A figure that counts up to its value when it appears (shown at once with reduced motion). */
function countUp(el, value, ms = 900) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return el;
  const t0 = performance.now();
  const tick = (t) => {
    const k = Math.min(1, (t - t0) / ms);
    const eased = 1 - (1 - k) ** 3;
    el.textContent = k < 1 ? String(Math.round(value * eased * 2) / 2) : String(value);
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return el;
}
const pct1 = (v) => `${(v * 100).toFixed(1).replace(/\.0$/, "")}%`;

export async function renderPortfolio(main, ctx) {
  const { h } = ctx;
  const body = h("div", { class: "stack" }, h("div", { class: "panel" }, ctx.skeleton(5)));
  const detailSlot = h("div");
  const toolsSlot = h("div", { class: "page-head__actions" });
  main.append(
    ctx.band("head", h("div", { class: "page-head page-head--split" },
      h("div", { class: "stack-sm" },
        h("h1", null, "Your portfolio"),
        h("p", null, "The simulated portfolio AlphaForge builds from your profile, or the one you already have, with what it holds, how risky it is and how well it fits you. Simulation only, not advice.")),
      h("div", { class: "page-head__tools" }, toolsSlot, detailSlot))),
    ctx.band("body", body));

  // Redraw when a background question ends, as long as this page is still the one on screen.
  const onJob = () => { if (document.body.contains(body)) draw(); else listeners.delete(onJob); };
  listeners.add(onJob);

  async function draw() {
    let profile; let runs; let uni;
    try {
      [profile, runs, uni] = await Promise.all([ctx.loadProfile(true), ctx.api("GET", "/v1/runs?limit=100").then((r) => r.data), loadUniverse()]);
    } catch (err) {
      body.replaceChildren(ctx.alertBox(err, "your portfolio", draw));
      return;
    }
    if (!profile) {
      detailSlot.replaceChildren();
      body.replaceChildren(h("section", { class: "panel empty" },
        h("h2", { class: "section-title" }, "First, tell AlphaForge about you"),
        h("p", { class: "lede" }, "Your portfolio is built from your profile: your risk level, your horizon and your experience. Fill it in, save it, and AlphaForge builds your portfolio right away."),
        h("a", { class: "btn btn--primary", href: "#/profile" }, "Set up my profile")));
      return;
    }
    const level = D.levelFor(profile, ctx.store.get(DETAIL_KEY(ctx)));
    if (!detailSlot.firstChild) {
      detailSlot.append(detailControl(ctx, level, (l) => {
        ctx.store.set(DETAIL_KEY(ctx), l === D.levelFor(profile) ? null : l);
        ctx.announce(`${D.LEVEL_LABELS[l]} view.`);
        body.classList.add("is-switching"); // fades the old view out while the new one is drawn
        window.setTimeout(draw, 160);
      }));
    } else {
      detailSlot.firstChild.update(level);
    }

    const holdings = D.cleanWeights(profile.holdings);
    const hasHoldings = Object.keys(holdings).length > 0;
    const job = jobFor(ctx, "portfolio");
    const build = D.findBuild(runs);
    const building = Boolean((job && !job.done) || (!(job && job.done) && D.pendingBuild(runs)));
    // The current portfolio: a mix the user picked (holdings, what-if) while no newer build finished, else the latest build.
    let chosen = D.chosenIsCurrent(profile, build) ? D.cleanWeights(profile.portfolio) : null;
    if (!chosen && !build && hasHoldings) chosen = holdings;
    toolsSlot.replaceChildren(...(hasHoldings ? [h("button", { class: "btn btn--primary", type: "button", disabled: building,
      onclick: () => { ctx.store.set(VIEW_KEY(ctx), null); startJob(ctx, "portfolio", D.optimizePrompt(holdings)); ctx.route(); } },
      building ? "Optimizing…" : "Optimize my portfolio")] : []));

    if (!chosen && !build) {
      if (job && job.done && (job.error || (job.run && job.run.status !== "DONE"))) {
        body.replaceChildren(buildFailed(ctx, job), history(ctx, runs));
        return;
      }
      body.classList.remove("is-switching");
      body.replaceChildren(building ? buildingPanel(ctx) : startPanel(ctx), history(ctx, runs));
      return;
    }

    // With holdings, the person can look at what they actually hold whenever the current portfolio is something else.
    const canSwitch = hasHoldings && !(chosen && D.sameWeights(chosen, holdings));
    const showActual = canSwitch && ctx.store.get(VIEW_KEY(ctx)) === "actual";
    let p; let risk; let riskRunId; let riskState;
    if (showActual || chosen) {
      const w = showActual ? holdings : chosen;
      const got = await analysed(w);
      if (!got) return;
      p = got; risk = p.risk; riskRunId = p.runId;
      riskState = risk.matches ? "ready" : p.pending ? "measuring" : "missing";
      p.actual = hasHoldings && D.sameWeights(w, holdings);
    } else {
      let run; let records;
      try {
        [run, records] = await Promise.all([
          ctx.api("GET", `/v1/runs/${encodeURIComponent(build.run_id)}`).then((r) => r.data),
          ctx.api("GET", `/v1/runs/${encodeURIComponent(build.run_id)}/evidence`).then((r) => r.data)]);
      } catch (err) {
        body.replaceChildren(ctx.alertBox(err, "your portfolio", draw));
        return;
      }
      p = D.extractPortfolio(run, records);
      p.optimized = (build.question || "").startsWith(D.OPTIMIZE_PROMPT_PREFIX);
      if (!p.weights) {
        body.replaceChildren(noWeights(ctx, p, run), history(ctx, runs));
        return;
      }
      // This is now the user's portfolio: saved in the profile, so a question about "my portfolio" is analysed on it.
      if (profile.portfolio_run_id !== p.runId) syncPortfolio(ctx).catch(() => { /* retried on the next visit or question */ });

      // Risk of these exact weights: from this run when it measured them, else from the follow-up question.
      risk = p.risk; riskRunId = run.run_id; riskState = risk.matches ? "ready" : "missing";
      if (!risk.matches) {
        const q = D.riskPrompt(p.weights);
        const rdone = D.findRun(runs, q, { exact: true });
        const rjob = jobFor(ctx, "risk");
        const ended = rjob && rjob.done && rjob.question === q; // this page asked and saw the answer: not pending
        if (rdone) {
          try {
            const recs = (await ctx.api("GET", `/v1/runs/${encodeURIComponent(rdone.run_id)}/evidence`)).data;
            const r2 = D.riskOf(recs, p.weights);
            if (r2.matches) { risk = r2; riskRunId = rdone.run_id; riskState = "ready"; }
          } catch { /* keep "missing": the section offers to measure it */ }
        } else if (!ended && ((rjob && !rjob.done && rjob.question === q) || D.pendingRun(runs, q, true))) {
          riskState = "measuring";
        } else if (!ended && autoOnce(ctx, `risk-${run.run_id}`)) {
          startJob(ctx, "risk", q);
          riskState = "measuring";
        }
      }
    }
    p.key = p.runId && !p.chosen ? p.runId : `w:${JSON.stringify(D.sortedWeights(p.weights))}`;
    const currentLabel = chosen ? "The mix I chose" : p.optimized || (build.question || "").startsWith(D.OPTIMIZE_PROMPT_PREFIX) ? "Optimized portfolio" : "Built for my profile";
    const switcher = canSwitch ? viewSwitch(ctx, showActual ? "actual" : "current", currentLabel, () => {
      body.classList.add("is-switching"); // same fade as the detail switch
      window.setTimeout(draw, 160);
    }) : null;

    const view = { ctx, h, profile, p, risk, riskState, riskRunId, level, learn: Boolean(profile.wants_to_learn) && level !== "detailed", uni, hasHoldings };
    body.classList.remove("is-switching");
    body.replaceChildren(...[ // sections that don't apply are null: dropped, never rendered as text
      switcher,
      ...banners(view, building),
      heroCard(view),
      kpis(view),
      allocationSection(view),
      whatIfSection(view),
      riskSection(view),
      projectionSection(view),
      analysisOn(profile, "macro_scenarios") ? ratesSection(view) : null,
      level !== "simple" && risk.concentration ? sectorSection(view) : null,
      level === "detailed" && risk.metrics ? metricsSection(view) : null,
      level === "detailed" && risk.correlation ? correlationSection(view) : null,
      checksSection(view),
      // The analyses switched off in the profile are not shown (they don't change the mix: no rebuild needed).
      level !== "simple" && p.esg && wantsEsg(profile) && (analysisOn(profile, "esg") || esgLimit(profile)) ? esgSection(view) : null,
      level !== "simple" && p.news.length && (analysisOn(profile, "news") || analysisOn(profile, "fundamentals")) ? newsSection(view) : null,
      explanationSection(view),
      history(ctx, runs, p.runId)].filter(Boolean));

    /** A mix the user chose, analysed by the run riskPrompt(weights): started once when missing, polled while it runs. */
    async function analysed(w) {
      const q = D.riskPrompt(w);
      const done = D.findRun(runs, q, { exact: true });
      if (done) {
        try {
          const [run, recs] = await Promise.all([
            ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}`).then((r) => r.data),
            ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}/evidence`).then((r) => r.data)]);
          return D.analysedPortfolio(w, run, recs);
        } catch (err) {
          body.replaceChildren(ctx.alertBox(err, "your portfolio", draw));
          return null;
        }
      }
      const rjob = jobFor(ctx, "risk");
      const ended = rjob && rjob.done && rjob.question === q;
      let pending = !ended && Boolean((rjob && !rjob.done && rjob.question === q) || D.pendingRun(runs, q, true));
      if (!pending && !ended && autoOnce(ctx, `analysis-${q}`)) { startJob(ctx, "risk", q); pending = true; }
      return { ...D.analysedPortfolio(w, null, []), pending };
    }
  }

  draw();
}

/* ---------- States ---------- */

function buildingPanel(ctx) {
  const { h } = ctx;
  return h("section", { class: "panel building", "aria-live": "polite" },
    h("p", { class: "building__head" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Building your portfolio"),
    h("p", { class: "lede" }, "AlphaForge is choosing a mix that fits your profile, checking it against your rules and writing the explanation. This takes about a minute. You can keep using the app: this page updates when it's ready."));
}

/** No portfolio yet: build one from the profile, or enter the one the person already has. */
export function startChoice(ctx, { title = "No portfolio yet", headingLevel = "h2" } = {}) {
  const { h } = ctx;
  return h("div", { class: "stack-sm start-choice" },
    h(headingLevel, { class: "section-title" }, title),
    h("p", { class: "lede" }, "AlphaForge can build a portfolio from your profile in about a minute, or analyse the one you already have and score how well it fits you."),
    h("div", { class: "start-choice__actions" },
      h("button", { class: "btn btn--primary", type: "button", onclick: () => { startBuild(ctx); if (location.hash === "#/portfolio") ctx.route(); else location.hash = "#/portfolio"; } }, "Build my portfolio"),
      h("button", { class: "btn btn--secondary", type: "button", onclick: () => openHoldingsDialog(ctx) }, "I already have a portfolio")));
}

function startPanel(ctx) {
  return ctx.h("section", { class: "panel empty" }, startChoice(ctx));
}

function buildFailed(ctx, job) {
  const { h } = ctx;
  const retry = () => { startJob(ctx, "portfolio", job.question || D.PORTFOLIO_PROMPT); ctx.route(); };
  if (job.error) {
    if (job.error.reply) {
      return h("section", { class: "panel" }, h("h2", { class: "section-title" }, "AlphaForge needs something first"),
        h("p", null, job.error.message), h("a", { class: "link-btn", href: "#/profile" }, "Open my profile"));
    }
    return ctx.alertBox(job.error, "your portfolio", retry);
  }
  const run = job.run;
  const planOnly = run.status === "QUEUED";
  return h("section", { class: "panel" },
    h("h2", { class: "section-title" }, planOnly ? "Analyses aren't available right now" : "Your portfolio couldn't be built this time"),
    h("p", { class: "lede" }, planOnly
      ? "AlphaForge can't run analyses right now, so no portfolio can be built. Try again later."
      : "The analysis stopped before the end. You can see what happened, or try again."),
    h("div", { class: "notice" },
      h("a", { class: "link-btn", href: `#/runs/${run.run_id}` }, "See what happened"),
      planOnly ? null : h("button", { class: "btn btn--secondary", type: "button", onclick: retry }, "Try again")));
}

function noWeights(ctx, p, run) {
  const { h } = ctx;
  return h("section", { class: "panel" },
    h("h2", { class: "section-title" }, "No mix fits all of your rules"),
    h("p", { class: "lede" }, p.infeasible && p.infeasible.message
      ? p.infeasible.message
      : "The allocation search found no portfolio that respects every rule of your profile at the same time."),
    p.infeasible && p.infeasible.conflicts.length ? h("ul", { class: "warning-list" }, p.infeasible.conflicts.map((c) => h("li", null, String(c)))) : null,
    h("div", { class: "notice" },
      h("a", { class: "link-btn", href: "#/profile" }, "Review my profile"),
      h("a", { class: "link-btn", href: `#/runs/${run.run_id}` }, "See the analysis")));
}

function banners(view, building) {
  const { ctx, h, p, profile, hasHoldings } = view;
  const out = [];
  const job = jobFor(ctx, "portfolio");
  const rebuild = hasHoldings ? D.optimizePrompt(D.cleanWeights(profile.holdings)) : D.PORTFOLIO_PROMPT;
  if (building) {
    out.push(h("div", { class: "banner", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      h("span", null, hasHoldings
        ? "AlphaForge is optimizing your portfolio: a mix close to what you hold that fits your profile better. This page updates when it's ready."
        : "A new portfolio is being built from your updated profile. This page updates when it's ready.")));
  } else if (job && job.done && (job.error || (job.run && job.run.status !== "DONE")) && !job.seen) {
    const bar = h("div", { class: "banner banner--action", role: "status" },
      h("span", null, "Your portfolio couldn't be optimized this time."),
      h("div", { class: "banner__actions" },
        job.run ? h("a", { class: "link-btn", href: `#/runs/${job.run.run_id}` }, "See what happened") : null,
        h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: () => { startJob(ctx, "portfolio", job.question); ctx.route(); } }, "Try again"),
        closeButton(ctx, "Close this message", () => { job.seen = true; bar.remove(); })));
    out.push(bar);
  } else if (D.profileChanged(p.profileSnapshot, profile)) {
    out.push(h("div", { class: "banner banner--action" },
      h("span", null, "Your profile changed since this portfolio was built."),
      h("button", { class: "btn btn--primary", type: "button", onclick: () => { startJob(ctx, "portfolio", rebuild); ctx.route(); } },
        hasHoldings ? "Optimize again" : "Rebuild my portfolio")));
  }
  return out;
}

/** With holdings: the current portfolio (optimized, built, or a mix the user chose) or what they actually hold. */
function viewSwitch(ctx, value, currentLabel, redraw) {
  return ctx.h("div", { class: "view-switch" }, segControl(ctx, {
    legend: "Portfolio shown", name: "portfolio-view", value,
    options: [["current", currentLabel], ["actual", "My actual portfolio"]],
    onChange: (v) => {
      ctx.store.set(VIEW_KEY(ctx), v === "actual" ? "actual" : null);
      redraw();
    },
  }));
}

/** A capsule switch whose thumb slides to the chosen option ("How much detail", "Values shown"...). Update it in place
 * (el.update) so the slide animates and focus stays on the option. */
export function segControl(ctx, { legend, name, options: choices, value, onChange, small = false }) {
  const { h } = ctx;
  const thumb = h("span", { class: "seg-control__thumb", "aria-hidden": "true" });
  const options = h("div", { class: "seg-control__options" }, thumb, choices.map(([v, label]) => h("label", { class: "seg-control__option" },
    h("input", { type: "radio", name, value: v, checked: v === value, onchange: () => { place(); onChange(v); } }),
    h("span", null, label))));
  const el = h("fieldset", { class: `seg-control${small ? " seg-control--small" : ""}` }, h("legend", null, legend), options);
  function place() {
    const on = options.querySelector("input:checked");
    if (!on) return;
    const label = on.closest("label");
    thumb.style.width = `${label.offsetWidth}px`;
    thumb.style.transform = `translateX(${label.offsetLeft - 2}px)`;
  }
  el.update = (v) => { const input = options.querySelector(`input[value="${v}"]`); if (input) input.checked = true; place(); };
  // First placement without a slide (the thumb appears under the current option), then slides on every change.
  requestAnimationFrame(() => { place(); requestAnimationFrame(() => options.classList.add("is-ready")); });
  const onResize = () => { if (el.isConnected) place(); else window.removeEventListener("resize", onResize); };
  window.addEventListener("resize", onResize);
  return el;
}

/** The Simple / Standard / Detailed switch, created once per page. */
export function detailControl(ctx, level, onChange) {
  return segControl(ctx, { legend: "How much detail", name: "detail-level", value: level, onChange,
    options: D.LEVELS.map((l) => [l, D.LEVEL_LABELS[l]]) });
}

/* ---------- Sections ---------- */

const verdictWord = (v) => ({ PASS: "Fits your profile", WARN: "Fits, with warnings", BLOCK: "Breaks a rule of your profile", UNVERIFIED: "Some rules couldn't be checked" })[v] || "Not checked";

function heroCard(view) {
  const { ctx, h, p, risk, level } = view;
  const v = (p.governance && p.governance.verdict) || "UNVERIFIED";
  const vs = ctx.VERDICTS[v] || ctx.VERDICTS.UNVERIFIED;
  // Written here from the page's own numbers, so it follows the level and "wants to learn" (the Explainer's text,
  // written once at build time, is under "Why this portfolio").
  const summary = D.heroSummary({ level, learn: view.learn, weights: p.weights, universe: view.uni, governance: p.governance,
    vol: risk.contribution ? risk.contribution.vol : null, profile: view.profile, own: Boolean(p.chosen) });
  const title = p.chosen ? (p.actual ? "The portfolio you have" : "The mix you chose")
    : p.optimized ? "Your optimized portfolio" : level === "simple" ? "A mix chosen for you" : "Your simulated portfolio";
  const origin = !p.chosen
    ? [`${p.optimized ? "Optimized from what you hold" : "Built from your profile"} on ${ctx.timeText(p.builtAt)} \u00B7 `,
      h("a", { href: `#/runs/${p.runId}` }, "See how it was built and its sources")]
    : p.runId ? [`Analysed on ${ctx.timeText(p.builtAt)} \u00B7 `, h("a", { href: `#/runs/${p.runId}` }, "See the analysis and its sources")]
      : [p.pending ? "AlphaForge is analysing this mix (about a minute). This page updates when it's done." : "This mix hasn't been analysed yet."];
  return h("section", { class: "panel hero-card", "aria-labelledby": "hero-title" },
    h("div", { class: "hero-card__main stack-sm" },
      !p.chosen || p.governance ? h("span", { class: `verdict verdict--${vs.cls}` }, ctx.icon(vs.icon, 14), verdictWord(v)) : null,
      h("h2", { id: "hero-title", class: "hero-card__title" }, title),
      h("p", { class: "hero-card__answer" }, summary.text),
      summary.learn ? learnNote(view, summary.learn) : null,
      h("p", { class: "small muted" }, ...origin)),
    fitCard(view));
}

/* ---------- Fit with the profile (POST /v1/fit-score): out of 100, measured on the server, every point explained ---------- */

const fitCache = new Map(); // weights + profile -> the server's answer (the level only changes how it is shown)

/** The fit of these weights with the profile (POST /v1/fit-score), asked once per weights and profile. */
function fitFor(ctx, profile, weights) {
  const key = JSON.stringify([weights, profile.risk_band, profile.horizon_years, profile.loss_capacity, profile.exclusions, profile.preferences]);
  if (!fitCache.has(key)) {
    const ask = ctx.api("POST", "/v1/fit-score", { weights }).then((r) => r.data);
    ask.catch(() => fitCache.delete(key)); // a failure is asked again next time
    fitCache.set(key, ask);
  }
  return fitCache.get(key);
}

function fitCard(view) {
  const { ctx, h, p, profile } = view;
  const box = h("div", { class: "hero-card__score", "aria-live": "polite" },
    h("p", { class: "kpi__label" }, "Fit with your profile"), h("p", { class: "small muted" }, "Measuring the fit…"));
  // replaceChildren writes a null as the text "null": drop the parts left out at this level.
  const show = (fit) => box.replaceChildren(...fitBody(view, fit).filter(Boolean));
  const load = () => {
    fitFor(ctx, profile, p.weights).then(show).catch((err) => {
      box.replaceChildren(h("p", { class: "kpi__label" }, "Fit with your profile"), ctx.alertBox(err, "the fit with your profile", load));
    });
  };
  load();
  return box;
}

function fitBody(view, fit) {
  const { h, level } = view;
  const lines = D.fitLines(fit, level);
  const caps = (fit.caps || []).map((c) => h("p", { class: "small" }, h("strong", null, `Capped at ${c.at}%: `), `${c.why}.`));
  return [
    h("p", { class: "kpi__label" }, "Fit with your profile"),
    h("p", { class: "hero-card__figure" }, countUp(h("span", null, `${fit.score}`), fit.score), h("span", { class: "hero-card__of" }, "%")),
    meter({ value: fit.score, max: 100, label: "Fit with your profile", display: `${fit.score}%`, tone: D.fitTone(fit.score) }),
    h("p", { class: "small" }, fit.band),
    ...caps,
    lines.length ? h("ul", { class: "small plain-list fit-lines" }, lines.map((l) => h("li", null,
      h("strong", null, level === "simple" ? l.title : `${l.title}: ${l.points} / ${l.max}`),
      l.reasons.length ? h("span", { class: "fit-lines__why" }, ` ${l.reasons.join("; ")}.`) : null)))
      : h("p", { class: "small muted" }, "Every part of your profile is met: no point is taken off."),
    level !== "simple" ? h("details", { class: "fold small" }, h("summary", null, "How this score is measured"),
      h("p", null, `${fit.method} Data ${fit.period.replace("/", " to ")}.`),
      h("ul", { class: "plain-list" }, fit.assumptions.map((a) => h("li", null, a)))) : null,
  ];
}

/* ---------- What if: move the weights, see the fit with the profile change live (POST /v1/fit-score) ---------- */

const trials = new Map(); // run id -> the weights being tried (kept when the level changes or the page redraws)

function whatIfSection(view) {
  const { ctx, h, p, level, profile } = view;
  const uni = view.uni || {};
  const label = (t) => (level === "simple" ? (uni[t] || {}).plain || t : t);
  let weights = trials.get(p.key) || { ...p.weights };
  let order = D.sortedWeights(weights).map(([t]) => t); // the rows keep their place while sliding
  let seq = 0; let timer = null;

  const rows = h("div", { class: "whatif__rows" });
  const result = h("div", { class: "whatif__result", "aria-live": "polite" });
  const suggestion = h("div", { class: "whatif__suggest", "aria-live": "polite" });
  const reset = h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: () => setWeights({ ...p.weights }, true) }, "Back to my portfolio");
  const pick = h("input", { class: "input whatif__pick", id: "whatif-pick", list: "whatif-assets", placeholder: "Ticker or name, e.g. NVDA",
    autocomplete: "off", "aria-describedby": "whatif-pick-help" });
  const pickHelp = h("p", { class: "small muted", id: "whatif-pick-help" }, "Starts at 5%; the others make room.");
  const options = h("datalist", { id: "whatif-assets" }, Object.entries(uni).map(([t, a]) => h("option", { value: t }, `${a.name || t}${a.plain && a.plain !== a.name ? ` (${a.plain})` : ""}`)));
  const add = () => {
    const raw = pick.value.trim();
    const t = uni[raw] ? raw : uni[raw.toUpperCase()] ? raw.toUpperCase()
      : Object.keys(uni).find((k) => (uni[k].name || "").toLowerCase() === raw.toLowerCase());
    if (!t) { pickHelp.textContent = raw ? `“${raw}” isn't in the list of investments AlphaForge has data for.` : "Type a ticker or a name first."; return; }
    if (!order.includes(t)) order.push(t);
    pick.value = ""; pickHelp.textContent = "Starts at 5%; the others make room.";
    setWeights(D.addHolding(weights, t, weights[t] ? weights[t] : 0.05), true);
  };
  pick.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });

  function drawRows() {
    rows.replaceChildren(...order.map((t) => {
      const v = weights[t] || 0;
      const out = h("output", { class: "whatif__value", for: `whatif-${t}` }, pct1(v));
      const input = h("input", { type: "range", class: "scale__input", id: `whatif-${t}`, min: "0", max: "100", step: "1",
        value: String(Math.round(v * 100)), style: `--fill: ${v * 100}%`, "aria-valuetext": `${pct1(v)} of the money` });
      input.addEventListener("input", () => setWeights(D.rebalance(weights, t, Number(input.value) / 100), false, t));
      return h("div", { class: "whatif__row", "data-ticker": t },
        h("label", { class: "whatif__name", for: `whatif-${t}` }, label(t), level !== "simple" ? h("span", { class: "small muted" }, ` ${(uni[t] || {}).plain || ""}`) : null),
        input, out,
        order.length > 1 ? h("button", { class: "link-btn whatif__remove", type: "button", "aria-label": `Remove ${label(t)}`,
          onclick: () => { order = order.filter((x) => x !== t); setWeights(D.removeHolding(weights, t), true); } }, "Remove") : null);
    }));
  }
  /** New weights: update the sliders in place (the one being dragged keeps its focus), then score them. */
  function setWeights(next, redraw, moving, keepSuggestion = false) {
    weights = next;
    if (!keepSuggestion) suggestion.replaceChildren(); // a suggestion is for the mix it was made from
    trials.set(p.key, weights);
    if (redraw) {
      // One row per holding in the weights, always: rows keep their place, and a holding that comes back (the reset
      // after a removal) gets its row again, so the rows on screen always add up to 100%.
      const kept = order.filter((t) => t in weights);
      order = [...kept, ...D.sortedWeights(weights).map(([t]) => t).filter((t) => !kept.includes(t))];
      drawRows();
    }
    else {
      for (const row of rows.children) {
        const t = row.dataset.ticker; const v = weights[t] || 0;
        const input = row.querySelector("input"); const out = row.querySelector("output");
        if (t !== moving) input.value = String(Math.round(v * 100));
        input.style.setProperty("--fill", `${v * 100}%`);
        input.setAttribute("aria-valuetext", `${pct1(v)} of the money`);
        out.textContent = pct1(v);
      }
    }
    reset.disabled = D.sameWeights(weights, p.weights);
    window.clearTimeout(timer);
    timer = window.setTimeout(score, 200);
  }
  async function score() {
    const mine = ++seq;
    const clean = Object.fromEntries(Object.entries(weights).filter(([, v]) => v > 0.0005).map(([t, v]) => [t, Math.round(v * 10000) / 10000]));
    result.classList.add("is-loading");
    try {
      const [trial, base] = await Promise.all([fitFor(ctx, profile, clean), fitFor(ctx, profile, p.weights).catch(() => null)]);
      if (mine !== seq) return; // a newer move is on its way
      result.classList.remove("is-loading");
      result.replaceChildren(...whatIfResult(view, trial, base, improve, D.sameWeights(clean, p.weights) ? null : () => adopt(clean)).filter(Boolean));
    } catch (err) {
      if (mine !== seq) return;
      result.classList.remove("is-loading");
      result.replaceChildren(ctx.alertBox(err, "the fit of this mix", score));
    }
  }

  /** "Improve my score": the closest mix that fits the profile better (POST /v1/fit-score/improve), shown with the
   * before/after and each change; "Try this mix" loads it into the sliders (Back to my portfolio undoes it). */
  async function improve() {
    const from = { ...weights };
    suggestion.replaceChildren(h("p", { class: "small muted" }, "Looking for a closer fit to your profile…"));
    try {
      const { data } = await ctx.api("POST", "/v1/fit-score/improve", { weights: from });
      if (!D.sameWeights(from, weights)) return; // the mix moved meanwhile: this answer is for an older one
      suggestion.replaceChildren(...suggestionCard(view, data, {
        apply: () => {
          order = D.sortedWeights(data.weights).map(([t]) => t);
          setWeights({ ...data.weights }, true, null, true);
          suggestion.replaceChildren(note(h("strong", null, "The suggested mix is in the sliders. "),
            "Adjust it as you like; “Back to my portfolio” undoes it."));
        },
        dismiss: () => suggestion.replaceChildren(),
        close: (...parts) => note(...parts),
      }).filter(Boolean));
    } catch (err) {
      suggestion.replaceChildren(ctx.alertBox(err, "a better mix", improve));
    }
  }

  /** A one-line note in the suggestion area, with an × to close it. */
  function note(...parts) {
    return h("div", { class: "whatif__note" }, h("p", { class: "small" }, ...parts),
      closeButton(ctx, "Close this message", () => suggestion.replaceChildren()));
  }

  /** "Make this my portfolio": the mix in the sliders becomes the portfolio; the page reloads on it. */
  async function adopt(mix) {
    result.classList.add("is-loading");
    try {
      await adoptWeights(ctx, mix);
      trials.delete(p.key);
      ctx.announce("This mix is now your portfolio. AlphaForge is analysing it.");
      window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      ctx.route();
    } catch (err) {
      result.classList.remove("is-loading");
      suggestion.replaceChildren(ctx.alertBox(err, "your portfolio", () => adopt(mix)));
    }
  }

  drawRows();
  setWeights(weights, false);
  return h("section", { class: "panel stack", "aria-labelledby": "whatif-title" },
    h("div", { class: "stack-sm" },
      h("h2", { id: "whatif-title", class: "section-title" }, level === "simple" ? "Try a different mix" : "What if you changed the weights?"),
      h("p", { class: "small muted" }, "Move a slider: the others adjust so the total stays 100%. The fit with your profile is measured again each time. Nothing is saved.")),
    h("div", { class: "whatif" },
      h("div", { class: "stack-sm" }, rows,
        h("div", { class: "whatif__add" }, h("label", { class: "field__label", for: "whatif-pick" }, "Add an investment"),
          h("div", { class: "whatif__add-row" }, pick, h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: add }, "Add")), pickHelp, options),
        reset),
      result),
    suggestion);
}

function suggestionCard(view, data, { apply, dismiss, close }) {
  const { h, level } = view;
  const uni = view.uni || {};
  if (!data.found) return [close(h("strong", null, "No better mix found. "), data.message)];
  const name = (t) => (level === "simple" ? (uni[t] || {}).plain || t : t);
  return [
    h("h3", { class: "whatif__suggest-title" }, level === "simple" ? "A mix that suits you better" : "A closer fit to your profile"),
    h("p", { class: "whatif__jump" }, `${data.base.score}%`, h("span", { "aria-hidden": "true" }, " → "),
      h("span", { class: "visually-hidden" }, " becomes "), h("strong", null, `${data.best.score}%`),
      h("span", { class: "small muted" }, ` ${data.best.band}`)),
    data.steps.length ? h("ul", { class: "small answer-list" }, data.steps.map((s) => h("li", null, s))) : null,
    h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
      h("caption", { class: "visually-hidden" }, "What changes, holding by holding"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Holding"), ["Now", "Suggested"].map((x) => h("th", { scope: "col", class: "num" }, x)))),
      h("tbody", null, data.changes.map((c) => h("tr", null, h("th", { scope: "row" }, name(c.ticker)),
        h("td", { class: "num" }, c.from ? pct1(c.from) : "—"), h("td", { class: "num" }, c.to ? pct1(c.to) : "removed")))))),
    h("div", { class: "whatif__suggest-actions" },
      h("button", { class: "btn btn--primary btn--sm", type: "button", onclick: apply }, "Try this mix"),
      h("button", { class: "link-btn", type: "button", onclick: dismiss }, "Dismiss")),
    level !== "simple" ? h("p", { class: "small muted" }, data.method) : null,
  ];
}

function whatIfResult(view, fit, base, onImprove, onAdopt) {
  const { h, level } = view;
  const delta = base ? fit.score - base.score : null;
  const mix = (fit.parts.find((x) => x.key === "asset_mix") || { items: [{}] }).items[0].measured;
  const lines = D.fitLines(fit, "simple"); // only what loses points, whatever the level
  return [
    h("p", { class: "kpi__label" }, "Fit of this mix with your profile"),
    h("p", { class: "hero-card__figure" }, `${fit.score}`, h("span", { class: "hero-card__of" }, "%")),
    meter({ value: fit.score, max: 100, label: "Fit of this mix with your profile", display: `${fit.score}%`, tone: D.fitTone(fit.score) }),
    h("p", { class: "small" }, h("strong", null, fit.band),
      delta === null ? null : delta === 0 ? " · same as your portfolio"
        : ` · ${delta > 0 ? "+" : "−"}${Math.abs(delta)} points vs your portfolio (${base.score}%)`),
    ...(fit.caps || []).map((c) => h("p", { class: "small" }, h("strong", null, `Capped at ${c.at}%: `), `${c.why}.`)),
    h("dl", { class: "facts" },
      h("dt", null, level === "simple" ? "Typical yearly swing" : "Volatility (annualised)"), h("dd", null, pct1(fit.measured.ann_vol)),
      h("dt", null, "Worst drop"), h("dd", null, pct1(Math.abs(fit.measured.max_drawdown))),
      typeof mix === "number" ? [h("dt", null, "In stocks"), h("dd", null, pct(mix))] : null),
    lines.length ? h("ul", { class: "small plain-list fit-lines" }, lines.map((l) => h("li", null,
      h("strong", null, level === "simple" ? l.title : `${l.title}: −${l.lost}`), h("span", { class: "fit-lines__why" }, ` ${l.reasons.join("; ")}.`))))
      : h("p", { class: "small muted" }, "This mix meets every part of your profile."),
    h("div", { class: "whatif__actions" },
      fit.score < 100 && onImprove ? h("button", { class: "btn btn--secondary btn--sm", type: "button", onclick: onImprove },
        level === "simple" ? "Find a mix that suits me better" : "Improve my score") : null,
      // Offered when the mix in the sliders fits the profile better than the portfolio.
      onAdopt && delta !== null && delta > 0 ? h("button", { class: "btn btn--primary btn--sm", type: "button", onclick: onAdopt }, "Make this my portfolio") : null),
  ];
}

function kpis(view) {
  const { ctx, h, p, risk, level, profile } = view;
  const split = D.classSplit(p.weights, view.uni);
  const holdings = D.sortedWeights(p.weights);
  const stocks = (split.find((g) => g.key === "stocks") || { value: 0 }).value;
  const bonds = (split.find((g) => g.key === "bonds") || { value: 0 }).value;
  const tile = (label, value, sub) => h("div", { class: "kpi" },
    h("p", { class: "kpi__label" }, label), h("p", { class: "kpi__value" }, value), sub ? h("p", { class: "kpi__sub" }, sub) : null);
  const tiles = [
    tile("Holdings", String(holdings.length), level === "simple" ? "funds and companies" : `largest: ${holdings[0][0]} ${pct1(holdings[0][1])}`),
    tile(level === "simple" ? "Stocks and bonds" : "Stocks / bonds", `${pct(stocks)} / ${pct(bonds)}`,
      level === "simple" ? "stocks grow more but move more; bonds are steadier" : "share of the money, by holding"),
  ];
  if (risk.contribution) {
    tiles.push(tile(level === "simple" ? "Typical yearly swing" : "Volatility (annualised)", pct1(risk.contribution.vol),
      level === "simple" ? "how much its value usually moves up or down in a year" : "5 years of daily returns, in CAD"));
  }
  if (profile.capital) tiles.push(tile("Amount simulated", ctx.money(profile.capital), "from your profile, not real money"));
  if (level === "detailed" && risk.concentration) {
    tiles.push(tile("Effective positions", Number(risk.concentration.effective).toFixed(1), `as if spread evenly over this many companies (${risk.concentration.holdings} counted, including inside your funds)`));
    if (risk.contribution) tiles.push(tile("Diversification ratio", Number(risk.contribution.diversification).toFixed(2), "above 1: holdings offset each other"));
  }
  return h("section", { class: "kpis", "aria-label": "Key figures" }, tiles);
}

function learnNote(view, text) {
  return view.learn ? view.h("p", { class: "learn-note" }, view.h("strong", null, "What this means: "), text) : null;
}

function allocationSection(view) {
  const { ctx, h, p, level, profile } = view;
  const uni = view.uni || {};
  const holdings = D.sortedWeights(p.weights);
  const name = (t) => (level === "simple" ? (uni[t] || {}).plain || t : t);
  const sub = (t) => (level === "simple" ? t : (uni[t] || {}).plain || (uni[t] || {}).name || "");
  const amount = (w) => (profile.capital ? ` · ${ctx.money(w * profile.capital)}` : "");
  const extra = level === "detailed" && p.starting ? [
    { header: "Starting mix", value: (r) => pct1(p.starting[r.ticker] || 0) },
    { header: "Change", value: (r) => { const c = (p.changes || {})[r.ticker] || 0; return `${c >= 0 ? "+" : "−"}${pct1(Math.abs(c))}`; } }] : [];
  return h("section", { class: "panel stack", "aria-labelledby": "alloc-title" },
    h("h2", { id: "alloc-title", class: "section-title" }, level === "simple" ? "What you own" : "Allocation"),
    segmentedBar({ id: "split", title: level === "simple" ? "How your money is split" : "By asset class",
      note: "Share of the money, from the portfolio's weights", format: pct,
      parts: D.classSplit(p.weights, uni).map((g) => ({ label: g.label, value: g.value, slot: g.slot })) }),
    rankedBars({ id: "holdings", title: level === "simple" ? "Each investment" : "By holding",
      note: profile.capital ? "Share of the money, and the simulated amount" : "Share of the money",
      rows: holdings.map(([t, w]) => ({ ticker: t, label: name(t), sub: sub(t), value: w, display: `${pct1(w)}${amount(w)}` })),
      format: pct1, extraColumns: extra }),
    trendChips(view),
    level === "detailed" && p.activeConstraints.length ? h("p", { class: "small muted" }, `Rules that shaped the weights: ${p.activeConstraints.map(String).join(", ")}.`) : null,
    learnNote(view, "a fund holds many companies or bonds at once. Spreading your money over several funds means one bad company or sector can't sink the whole portfolio."));
}

function riskSection(view) {
  const { h, risk, level, riskState, riskRunId, ctx, p } = view;
  const section = h("section", { class: "panel stack", "aria-labelledby": "risk-title" },
    h("h2", { id: "risk-title", class: "section-title" }, level === "simple" ? "Where the ups and downs come from" : "Risk contribution"));
  if (!risk.contribution) {
    section.append(riskState === "measuring"
      ? h("p", { class: "building__head" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Measuring the risk of this exact mix (about a minute).")
      : h("div", { class: "notice" }, h("span", null, "The risk of this exact mix hasn't been measured yet."),
        h("button", { class: "btn btn--secondary", type: "button", onclick: () => { startJob(ctx, "risk", D.riskPrompt(p.weights)); ctx.route(); } }, "Measure it")));
    return section;
  }
  const c = risk.contribution;
  const rows = Object.keys(c.weights).sort((a, b) => (c.risk[b] || 0) - (c.risk[a] || 0))
    .map((t) => ({ label: t, sub: ((view.uni || {})[t] || {}).plain || "", a: c.weights[t], b: c.risk[t] || 0 }));
  const top = rows[0];
  section.append(h("p", null, top.b > top.a + 0.05
    ? `${level === "simple" ? (view.uni[top.label] || {}).plain || top.label : top.label} is ${pct(top.a)} of your money but ${pct(top.b)} of the ups and downs.`
    : "No holding carries much more of the ups and downs than its share of the money."));
  if (level !== "simple") {
    section.append(pairedBars({ id: "risk", title: "Share of the money vs share of the risk", format: pct1,
      note: "Each holding's contribution to the portfolio's volatility", rows,
      series: [{ name: "Share of the money", slot: 1 }, { name: "Share of the risk", slot: 2 }] }));
  }
  if (riskRunId !== p.runId) {
    section.append(h("p", { class: "small muted" }, "Measured on this exact mix in a follow-up analysis. ",
      h("a", { href: `#/runs/${riskRunId}` }, "See it")));
  }
  const note = learnNote(view, "some holdings move much more than others. A holding can be a small part of your money and still cause a big part of the swings.");
  if (note) section.append(note); // append(null) would print "null"
  return section;
}

/* ---------- Risk trend per holding (GET /v1/vol-forecast): volatility, never the direction of returns ---------- */

function trendChips(view) {
  const { ctx, h, p, level } = view;
  const tickers = D.sortedWeights(p.weights).map(([t]) => t);
  const list = h("ul", { class: "trend-chips", "aria-label": "Risk trend of each holding" });
  const wrap = h("div", { class: "stack-sm" },
    h("p", { class: "small" }, h("strong", null, level === "simple" ? "Are the ups and downs getting bigger?" : "Risk trend"),
      " ", h("span", { class: "muted" }, "Expected swings over the next month compared with the last 3 months. It describes risk, not where prices go.")),
    list);
  ctx.api("GET", `/v1/vol-forecast?tickers=${encodeURIComponent(tickers.join(","))}`).then(({ data }) => {
    list.replaceChildren(...tickers.map((t) => {
      const c = PD.trendChip(t, data.tickers[t]);
      return h("li", { class: `trend-chip trend-chip--${c.cls}`, title: c.tooltip, tabindex: "0", "aria-label": `${t}: risk ${c.word.toLowerCase()}. ${c.tooltip}` },
        h("span", { class: "trend-chip__ticker" }, t),
        c.arrow ? h("span", { class: "trend-chip__arrow", "aria-hidden": "true" }, c.arrow) : null,
        h("span", null, c.word));
    }));
  }).catch(() => { wrap.replaceChildren(h("p", { class: "small muted" }, "The risk trend of the holdings couldn't be loaded.")); });
  return wrap;
}

/* ---------- Range of possible values (POST /v1/projection): planning guidelines, not a forecast ---------- */

function projectionSection(view) {
  const { ctx, h, p, profile, level } = view;
  const defaultYears = PD.horizonFromProfile(profile.horizon_years) || 10;
  const state = { years: defaultYears, mode: "nominal", contribution: 0, data: null, seq: 0 };
  const out = h("div", { class: "stack" }, ctx.skeleton(3));
  const section = h("section", { class: "panel stack", "aria-labelledby": "proj-title" },
    h("h2", { id: "proj-title", class: "section-title" }, level === "simple" ? "What it could be worth later" : "Range of possible values"),
    h("p", { class: "disclaimer-line", role: "note" }, PD.DISCLAIMER));
  if (!profile.capital) {
    section.append(h("div", { class: "notice" }, h("span", null, "Add a simulated amount to your profile to see the range of possible values."),
      h("a", { class: "link-btn", href: "#/profile" }, "Open my profile")));
    return section;
  }
  const horizon = h("select", { class: "input input--inline", id: "proj-horizon" }, PD.horizonOptions(defaultYears).map((y) =>
    h("option", { value: String(y), selected: y === defaultYears }, `${y} year${y === 1 ? "" : "s"}${y === defaultYears ? " (your horizon)" : ""}`)));
  horizon.addEventListener("change", () => { state.years = Number(horizon.value); load(); });
  const modes = segControl(ctx, { legend: "Values shown", name: "proj-mode", value: state.mode, small: true,
    options: [["nominal", "In future dollars"], ["real", "In today's dollars"]],
    onChange: (m) => {
      state.mode = m;
      out.classList.add("is-switching"); // fades the old values out while the new ones are drawn, like the detail switch
      window.setTimeout(() => { draw(); out.classList.remove("is-switching"); }, 160);
    } });
  const contribution = h("input", { class: "input input--inline", id: "proj-contrib", type: "number", min: "0", step: "50", inputmode: "decimal", value: "0" });
  const apply = h("button", { class: "btn btn--secondary", type: "button", onclick: () => {
    const v = Math.max(0, Number(contribution.value) || 0);
    if (v !== state.contribution) { state.contribution = v; load(); }
  } }, "Update");
  section.append(h("div", { class: "proj-controls" },
    h("div", { class: "field" }, h("label", { class: "field__label", for: "proj-horizon" }, "Horizon"), horizon),
    modes,
    level === "simple" ? null : h("div", { class: "field" }, h("label", { class: "field__label", for: "proj-contrib" }, "Monthly contribution (CAD, optional)"),
      h("div", { class: "proj-inline" }, contribution, apply))), out);

  async function load() {
    const seq = (state.seq += 1);
    out.replaceChildren(ctx.skeleton(3));
    try {
      const { data } = await ctx.api("POST", "/v1/projection", { weights: p.weights, start_value: profile.capital,
        horizon_years: state.years, monthly_contribution: state.contribution });
      if (seq !== state.seq) return; // a newer request was sent meanwhile
      state.data = data;
      draw();
    } catch (err) {
      if (seq === state.seq) out.replaceChildren(ctx.alertBox(err, "the range of possible values", load));
    }
  }

  function draw() {
    const d = state.data;
    if (!d) return;
    const series = PD.fanSeries(d, state.mode);
    const geo = PD.fanGeometry(series);
    const rows = series.years.map((y, i) => [y === 0 ? "Now" : String(y), ...["p10", "p25", "p50", "p75", "p90"].map((k) => ctx.money(series[k][i]))]);
    const short = (v) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}k`);
    out.replaceChildren(...[ // parts that don't apply are null: dropped (native replaceChildren prints "null")
      h("p", { class: "proj-summary" }, PD.summarySentence(d, state.mode, ctx.money)),
      fanChart({ id: "projection", title: state.mode === "real" ? "Simulated value, in today's dollars" : "Simulated value, in future dollars",
        note: `${d.assumptions.n_paths.toLocaleString("en-CA")} simulated market scenarios, rebalanced every month`,
        geo, format: short, rows, startLabel: `Today's value (${ctx.money(d.start_value)})` }),
      d.warnings.length ? h("ul", { class: "warning-list" }, d.warnings.map((w) => h("li", null, w))) : null,
      assumptionsBlock(view, d),
      learnNote(view, "each scenario is one possible path for markets. The band shows where 8 in 10 of them ended, so your result could be anywhere in it, or outside it."),
    ].filter(Boolean));
  }

  load();
  return section;
}

/* ---------- How the portfolio reacts to rates (POST /v1/rate-exposure): shown when "Macro and rates" is on ---------- */
function ratesSection(view) {
  const { ctx, h, p, level } = view;
  const out = h("div", { class: "stack-sm" }, ctx.skeleton(3));
  const section = h("section", { class: "panel stack", "aria-labelledby": "rates-title" },
    h("h2", { id: "rates-title", class: "section-title" }, "How your portfolio reacts to rates"), out);
  (async () => {
    try {
      const { data } = await ctx.api("POST", "/v1/rate-exposure", { weights: p.weights });
      const facts = D.rateFacts(data);
      out.replaceChildren(...[ // parts that don't apply are null: dropped (native replaceChildren prints "null")
        h("p", null, D.rateSentence(data)),
        facts.length ? h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])) : null,
        level === "simple" ? null : h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
          h("caption", { class: "visually-hidden" }, "Sensitivity of each holding to a 1-point rise in yields"),
          h("thead", null, h("tr", null, ["Holding", "Share", "Sensitivity (years)", "If yields rise 1 point"].map((x) => h("th", { scope: "col" }, x)))),
          h("tbody", null, D.rateRows(data).map((r) => h("tr", null, h("th", { scope: "row" }, r.ticker),
            h("td", { class: "num" }, r.weight), h("td", { class: "num" }, r.duration), h("td", { class: "num" }, r.impact)))))),
        h("p", { class: "small muted" }, `Rates as of ${data.rates_date || data.as_of}. Only bond funds have a measured sensitivity; stocks and equity funds are not measured. `
          + "It uses the average of 2021–2026 and changes over time."),
      ].filter(Boolean));
    } catch (err) {
      out.replaceChildren(h("p", { class: "muted" }, "The rate sensitivity isn't available right now."));
    }
  })();
  return section;
}
function assumptionsBlock(view, d) {
  const { h } = view;
  const a = d.assumptions;
  const rows = PD.assumptionRows(d);
  return h("details", { class: "proj-assumptions" }, h("summary", null, "Assumptions"),
    h("div", { class: "stack-sm" },
      h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
        h("caption", { class: "visually-hidden" }, "Expected yearly return and volatility used for each holding"),
        h("thead", null, h("tr", null, ["Holding", "Category", "Yearly rate", "Source", "Volatility (year 1)"].map((x) => h("th", { scope: "col" }, x)))),
        h("tbody", null, rows.map((r) => h("tr", null, h("th", { scope: "row" }, r.ticker),
          h("td", null, r.category), h("td", { class: "num" }, r.rate), h("td", null, r.source), h("td", { class: "num" }, r.vol)))))),
      h("ul", { class: "plain-list small" },
        h("li", null, `Volatility: ${PD.volSourceText(d)}`),
        h("li", null, `Inflation: ${(a.inflation * 100).toFixed(1)}% a year. Fees: ${(a.annual_fee * 100).toFixed(2)}% a year.`),
        h("li", null, `${a.n_paths.toLocaleString("en-CA")} simulated market scenarios, rebalanced every month to the target weights.`),
        rows.some((r) => r.note) ? h("li", null, "Individual stocks get their region's equity rate (the guidelines give rates by asset class) and their own volatility.") : null,
        h("li", null, "Source of the rates: ", h("a", { href: a.source_url, rel: "noopener noreferrer", target: "_blank" }, a.source), ". ", a.horizon_note || ""))));
}

function sectorSection(view) {
  const { h, risk } = view;
  const entries = Object.entries(risk.concentration.bySector).sort((a, b) => b[1] - a[1]);
  const top = entries.slice(0, 8);
  const rest = entries.slice(8).reduce((s, [, v]) => s + v, 0);
  if (rest > 0.0005) top.push(["Other sectors", rest]);
  return h("section", { class: "panel stack", "aria-labelledby": "sector-title" },
    h("h2", { id: "sector-title", class: "section-title" }, "Exposure by sector"),
    rankedBars({ id: "sectors", title: "Share of the money by sector",
      note: risk.concentration.lookThrough ? "Includes the companies held inside your funds" : "Holdings as listed",
      rows: top.map(([s, v]) => ({ label: s, value: v })), format: pct1 }),
    learnNote(view, "a sector is a part of the economy, like banks or technology. When one sector falls, every company in it tends to fall together."));
}

function metricsSection(view) {
  const { h, risk, p } = view;
  const rows = Object.entries(risk.metrics).filter(([t]) => p.weights[t]);
  return h("section", { class: "panel stack", "aria-labelledby": "metrics-title" },
    h("h2", { id: "metrics-title", class: "section-title" }, "Each holding's history"),
    h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
      h("caption", { class: "visually-hidden" }, "Risk measures per holding, daily returns in CAD"),
      h("thead", null, h("tr", null, ["Holding", "Volatility", "Worst drop", "Daily VaR 95%", "Daily CVaR 95%", "Yearly growth"].map((x) => h("th", { scope: "col" }, x)))),
      h("tbody", null, rows.map(([t, m]) => h("tr", null, h("th", { scope: "row" }, t),
        [pct1(m.ann_vol), `${pct1(-Math.abs(m.max_drawdown))}`, pct1(m.var95_daily), pct1(m.cvar95_daily), m.cagr == null ? "n/a" : pct1(m.cagr)]
          .map((x) => h("td", { class: "num" }, x))))))),
    h("p", { class: "small muted" }, "Past results over the data window. They do not predict future returns."));
}

function correlationSection(view) {
  const { h, risk, p } = view;
  const labels = Object.keys(risk.correlation).filter((t) => p.weights[t]);
  if (labels.length < 2) return null;
  const matrix = labels.map((a) => labels.map((b) => Number((risk.correlation[a] || {})[b] ?? 0)));
  return h("section", { class: "panel stack", "aria-labelledby": "corr-title" },
    h("h2", { id: "corr-title", class: "section-title" }, "Do the holdings move together?"),
    heatmap({ id: "corr", title: "Correlation of daily returns", note: "1 = always together, 0 = unrelated, −1 = opposite",
      labels, matrix, format: (v) => v.toFixed(2) }));
}

function checksSection(view) {
  const { ctx, h, p, level } = view;
  const checks = (p.governance && p.governance.checks) || [];
  return h("section", { class: "panel stack-sm", "aria-labelledby": "checks-title" },
    h("h2", { id: "checks-title", class: "section-title" }, "Checked against your profile"),
    checks.length ? h("div", null, checks.map((c) => {
      const v = ctx.VERDICTS[c.status] || ctx.VERDICTS.UNVERIFIED;
      const value = level === "simple" ? "" : D.checkValue(c);
      return h("div", { class: "check-row" },
        h("span", { class: `verdict verdict--${v.cls}` }, ctx.icon(v.icon, 14), v.word),
        h("span", { class: "check-row__what" }, D.checkName(c.id, level === "detailed")),
        value || (level === "detailed" && c.detail) ? h("p", { class: "check-row__text" }, value, level === "detailed" && c.detail ? ` ${c.detail}` : "") : null);
    })) : h("p", { class: "muted" }, p.pending ? "Being checked against your profile (about a minute)."
      : p.chosen ? "This mix hasn't been checked against your profile yet." : "This portfolio hasn't been checked against your profile."),
    learnNote(view, "your profile sets limits, for example how big one company can be. AlphaForge's rules check every limit; a portfolio that breaks one is never proposed."));
}

const analysisOn = (profile, agent) => !(profile.agent_toggles && profile.agent_toggles[agent] === false);

// The ESG hard limit (or an ESG exclusion said in chat) makes the ESG screening run (box ④ forces it).
const esgLimit = (profile) => (profile.exclusions || []).some((e) => e.type === "controversy" || e.type === "esg_activity");

function wantsEsg(profile) {
  const prefs = (profile.preferences || []).join(" ").toLowerCase();
  return esgLimit(profile) || /esg|controvers|ethic|sustainab/.test(prefs);
}

function esgSection(view) {
  const { h, p } = view;
  return h("section", { class: "panel stack-sm", "aria-labelledby": "esg-title" },
    h("h2", { id: "esg-title", class: "section-title" }, "ESG screening"),
    p.esg.absent.length ? h("p", { class: "small" }, "Outside the reference ESG index: ", p.esg.absent.map((a) => a.ticker).join(", "),
      ". Absence is a signal to discuss, not a ban: the index does not publish its reasons.") : null,
    p.esg.controversies.length ? h("ul", { class: "answer-list" }, p.esg.controversies.map((c) => h("li", null,
      h("span", null, h("strong", null, `${c.ticker} · ${c.severity} · ${c.status}`), h("br"), c.description)))) : null,
    h("p", { class: "small muted" }, "Companies screened in this analysis, including those held inside your funds."));
}

function newsSection(view) {
  const { h, p, level } = view;
  const items = p.news.slice(0, level === "detailed" ? 8 : 4);
  return h("section", { class: "panel stack-sm", "aria-labelledby": "news-title" },
    h("h2", { id: "news-title", class: "section-title" }, "Filings and rate decisions used"),
    h("ul", { class: "answer-list" }, items.map((n) => h("li", null, h("span", null,
      h("span", { class: "small muted num" }, n.date), " ",
      n.url ? h("a", { href: n.url, rel: "noopener noreferrer", target: "_blank" }, n.title) : n.title)))));
}

function explanationSection(view) {
  const { h, p, level } = view;
  const a = p.answer;
  const heading = p.chosen ? "What the analysis found" : "Why this portfolio";
  if (!a) {
    return h("section", { class: "panel" }, h("h2", { class: "section-title" }, heading),
      h("p", { class: "muted" }, p.pending ? "The explanation is being written (about a minute)."
        : p.answerError ? "The explanation could not be written this time." : "No explanation was written for this portfolio."));
  }
  const max = level === "simple" ? 3 : level === "standard" ? 5 : 8;
  return h("section", { class: "panel stack-sm", "aria-labelledby": "why-title" },
    h("h2", { id: "why-title", class: "section-title" }, heading),
    level === "detailed" && a.short_answer ? h("p", null, a.short_answer) : null,
    a.key_points.length ? h("ul", { class: "answer-list" }, a.key_points.slice(0, max).map((k) => h("li", null, k.text))) : null,
    a.glossary && a.glossary.length ? h("div", { class: "answer__section" }, h("h3", null, "Words used here"),
      h("dl", { class: "glossary" }, a.glossary.map((g) => [h("dt", null, g.term), h("dd", null, g.meaning)]))) : null,
    level !== "simple" && a.limitations.length ? h("div", { class: "answer__section" }, h("h3", null, "Limits to keep in mind"),
      h("ul", { class: "answer-list" }, a.limitations.map((k) => h("li", null, k.text)))) : null,
    h("p", { class: "small" }, h("a", { href: `#/runs/${p.runId}` }, "Read the full explanation, with every source")),
    a.disclaimer ? h("p", { class: "small muted" }, a.disclaimer) : null);
}

function history(ctx, runs, currentId = null) {
  const { h } = ctx;
  const STATUS = { DONE: "Done", FAILED: "Stopped", RUNNING: "Running", QUEUED: "Planned" };
  const kind = (r) => (r.question === D.PORTFOLIO_PROMPT ? "Portfolio" : (r.question || "").startsWith(D.OPTIMIZE_PROMPT_PREFIX) ? "Optimization"
    : (r.question || "").startsWith(D.RISK_PROMPT_PREFIX) ? "Analysis" : "Question");
  return h("section", { class: "panel panel--plain stack-sm", "aria-labelledby": "history-title" },
    h("h2", { id: "history-title", class: "section-title" }, "Your analyses"),
    (runs || []).length ? h("ul", { class: "plain-list history" }, runs.slice(0, 12).map((r) => h("li", null,
      h("a", { href: `#/runs/${r.run_id}`, "aria-current": r.run_id === currentId ? "true" : null },
        h("span", { class: "recent__q" }, r.question),
        h("span", { class: "small muted" }, `${kind(r)} · ${STATUS[r.status] || r.status} · ${ctx.timeText(r.created_at)}`)))))
      : h("p", { class: "muted" }, "Your analyses will appear here."),
    h("a", { class: "link-btn", href: "#/ask" }, "Ask a question about your portfolio"));
}
