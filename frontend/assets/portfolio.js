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
import { meter, pairedBars, rankedBars, segmentedBar, heatmap } from "./charts.js";

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
 * Runs when a build finishes, when the dashboard shows a portfolio and before the chat sends a question. */
export async function syncPortfolio(ctx) {
  const profile = await ctx.loadProfile(true);
  if (!profile) return null;
  const runs = (await ctx.api("GET", "/v1/runs?limit=100")).data;
  const done = D.findRun(runs, D.PORTFOLIO_PROMPT);
  if (!done || profile.portfolio_run_id === done.run_id) return profile.portfolio || null;
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
  if (before && !D.profileChanged(before, after)) return null;
  return startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT);
}

/* ---------- Page ---------- */

const DETAIL_KEY = (ctx) => `af.detail.${ctx.actorId()}`;
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
  main.append(
    ctx.band("head", h("div", { class: "page-head page-head--split" },
      h("div", { class: "stack-sm" },
        h("h1", null, "Your portfolio"),
        h("p", null, "The simulated portfolio AlphaForge builds from your profile, with what it holds, how risky it is and why it fits you. Simulation only, not advice.")),
      detailSlot)),
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

    const job = jobFor(ctx, "portfolio");
    const done = D.findRun(runs, D.PORTFOLIO_PROMPT);
    const building = (job && !job.done) || (!(job && job.done) && D.pendingRun(runs, D.PORTFOLIO_PROMPT));
    if (!done) {
      if (job && job.done && (job.error || (job.run && job.run.status !== "DONE"))) {
        body.replaceChildren(buildFailed(ctx, job), history(ctx, runs));
        return;
      }
      if (!building && autoOnce(ctx, "portfolio")) startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT);
      const now = jobFor(ctx, "portfolio");
      body.replaceChildren(now && !now.done ? buildingPanel(ctx) : notBuilt(ctx), history(ctx, runs));
      return;
    }

    let run; let records;
    try {
      [run, records] = await Promise.all([
        ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}`).then((r) => r.data),
        ctx.api("GET", `/v1/runs/${encodeURIComponent(done.run_id)}/evidence`).then((r) => r.data)]);
    } catch (err) {
      body.replaceChildren(ctx.alertBox(err, "your portfolio", draw));
      return;
    }
    const p = D.extractPortfolio(run, records);
    if (!p.weights) {
      body.replaceChildren(noWeights(ctx, p, run), history(ctx, runs));
      return;
    }
    // This is now the user's portfolio: saved in the profile, so a question about "my portfolio" is analysed on it.
    if (profile.portfolio_run_id !== p.runId) syncPortfolio(ctx).catch(() => { /* retried on the next visit or question */ });

    // Risk of these exact weights: from this run when it measured them, else from the follow-up question.
    let risk = p.risk; let riskRunId = run.run_id; let riskState = risk.matches ? "ready" : "missing";
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

    const view = { ctx, h, profile, p, risk, riskState, riskRunId, level, learn: Boolean(profile.wants_to_learn) && level !== "detailed", uni };
    body.classList.remove("is-switching");
    body.replaceChildren(...[ // sections that don't apply are null: dropped, never rendered as text
      ...banners(view, building),
      heroCard(view),
      kpis(view),
      allocationSection(view),
      riskSection(view),
      level !== "simple" && risk.concentration ? sectorSection(view) : null,
      level === "detailed" && risk.metrics ? metricsSection(view) : null,
      level === "detailed" && risk.correlation ? correlationSection(view) : null,
      checksSection(view),
      // The analyses switched off in the profile are not shown (they don't change the mix: no rebuild needed).
      level !== "simple" && p.esg && wantsEsg(profile) && analysisOn(profile, "esg") ? esgSection(view) : null,
      level !== "simple" && p.news.length && (analysisOn(profile, "news") || analysisOn(profile, "fundamentals")) ? newsSection(view) : null,
      explanationSection(view),
      history(ctx, runs, run.run_id)].filter(Boolean));
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

function notBuilt(ctx) {
  const { h } = ctx;
  return h("section", { class: "panel empty" },
    h("h2", { class: "section-title" }, "No portfolio yet"),
    h("p", { class: "lede" }, "AlphaForge builds it from your profile in about a minute."),
    h("button", { class: "btn btn--primary", type: "button", onclick: () => { startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT); ctx.route(); } }, "Build my portfolio"));
}

function buildFailed(ctx, job) {
  const { h } = ctx;
  const retry = () => { startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT); ctx.route(); };
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
    h("h2", { class: "section-title" }, planOnly ? "The analysis agents aren't running" : "Your portfolio couldn't be built this time"),
    h("p", { class: "lede" }, planOnly
      ? "This server only plans analyses (AF_DISPATCH=none), so no portfolio can be built here."
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
  const { ctx, h, p, profile } = view;
  const out = [];
  if (building) {
    out.push(h("div", { class: "banner", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      h("span", null, "A new portfolio is being built from your updated profile. This page updates when it's ready.")));
  } else if (D.profileChanged(p.profileSnapshot, profile)) {
    out.push(h("div", { class: "banner banner--action" },
      h("span", null, "Your profile changed since this portfolio was built."),
      h("button", { class: "btn btn--primary", type: "button", onclick: () => { startJob(ctx, "portfolio", D.PORTFOLIO_PROMPT); ctx.route(); } }, "Rebuild my portfolio")));
  }
  return out;
}

/** The Simple / Standard / Detailed switch: a capsule whose thumb slides to the chosen option. It is created once
 * per page and updated in place, so the slide animates and focus stays on the option. */
export function detailControl(ctx, level, onChange) {
  const { h } = ctx;
  const thumb = h("span", { class: "seg-control__thumb", "aria-hidden": "true" });
  const options = h("div", { class: "seg-control__options" }, thumb, D.LEVELS.map((l) => h("label", { class: "seg-control__option" },
    h("input", { type: "radio", name: "detail-level", value: l, checked: l === level, onchange: () => { place(); onChange(l); } }),
    h("span", null, D.LEVEL_LABELS[l]))));
  const el = h("fieldset", { class: "seg-control" }, h("legend", null, "How much detail"), options);
  function place() {
    const on = options.querySelector("input:checked");
    if (!on) return;
    const label = on.closest("label");
    thumb.style.width = `${label.offsetWidth}px`;
    thumb.style.transform = `translateX(${label.offsetLeft - 2}px)`;
  }
  el.update = (l) => { const input = options.querySelector(`input[value="${l}"]`); if (input) input.checked = true; place(); };
  // First placement without a slide (the thumb appears under the current level), then slides on every change.
  requestAnimationFrame(() => { place(); requestAnimationFrame(() => options.classList.add("is-ready")); });
  window.addEventListener("resize", place);
  return el;
}

/* ---------- Sections ---------- */

const verdictWord = (v) => ({ PASS: "Fits your profile", WARN: "Fits, with warnings", BLOCK: "Breaks a rule of your profile", UNVERIFIED: "Some rules couldn't be checked" })[v] || "Not checked";

function heroCard(view) {
  const { ctx, h, p, risk, level } = view;
  const v = (p.governance && p.governance.verdict) || "UNVERIFIED";
  const vs = ctx.VERDICTS[v] || ctx.VERDICTS.UNVERIFIED;
  const fit = D.fitScore(p.governance, risk.contribution);
  const short = p.answer && p.answer.short_answer;
  return h("section", { class: "panel hero-card", "aria-labelledby": "hero-title" },
    h("div", { class: "hero-card__main stack-sm" },
      h("span", { class: `verdict verdict--${vs.cls}` }, ctx.icon(vs.icon, 14), verdictWord(v)),
      h("h2", { id: "hero-title", class: "hero-card__title" }, level === "simple" ? "A mix chosen for you" : "Your simulated portfolio"),
      short ? h("p", { class: "hero-card__answer" }, short) : null,
      h("p", { class: "small muted" }, `Built from your profile on ${ctx.timeText(p.builtAt)} \u00B7 `,
        h("a", { href: `#/runs/${p.runId}` }, "See how it was built and its sources"))),
    h("div", { class: "hero-card__score" },
      h("p", { class: "kpi__label" }, "Fit with your rules"),
      h("p", { class: "hero-card__figure" }, countUp(h("span", null, `${fit.score}`), fit.score), h("span", { class: "hero-card__of" }, " / 10")),
      meter({ value: fit.score, max: 10, label: "Fit with your rules", display: `${fit.score} out of 10`, tone: fit.score >= 8.5 ? "good" : fit.score >= 6.5 ? "ok" : "bad" }),
      h("p", { class: "small" }, fit.band),
      level !== "simple" && fit.why.length ? h("ul", { class: "small plain-list" }, fit.why.map((w) => h("li", null, `${w.reason} (${w.points})`))) : null,
      level !== "simple" && !fit.why.length ? h("p", { class: "small muted" }, "No rule warns or blocks: no point is taken off.") : null));
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
    tiles.push(tile("Effective positions", Number(risk.concentration.effective).toFixed(1), `1/HHI, ${risk.concentration.holdings} companies looked through`));
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

function sectorSection(view) {
  const { h, risk } = view;
  const entries = Object.entries(risk.concentration.bySector).sort((a, b) => b[1] - a[1]);
  const top = entries.slice(0, 8);
  const rest = entries.slice(8).reduce((s, [, v]) => s + v, 0);
  if (rest > 0.0005) top.push(["Other sectors", rest]);
  return h("section", { class: "panel stack", "aria-labelledby": "sector-title" },
    h("h2", { id: "sector-title", class: "section-title" }, "Exposure by sector"),
    rankedBars({ id: "sectors", title: "Share of the money by sector",
      note: risk.concentration.lookThrough ? "Funds are looked through to the companies they hold" : "Holdings as listed",
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
    })) : h("p", { class: "muted" }, "No governance check was recorded for this portfolio."),
    learnNote(view, "your profile sets limits, for example how big one company can be. AlphaForge's rules check every limit; a portfolio that breaks one is never proposed."));
}

const analysisOn = (profile, agent) => !(profile.agent_toggles && profile.agent_toggles[agent] === false);

function wantsEsg(profile) {
  const prefs = (profile.preferences || []).join(" ").toLowerCase();
  return (profile.exclusions || []).some((e) => e.type === "controversy" || e.type === "esg_activity")
    || /esg|controvers|ethic|sustainab/.test(prefs);
}

function esgSection(view) {
  const { h, p } = view;
  return h("section", { class: "panel stack-sm", "aria-labelledby": "esg-title" },
    h("h2", { id: "esg-title", class: "section-title" }, "ESG and controversies"),
    p.esg.absent.length ? h("p", { class: "small" }, "Outside the reference ESG index: ", p.esg.absent.map((a) => a.ticker).join(", "),
      ". Absence is a signal to discuss, not a ban: the index does not publish its reasons.") : null,
    p.esg.controversies.length ? h("ul", { class: "answer-list" }, p.esg.controversies.map((c) => h("li", null,
      h("span", null, h("strong", null, `${c.ticker} · ${c.severity} · ${c.status}`), h("br"), c.description)))) : null,
    h("p", { class: "small muted" }, "Companies screened by the ESG analysis of this run, including those held through your funds."));
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
  if (!a) {
    return h("section", { class: "panel" }, h("h2", { class: "section-title" }, "Why this portfolio"),
      h("p", { class: "muted" }, p.answerError ? "The explanation could not be written this time." : "No explanation was written for this portfolio."));
  }
  const max = level === "simple" ? 3 : level === "standard" ? 5 : 8;
  return h("section", { class: "panel stack-sm", "aria-labelledby": "why-title" },
    h("h2", { id: "why-title", class: "section-title" }, "Why this portfolio"),
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
  const kind = (r) => (r.question === D.PORTFOLIO_PROMPT ? "Portfolio" : (r.question || "").startsWith(D.RISK_PROMPT_PREFIX) ? "Risk check" : "Question");
  return h("section", { class: "panel panel--plain stack-sm", "aria-labelledby": "history-title" },
    h("h2", { id: "history-title", class: "section-title" }, "Your analyses"),
    (runs || []).length ? h("ul", { class: "plain-list history" }, runs.slice(0, 12).map((r) => h("li", null,
      h("a", { href: `#/runs/${r.run_id}`, "aria-current": r.run_id === currentId ? "true" : null },
        h("span", { class: "recent__q" }, r.question),
        h("span", { class: "small muted" }, `${kind(r)} · ${STATUS[r.status] || r.status} · ${ctx.timeText(r.created_at)}`)))))
      : h("p", { class: "muted" }, "Your analyses will appear here."),
    h("a", { class: "link-btn", href: "#/ask" }, "Ask a question about your portfolio"));
}
