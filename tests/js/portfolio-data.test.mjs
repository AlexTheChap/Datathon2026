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
  const fit = d.fitScore({ checks: [{ id: "SECTOR", status: "WARN" }, { id: "CRYPTO", status: "BLOCK" }] }, null);
  assert.equal(fit.score, 7); assert.equal(fit.band, "Acceptable fit"); assert.equal(fit.why.length, 2);
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
