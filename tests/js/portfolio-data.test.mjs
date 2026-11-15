// node --test tests/js/: the dashboard's data logic on a real graph-mode portfolio run (Mateo, 2026-11-15).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as d from "../../frontend/assets/portfolio-data.js";

const fixture = (p) => new URL(`../fixtures/${p}`, import.meta.url);
// The fixture was recorded before the NaN fix in analysis_tools.esg_screen: read NaN as null, like the API does.
const records = readFileSync(fixture("ledger_mateo_portfolio.jsonl"), "utf8").trim().split("\n")
  .map((l) => JSON.parse(l.replace(/\bNaN\b/g, "null")));
const run = JSON.parse(readFileSync(fixture("run_mateo_portfolio.json"), "utf8"));
const universe = JSON.parse(readFileSync(new URL("../../frontend/assets/universe.json", import.meta.url), "utf8")).assets;

test("the portfolio is the optimizer's verified weights, judged by the top-level governance check", () => {
  const p = d.extractPortfolio(run, records);
  assert.deepEqual(p.weights, { SPY: 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, QQQ: 0.0956 });
  assert.equal(p.governance.verdict, "PASS");
  assert.equal(p.governance.agent, "synthesis");
  assert.equal(p.answer.kind, "explained");
});

test("risk measured on another mix is not shown as the portfolio's", () => {
  const p = d.extractPortfolio(run, records);
  assert.equal(p.risk.matches, false);              // the risk sub-agent measured SPY 40 / XIU 15 / XBB 35 / QQQ 10
  assert.equal(p.risk.contribution, null);
  assert.equal(p.risk.concentration, null);
  assert.deepEqual(d.riskOf(records, p.risk.measuredWeights).contribution.weights, p.risk.measuredWeights);
});

test("the follow-up risk question names the exact weights, and is found again in the runs list", () => {
  const q = d.riskPrompt({ SPY: 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, QQQ: 0.0956 });
  assert.equal(q, "Check the risk of my portfolio: SPY 34.81%, XBB.TO 30.54%, XIU.TO 25.09%, QQQ 9.56%.");
  const runs = [{ question: q, status: "RUNNING" }, { question: d.PORTFOLIO_PROMPT, status: "DONE", run_id: "r1" }];
  assert.equal(d.findRun(runs, q, { exact: true }), null);
  assert.equal(d.pendingRun(runs, q, true).status, "RUNNING");
  assert.equal(d.findRun(runs, d.PORTFOLIO_PROMPT).run_id, "r1");
});

test("shares by asset class are arithmetic on the weights", () => {
  const split = d.classSplit({ SPY: 0.3481, "XBB.TO": 0.3054, "XIU.TO": 0.2509, QQQ: 0.0956 }, universe);
  assert.deepEqual(split.map((g) => [g.label, Number(g.value.toFixed(4))]), [["Stocks", 0.6946], ["Bonds", 0.3054]]);
});

test("profile changes that shape a portfolio are detected, others are not", () => {
  const snap = run.profile;
  assert.equal(d.profileChanged(snap, { ...snap }), false);
  assert.equal(d.profileChanged(snap, { ...snap, wants_to_learn: !snap.wants_to_learn }), false);
  assert.equal(d.profileChanged(snap, { ...snap, risk_band: "high" }), true);
});

test("checks in plain words, fit score and detail level", () => {
  assert.equal(d.checkName("SINGLE_POSITION"), "No single company is too big");
  assert.equal(d.checkName("EXCLUSION_ESG_FOSSIL_FUELS", true), "Exclusion: fossil fuels");
  assert.equal(d.checkValue({ value: 0.0339, limit: 0.1 }), "3.4% (limit 10%)");
  const fit = { score: 42, parts: [
    { key: "rules", title: "Your profile's rules", points: 11, max: 30, items: [
      { id: "CRYPTO", status: "BLOCK", why: "broken", lost: 11.67 }, { id: "SECTOR", status: "PASS", why: "respected", lost: 0 }] },
    { key: "risk_level", title: "Risk level", points: 20, max: 20, items: [{ id: "VOLATILITY", why: "15% a year, inside", lost: 0 }] }] };
  assert.deepEqual(d.fitLines(fit, "simple").map((l) => l.key), ["rules"]);           // only what lost points
  assert.deepEqual(d.fitLines(fit, "simple")[0].reasons, ["Crypto stays within your limit: broken"]);
  assert.equal(d.fitLines(fit, "standard").length, 2);
  assert.deepEqual(d.fitLines(fit, "detailed")[0].reasons, ["Crypto weight: broken", "Largest sector: respected"]);
  assert.equal(d.fitTone(42), "bad"); assert.equal(d.fitTone(90), "good");
  assert.equal(d.levelFor({ experience: "beginner" }), "simple");
  assert.equal(d.levelFor({ experience: "beginner" }, "detailed"), "detailed");
});

test("a run QUEUED for minutes is a plan-only server, not a run in progress", () => {
  const now = Date.parse("2026-11-15T12:00:00Z");
  assert.equal(d.inProgress({ status: "RUNNING", created_at: "2026-11-15T10:00:00Z" }, now), true);
  assert.equal(d.inProgress({ status: "QUEUED", created_at: "2026-11-15T11:59:30Z" }, now), true);
  assert.equal(d.inProgress({ status: "QUEUED", created_at: "2026-11-15T11:50:00Z" }, now), false);
  assert.equal(d.inProgress({ status: "DONE", created_at: "2026-11-15T11:59:59Z" }, now), false);
});

test("the summary at the top is written for the level, and explains the idea when the person wants to learn", () => {
  const p = d.extractPortfolio(run, records);
  const base = { weights: p.weights, universe, governance: p.governance, vol: 0.091, profile: { risk_band: "moderate", horizon_years: 10 } };
  const simple = d.heroSummary({ ...base, level: "simple" });
  const standard = d.heroSummary({ ...base, level: "standard" });
  const detailed = d.heroSummary({ ...base, level: "detailed" });
  assert.match(simple.text, /500 large US companies \(35%\)/);           // plain names, no tickers
  assert.doesNotMatch(simple.text, /SPY|volatility|PASS/);
  assert.match(standard.text, /largest is SPY .* 34\.8%/i);
  assert.match(detailed.text, /SPY 34\.8%, XBB\.TO 30\.5%/);
  assert.match(detailed.text, /Profile check: fits every limit/);
  assert.doesNotMatch(detailed.text, /(PASS|WARN|BLOCK|UNVERIFIED)/);
  assert.equal(new Set([simple.text, standard.text, detailed.text]).size, 3);
  for (const s of [simple, standard, detailed]) assert.match(s.text, /simulation, not advice/);
  assert.equal(simple.learn, null);
  assert.match(d.heroSummary({ ...base, level: "simple", learn: true }).learn, /bonds/);
});

test("a warning in the summary names the rule, in the level's words", () => {
  const governance = { verdict: "WARN", checks: [{ id: "EXCLUSION_CONTROVERSY", status: "WARN" }, { id: "CRYPTO", status: "PASS" }] };
  const args = { weights: { SPY: 0.6, "XBB.TO": 0.4 }, universe, governance };
  assert.match(d.heroSummary({ ...args, level: "simple" }).text, /one point to keep an eye on: no company with a major controversy/);
  assert.match(d.heroSummary({ ...args, level: "detailed" }).text, /Controversy exclusion: worth a look/);
});
