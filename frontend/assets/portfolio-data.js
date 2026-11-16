/* Portfolio dashboard, data side (no DOM: tested with node, tests/js/portfolio-data.test.mjs).
 *
 * "Your portfolio" is built by Fitfolio from the profile: a normal run of the pipeline with the question
 * PORTFOLIO_PROMPT (task allocation_proposal: the optimizer finds the weights, governance checks them, the
 * Explainer explains them). Every figure on the dashboard comes from that run's evidence ledger
 * (GET /v1/runs/{id}/evidence) or from plain arithmetic on its weights (shares, amounts): nothing is estimated here.
 *
 * Risk figures count as the portfolio's only when the tool measured these exact weights (weightsMatch). When the
 * run measured another mix (sub-agents run in parallel in graph mode), the dashboard asks one follow-up question,
 * riskPrompt(weights), and reads the risk from that run instead. */

export const PORTFOLIO_PROMPT = "Build a simulated portfolio that fits my profile.";
export const RISK_PROMPT_PREFIX = "Check the risk of my portfolio: ";
// "Optimize my portfolio": an allocation run that starts from what the user holds (Profile.holdings).
export const OPTIMIZE_PROMPT_PREFIX = "Rebalance my portfolio so it fits my profile: ";
// Profile.portfolio_run_id when the user picked the weights (holdings, or a what-if mix): "chosen:<latest build id>".
export const CHOSEN_PREFIX = "chosen:";
export const TOP_LEVEL = new Set(["orchestrator", "synthesis", null, undefined, ""]);

/* ---------- Ledger records ---------- */

export const toolOf = (r) => String(r.tool || "").split("___").pop(); // "metrics___metrics" (Gateway) -> "metrics"
export function bodyOf(r) {
  const p = r.payload;
  return Array.isArray(p) && p.length === 1 ? p[0] : p;
}
export function outputOf(r) {
  const b = bodyOf(r);
  if (!b || typeof b !== "object") return {};
  return b.output && typeof b.output === "object" ? b.output : b;
}
const ok = (r) => !r.error;
const byTool = (records, tool) => records.filter((r) => ok(r) && toolOf(r) === tool);
const last = (list) => (list.length ? list[list.length - 1] : null);

/* ---------- Weights ---------- */

export function cleanWeights(w) {
  return Object.fromEntries(Object.entries(w || {}).filter(([, v]) => typeof v === "number" && v > 0.0005));
}
export function weightsMatch(a, b, tol = 0.01) {
  const x = cleanWeights(a); const y = cleanWeights(b);
  const keys = new Set([...Object.keys(x), ...Object.keys(y)]);
  if (!keys.size) return false;
  for (const k of keys) if (Math.abs((x[k] || 0) - (y[k] || 0)) > tol) return false;
  return true;
}
export const sortedWeights = (w) => Object.entries(cleanWeights(w)).sort((p, q) => q[1] - p[1]);
const pct2 = (v) => `${(v * 100).toFixed(2).replace(/\.?0+$/, "")}%`;

/** The follow-up question that measures the risk of exactly these weights. */
export function riskPrompt(weights) {
  return RISK_PROMPT_PREFIX + sortedWeights(weights).map(([t, v]) => `${t} ${pct2(v)}`).join(", ") + ".";
}

/** The allocation question that optimizes these holdings. */
export function optimizePrompt(holdings) {
  return OPTIMIZE_PROMPT_PREFIX + sortedWeights(holdings).map(([t, v]) => `${t} ${pct2(v)}`).join(", ") + ".";
}

/** Holdings typed by the user ([{ticker, percent}]) as weights that add up to 1. Every problem is named. */
export function holdingsFromRows(rows, universe = {}) {
  const errors = []; const weights = {};
  for (const { ticker, percent } of rows || []) {
    const t = String(ticker || "").trim().toUpperCase();
    const v = Number(percent);
    if (!t && !percent) continue;
    if (!t) { errors.push("A row has a percentage but no investment."); continue; }
    if (!universe[t]) { errors.push(`“${t}” isn't in the list of investments Fitfolio has data for.`); continue; }
    if (!(v > 0) || v > 100) { errors.push(`${t}: give a percentage between 0 and 100.`); continue; }
    weights[t] = (weights[t] || 0) + v;
  }
  const total = Object.values(weights).reduce((a, b) => a + b, 0);
  if (!errors.length && !Object.keys(weights).length) errors.push("Add at least one investment.");
  if (!errors.length && Math.abs(total - 100) > 0.5) errors.push(`The percentages add up to ${Math.round(total * 10) / 10}%, not 100%.`);
  if (errors.length) return { weights: null, total, errors };
  return { weights: Object.fromEntries(Object.entries(weights).map(([t, v]) => [t, Math.round((v / total) * 10000) / 10000])), total, errors };
}

/* ---------- Runs ---------- */

const FINAL = new Set(["DONE", "FAILED"]);
export const isFinal = (status) => FINAL.has(status);

/** Newest run of the list (GET /v1/runs is newest first) whose question starts with `prefix`. */
export function findRun(runs, question, { exact = false, done = true } = {}) {
  return (runs || []).find((r) => (exact ? r.question === question : (r.question || "").startsWith(question))
    && (!done || r.status === "DONE")) || null;
}
/** A run still in progress: RUNNING, or QUEUED for less than two minutes (longer means this server only plans
 * analyses, AF_DISPATCH=none, and it will never start). */
export function inProgress(r, now = Date.now()) {
  if (r.status === "RUNNING") return true;
  return r.status === "QUEUED" && now - Date.parse(r.created_at || 0) < 120_000;
}
export const pendingRun = (runs, question, exact = false, now = Date.now()) => (runs || []).find((r) =>
  (exact ? r.question === question : (r.question || "").startsWith(question)) && inProgress(r, now)) || null;

/** A run that builds a portfolio: from the profile (PORTFOLIO_PROMPT) or from the holdings (optimizePrompt). */
export const isBuild = (r) => (r.question || "").startsWith(PORTFOLIO_PROMPT) || (r.question || "").startsWith(OPTIMIZE_PROMPT_PREFIX);
export const findBuild = (runs) => (runs || []).find((r) => isBuild(r) && r.status === "DONE") || null;
export const pendingBuild = (runs, now = Date.now()) => (runs || []).find((r) => isBuild(r) && inProgress(r, now)) || null;

/** The marker saved when the user picks the weights: remembers which build was the latest at that moment. */
export const chosenMarker = (build) => CHOSEN_PREFIX + (build ? build.run_id : "");
/** The user's own pick is still their portfolio: no build has finished since they picked it. */
export function chosenIsCurrent(profile, build) {
  const m = (profile && profile.portfolio_run_id) || "";
  return m.startsWith(CHOSEN_PREFIX) && m.slice(CHOSEN_PREFIX.length) === (build ? build.run_id : "")
    && Object.keys(cleanWeights(profile.portfolio)).length > 0;
}

/** Did the profile change, in what shapes a portfolio, since the run was made? */
const SHAPING = ["risk_band", "horizon_years", "capital", "loss_capacity", "exclusions", "preferences", "constraints"];
export function profileChanged(snapshot, profile) {
  if (!snapshot || !profile) return false;
  const norm = (v) => JSON.stringify(v ?? null);
  return SHAPING.some((k) => norm(snapshot[k]) !== norm(profile[k]));
}

/* ---------- The portfolio, from a run's evidence ---------- */

function governanceOf(r) {
  if (!r) return null;
  const b = bodyOf(r) || {};
  return { verdict: b.verdict, checks: b.checks || [], weights: cleanWeights((b.inputs || {}).weights),
           evidenceId: b.evidence_id || r.evidence_id, agent: r.agent || "orchestrator" };
}

/** Everything the dashboard shows about the portfolio run. `null` weights = no portfolio could be built. */
export function extractPortfolio(run, records) {
  const recs = records || [];
  const optimizes = byTool(recs, "optimize");
  const okOpt = last(optimizes.filter((r) => outputOf(r).status === "ok"));
  const lastOpt = last(optimizes);
  const alloc = last(byTool(recs, "allocation_subagent"));
  let weights = null; let source = null;
  if (okOpt) { weights = cleanWeights(outputOf(okOpt).weights); source = okOpt; }
  else if (alloc && Object.keys(cleanWeights(outputOf(alloc).weights)).length) {
    weights = cleanWeights(outputOf(alloc).weights); source = alloc;
  }
  const opt = okOpt ? outputOf(okOpt) : lastOpt ? outputOf(lastOpt) : null;

  const govs = byTool(recs, "governance_check");
  const topGov = last(govs.filter((r) => TOP_LEVEL.has(r.agent)));
  const governance = governanceOf(topGov || last(govs));

  return {
    runId: run.run_id,
    builtAt: run.updated_at || run.created_at,
    status: run.status,
    profileSnapshot: run.profile || null,
    weights,
    weightsEvidence: source ? (bodyOf(source) || {}).evidence_id || source.evidence_id : null,
    starting: opt ? cleanWeights(opt.requested_weights) : null,
    changes: opt && opt.changes ? opt.changes : null,
    activeConstraints: opt ? opt.active_constraints || [] : [],
    infeasible: opt && opt.status && opt.status !== "ok" && !weights
      ? { message: opt.message || "", conflicts: opt.conflicts || [] } : null,
    governance,
    risk: riskOf(recs, weights),
    esg: esgOf(recs),
    news: newsOf(recs),
    answer: (run.result || {}).answer || null,
    answerError: (run.result || {}).answer_error || null,
  };
}

/** A portfolio the user chose (their holdings, a what-if mix), analysed by the run riskPrompt(weights) when it exists.
 * Same shape as extractPortfolio; governance counts only when it checked these exact weights. */
export function analysedPortfolio(weights, run, records) {
  const recs = records || [];
  const w = cleanWeights(weights);
  const govs = byTool(recs, "governance_check").filter((r) => weightsMatch(governanceOf(r).weights, w));
  const gov = last(govs.filter((r) => TOP_LEVEL.has(r.agent))) || last(govs);
  return {
    runId: run ? run.run_id : null,
    builtAt: run ? run.updated_at || run.created_at : null,
    status: run ? run.status : null,
    chosen: true,
    profileSnapshot: null,
    weights: w,
    weightsEvidence: null, starting: null, changes: null, activeConstraints: [], infeasible: null,
    governance: gov ? governanceOf(gov) : null,
    risk: riskOf(recs, w),
    esg: esgOf(recs),
    news: newsOf(recs),
    answer: run ? (run.result || {}).answer || null : null,
    answerError: run ? (run.result || {}).answer_error || null : null,
  };
}

/** Risk evidence of a run: kept only when measured on `weights` (when given). */
export function riskOf(records, weights) {
  const recs = records || [];
  const rc = last(byTool(recs, "risk_contribution"));
  const conc = last(byTool(recs, "concentration"));
  const met = last(byTool(recs, "metrics"));
  const cor = last(byTool(recs, "correlation"));
  const rco = rc ? outputOf(rc) : null;
  const measured = rco ? cleanWeights(rco.weights) : null;
  const concWeights = conc ? cleanWeights(((bodyOf(conc) || {}).inputs || {}).weights) : null;
  const matches = Boolean(measured && (!weights || weightsMatch(measured, weights)));
  const concMatches = Boolean(conc && (!weights || !Object.keys(concWeights || {}).length || weightsMatch(concWeights, weights)));
  return {
    matches,
    measuredWeights: measured,
    contribution: matches ? { weights: measured, risk: rco.risk_contribution || {}, vol: rco.portfolio_vol,
      diversification: rco.diversification_ratio, evidenceId: (bodyOf(rc) || {}).evidence_id || rc.evidence_id } : null,
    concentration: conc && concMatches ? (() => {
      const o = outputOf(conc);
      return { bySector: o.by_sector || {}, byAssetClass: o.by_asset_class || {}, byCurrency: o.by_currency || {},
        hhi: o.hhi, effective: o.effective_positions, holdings: o.n_positions, lookThrough: o.look_through,
        evidenceId: (bodyOf(conc) || {}).evidence_id || conc.evidence_id };
    })() : null,
    metrics: met ? Object.fromEntries(Object.entries(outputOf(met)).filter(([, v]) => v && typeof v === "object" && "ann_vol" in v)) : null,
    correlation: cor && outputOf(cor).matrix ? outputOf(cor).matrix : null,
  };
}

function esgOf(records) {
  const r = last(byTool(records, "esg_screen"));
  if (!r) return null;
  const o = outputOf(r);
  const absent = Object.entries(o.membership || {}).filter(([, m]) => m && m.status === "absent_from_esg_index")
    .map(([t, m]) => ({ ticker: t, wording: m.wording || "" }));
  const controversies = (o.controversies || []).map((c) => ({ ticker: c.ticker, severity: c.severity, status: c.status,
    description: c.description || c.category || "" }));
  return { absent, controversies, say: (bodyOf(r) || {}).say || "" };
}

function newsOf(records) {
  const hits = []; const seen = new Set();
  for (const r of records) {
    if (!ok(r) || !["news_search", "search_news"].includes(toolOf(r))) continue;
    const o = outputOf(r);
    for (const e of [...(o.events || []), ...(o.hits || [])]) {
      const date = e.published_at || e.published_date || "";
      const key = `${e.title}|${date}`;
      if (seen.has(key)) continue;
      seen.add(key);
      hits.push({ date, title: e.title || "", url: e.source_url || "", source: e.source || e.origin || "" });
    }
  }
  return hits.sort((a, b) => (a.date < b.date ? 1 : -1));
}

/* ---------- Plain arithmetic on the weights ---------- */

export const CLASS_GROUPS = [
  { key: "stocks", label: "Stocks", classes: ["equity", "etf_equity"], slot: 1 },
  { key: "bonds", label: "Bonds", classes: ["etf_bond"], slot: 2 },
  { key: "gold", label: "Gold", classes: ["etf_commodity"], slot: 3 },
  { key: "crypto", label: "Crypto", classes: ["crypto"], slot: 4 },
  { key: "other", label: "Other", classes: [], slot: 5 },
];
export function classSplit(weights, universe) {
  const out = Object.fromEntries(CLASS_GROUPS.map((g) => [g.key, 0]));
  for (const [t, v] of Object.entries(cleanWeights(weights))) {
    const cls = ((universe || {})[t] || {}).class;
    const g = CLASS_GROUPS.find((x) => x.classes.includes(cls)) || CLASS_GROUPS[CLASS_GROUPS.length - 1];
    out[g.key] += v;
  }
  return CLASS_GROUPS.map((g) => ({ ...g, value: out[g.key] })).filter((g) => g.value > 0.0005);
}

/* ---------- Profile checks, in plain words ---------- */

const CHECK_NAMES = {
  SINGLE_POSITION: { plain: "No single company is too big", tech: "Largest single position" },
  SECTOR: { plain: "No sector is too big", tech: "Largest sector" },
  CRYPTO: { plain: "Crypto stays within your limit", tech: "Crypto weight" },
  SHORT_HORIZON_VOLATILITY: { plain: "Not too many big-swing assets for your horizon", tech: "High-volatility weight (short horizon)" },
  SHORT_HORIZON_DEFENSIVE: { plain: "Enough steady assets for your horizon", tech: "Defensive weight (short horizon)" },
  EXCLUSION_CONTROVERSY: { plain: "No company with a major controversy", tech: "Controversy exclusion" },
  EXCLUSION_ESG_FLAGS: { plain: "No company on an exclusion list", tech: "Exclusion lists" },
  ESG_INDEX_PREFERENCE: { plain: "Companies in an ESG index, as you prefer", tech: "ESG index preference" },
};
export function checkName(id, technical = false) {
  const known = CHECK_NAMES[id];
  if (known) return technical ? known.tech : known.plain;
  const m = /^EXCLUSION_(?:ESG_)?(.+)$/.exec(id || "");
  if (m) {
    const what = m[1].toLowerCase().replace(/_/g, " ");
    return technical ? `Exclusion: ${what}` : `Nothing from your excluded list: ${what}`;
  }
  return String(id || "Rule").toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
export function checkValue(c) {
  const fmt = (v) => (typeof v === "number" ? `${(v * 100).toFixed(1).replace(/\.0$/, "")}%` : Array.isArray(v) ? (v.length ? v.join(", ") : "none") : v ?? "");
  if (typeof c.value === "number" && typeof c.limit === "number") return `${fmt(c.value)} (limit ${fmt(c.limit)})`;
  if (Array.isArray(c.value)) return c.value.length ? `Found: ${c.value.join(", ")}` : "None found";
  return "";
}

/**
 * The fit score (POST /v1/fit-score: out of 100, measured on the server from the profile and the exact weights) as
 * lines for the level. Simple: only the parts that lost points, in plain words. Standard: every part with its points.
 * Detailed: every part, and each rule with its status. Each line: { title, points, max, reasons: [text] }.
 */
export function fitLines(fit, level = "standard") {
  if (!fit || !Array.isArray(fit.parts)) return [];
  const round = (x) => Math.round(x * 10) / 10;
  return fit.parts.map((part) => {
    const lost = part.items.filter((i) => i.lost > 0.05);
    const reasons = part.key === "rules"
      ? (level === "detailed" ? part.items.map((i) => `${checkName(i.id, true)}: ${i.why}`)
        : lost.map((i) => `${checkName(i.id)}: ${i.why}`))
      : (level === "simple" ? lost : part.items).map((i) => i.why);
    return { key: part.key, title: part.title, points: round(part.points), max: part.max, lost: round(part.max - part.points), reasons };
  }).filter((l) => level !== "simple" || l.lost > 0.05);
}
export const fitTone = (score) => (score >= 85 ? "good" : score >= 70 ? "ok" : "bad");

/* ---------- The summary at the top, in the words of each level ---------- */

const RISK_WORDS = { low: "low", low_moderate: "low-to-moderate", moderate: "moderate", high: "high" };
const pctText = (v, digits = 0) => `${(v * 100).toFixed(digits).replace(/\.0$/, "")}%`;
const listText = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/**
 * The portfolio told in a few sentences, written here from the same numbers as the charts, so it follows the level
 * (simple, standard, detailed) the moment it changes. The Explainer's text, written once at build time, can't.
 * `learn` (wants to learn, not detailed) adds one sentence that explains the idea behind the numbers.
 * Returns { text, learn }: learn is null when the person didn't ask to learn.
 */
// `own`: the user's own mix (holdings or a what-if pick), not one Fitfolio chose. No governance yet: no verdict sentence.
export function heroSummary({ level, learn = false, weights, universe = {}, governance, vol = null, profile = {}, own = false }) {
  const holdings = sortedWeights(weights);
  const split = Object.fromEntries(classSplit(weights, universe).map((g) => [g.key, g.value]));
  const stocks = split.stocks || 0; const bonds = split.bonds || 0;
  const plain = (t) => (universe[t] || {}).plain || t;
  const verdict = (governance && governance.verdict) || "UNVERIFIED";
  const flagged = ((governance && governance.checks) || []).filter((c) => c.status === "WARN" || c.status === "BLOCK" || c.status === "UNVERIFIED");
  const risk = RISK_WORDS[profile.risk_band];
  const years = Number(profile.horizon_years) || null;
  const out = [];

  if (level === "simple") {
    const top = holdings.slice(0, 2).map(([t, w]) => `${plain(t)} (${pctText(w)})`);
    out.push(`Your money is spread over ${holdings.length} investment${holdings.length === 1 ? "" : "s"}, mostly ${listText(top)}.`);
    if (stocks || bonds) out.push(`About ${pctText(stocks)} is in stocks and ${pctText(bonds)} in bonds${risk && !own ? `, a mix chosen for your ${risk} risk level` : ""}.`);
    if (vol) out.push(`In a typical year, its value could go up or down by about ${pctText(vol)}.`);
    if (governance) out.push({ PASS: "It respects every rule in your profile.",
      WARN: `It fits your profile, with ${flagged.length === 1 ? "one point" : "a few points"} to keep an eye on: ${listText(flagged.map((c) => checkName(c.id).toLowerCase()))}.`,
      BLOCK: `It breaks a rule of your profile: ${listText(flagged.map((c) => checkName(c.id).toLowerCase()))}.`,
    }[verdict] || "Some rules of your profile couldn't be checked.");
  } else if (level === "standard") {
    const [t0, w0] = holdings[0] || [];
    out.push(`${holdings.length} holding${holdings.length === 1 ? "" : "s"}: ${pctText(stocks)} stocks and ${pctText(bonds)} bonds`
      + `${risk ? `, for a ${risk} risk level` : ""}${years ? ` over ${years} year${years === 1 ? "" : "s"}` : ""}.`);
    if (t0) out.push(`The largest is ${t0} (${plain(t0)}), at ${pctText(w0, 1)}.`);
    if (vol) out.push(`Volatility: about ${pctText(vol, 1)} a year, measured on the last 5 years.`);
    if (governance) out.push({ PASS: "It fits every limit of your profile.",
      WARN: `It fits your profile, with a point worth a look: ${listText(flagged.map((c) => checkName(c.id).toLowerCase()))}.`,
      BLOCK: `It goes against your profile on: ${listText(flagged.map((c) => checkName(c.id).toLowerCase()))}.`,
    }[verdict] || `Some limits could not be checked, so it is not confirmed: ${listText(flagged.map((c) => checkName(c.id).toLowerCase()))}.`);
  } else {
    out.push(`Weights: ${holdings.map(([t, w]) => `${t} ${pctText(w, 1)}`).join(", ")}.`);
    out.push(`Asset classes: ${pctText(stocks, 1)} equity, ${pctText(bonds, 1)} fixed income`
      + `${risk ? `; risk band ${risk}` : ""}${years ? `, horizon ${years} y` : ""}.`);
    if (vol) out.push(`Annualised volatility ${pctText(vol, 1)} (5 years of daily returns, CAD).`);
    const n = ((governance && governance.checks) || []).length;
    const overall = { PASS: "fits every limit", WARN: "fits, with points worth a look", BLOCK: "goes against the profile" }[verdict]
      || "not confirmed (some limits could not be checked)";
    const status = { WARN: "worth a look", BLOCK: "not met", UNVERIFIED: "not confirmed" };
    if (governance) out.push(`Profile check: ${overall}${n ? `, ${n} limit${n === 1 ? "" : "s"} checked` : ""}`
      + `${flagged.length ? ` (${flagged.map((c) => `${checkName(c.id, true)}: ${status[c.status] || "not confirmed"}`).join("; ")})` : ""}.`);
  }
  out.push("This is a simulation, not advice.");

  const why = !learn ? null
    : bonds >= 0.05 && stocks >= 0.05 ? "stocks can grow more over time but swing more; bonds grow slowly and steady the whole. The more risk you accept, the bigger the share of stocks."
    : stocks >= 0.95 ? "this portfolio is almost all stocks: it can grow more over time, but its value can drop a lot in a bad year."
    : "bonds grow slowly but move little, so they protect your money more than stocks do.";
  return { text: out.join(" "), learn: why };
}

/* ---------- How much detail ---------- */

export const LEVELS = ["simple", "standard", "detailed"];
export const LEVEL_LABELS = { simple: "Simple", standard: "Standard", detailed: "Detailed" };
const FROM_EXPERIENCE = { beginner: "simple", intermediate: "standard", advanced: "detailed" };
export function levelFor(profile, override) {
  if (LEVELS.includes(override)) return override;
  return FROM_EXPERIENCE[(profile || {}).experience] || "standard";
}

/* ---------- Interest rates (POST /v1/rate-exposure): how the holdings reacted to rates, with the current rates ---------- */

const fmtPct = (v) => `${(v * 100).toFixed(0)}%`;
const signed = (n) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(1)}%`;

/** One row per holding for the table. A stock or equity fund has no measured sensitivity: "Not measured", never 0. */
export function rateRows(d) {
  return (d.holdings || []).map((x) => ({
    ticker: x.ticker, weight: fmtPct(x.weight), measured: x.measured,
    duration: x.measured ? x.duration.toFixed(1) : "Not measured",
    impact: x.measured ? signed(x.impact_pct_per_point) : "Not measured",
  }));
}

/** The one-line takeaway. Past sensitivity, not a forecast of rates or returns. */
export function rateSentence(d) {
  const p = d.portfolio || {};
  if (!p.measured_share) return "None of your holdings has a measured sensitivity to interest rates: stocks and equity funds are not measured here.";
  return `${fmtPct(p.measured_share)} of your portfolio is in funds with a measured sensitivity to interest rates. `
    + `By their history, a 1-point rise in yields would have moved your portfolio's value by about ${signed(p.impact_pct_per_point)}. `
    + "This describes past sensitivity. It is not a forecast of rates.";
}

/** The current rates as [label, value] pairs, in a fixed order. */
export function rateFacts(d) {
  const names = { "BoC policy rate (%)": "Bank of Canada rate", "CA 2y yield (%)": "Canada 2-year yield", "CA 10y yield (%)": "Canada 10-year yield",
    "US fed funds (%)": "US Fed funds rate", "US 10y yield (%)": "US 10-year yield", "CA CPI y/y (%)": "Canadian inflation (year over year)" };
  return Object.entries(names).filter(([k]) => (d.rates || {})[k] != null).map(([k, label]) => [label, `${Number(d.rates[k]).toFixed(2).replace(/\.?0+$/, "")}%`]);
}

/* ---------- "What if" weights: the total always stays 100% ---------- */

/** Set one holding's share; the others make room (or take the rest) in proportion to their current shares. */
export function rebalance(weights, ticker, share) {
  const x = Math.max(0, Math.min(1, share));
  const others = Object.keys(weights).filter((t) => t !== ticker);
  if (!others.length) return { [ticker]: 1 };
  const rest = others.reduce((s, t) => s + weights[t], 0);
  const out = {};
  for (const t of Object.keys(weights)) {
    out[t] = t === ticker ? x : rest > 1e-12 ? (weights[t] / rest) * (1 - x) : (1 - x) / others.length;
  }
  return out;
}
/** Add a holding at `share` (5% by default), the others scaled down; an existing one is just set to `share`. */
export const addHolding = (weights, ticker, share = 0.05) => rebalance({ ...weights, [ticker]: weights[ticker] || 0 }, ticker, share);
/** Remove a holding: its share goes to the others, in proportion. The last one can't be removed. */
export function removeHolding(weights, ticker) {
  const rest = Object.fromEntries(Object.entries(weights).filter(([t]) => t !== ticker));
  const total = Object.values(rest).reduce((s, v) => s + v, 0);
  if (!Object.keys(rest).length) return weights;
  return Object.fromEntries(Object.entries(rest).map(([t, v]) => [t, total > 1e-12 ? v / total : 1 / Object.keys(rest).length]));
}
export const sameWeights = (a, b) => weightsMatch(a || {}, b || {}, 0.0005) && Object.keys(cleanWeights(a)).length === Object.keys(cleanWeights(b)).length;
