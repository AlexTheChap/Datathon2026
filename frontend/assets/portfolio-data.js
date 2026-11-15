/* Portfolio dashboard, data side (no DOM: tested with node, tests/js/portfolio-data.test.mjs).
 *
 * "Your portfolio" is built by AlphaForge from the profile: a normal run of the pipeline with the question
 * PORTFOLIO_PROMPT (task allocation_proposal: the optimizer finds the weights, governance checks them, the
 * Explainer explains them). Every figure on the dashboard comes from that run's evidence ledger
 * (GET /v1/runs/{id}/evidence) or from plain arithmetic on its weights (shares, amounts): nothing is estimated here.
 *
 * Risk figures count as the portfolio's only when the tool measured these exact weights (weightsMatch). When the
 * run measured another mix (sub-agents run in parallel in graph mode), the dashboard asks one follow-up question,
 * riskPrompt(weights), and reads the risk from that run instead. */

export const PORTFOLIO_PROMPT = "Build a simulated portfolio that fits my profile.";
export const RISK_PROMPT_PREFIX = "Check the risk of my portfolio: ";
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

/** Fit with the profile's own rules, out of 10: every point lost has a reason (a rule that warns or blocks). */
export function fitScore(governance, contribution) {
  let score = 10; const why = [];
  for (const c of (governance && governance.checks) || []) {
    if (c.status === "BLOCK") { score -= 2; why.push({ reason: `${checkName(c.id)}: blocked`, points: -2 }); }
    else if (c.status === "WARN") { score -= 1; why.push({ reason: `${checkName(c.id)}: warning`, points: -1 }); }
  }
  if (contribution && contribution.risk) {
    const [top, share] = Object.entries(contribution.risk).sort((a, b) => b[1] - a[1])[0] || [];
    if (share > 0.5) { score -= 1; why.push({ reason: `${top} carries ${Math.round(share * 100)}% of the ups and downs`, points: -1 }); }
  }
  score = Math.max(0, Math.min(10, Math.round(score * 2) / 2));
  const band = score < 3.5 ? "Poor fit" : score < 6.5 ? "Risky fit" : score < 8.5 ? "Acceptable fit" : "Good fit";
  return { score, band, why };
}

/* ---------- How much detail ---------- */

export const LEVELS = ["simple", "standard", "detailed"];
export const LEVEL_LABELS = { simple: "Simple", standard: "Standard", detailed: "Detailed" };
const FROM_EXPERIENCE = { beginner: "simple", intermediate: "standard", advanced: "detailed" };
export function levelFor(profile, override) {
  if (LEVELS.includes(override)) return override;
  return FROM_EXPERIENCE[(profile || {}).experience] || "standard";
}
