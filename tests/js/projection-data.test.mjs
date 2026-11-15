// projection-data.js: fan-chart data and geometry, summary sentence, assumptions rows, trend chips.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as pd from "../../frontend/assets/projection-data.js";

const pcts = (base) => ({ p5: base * 0.8, p10: base * 0.85, p25: base * 0.93, p50: base, p75: base * 1.08, p90: base * 1.18, p95: base * 1.25 });
const PROJ = {
  horizon_years: 3, start_value: 100000,
  percentiles_by_year: [0, 1, 2, 3].map((y) => ({ year: y, nominal: y ? pcts(100000 * 1.05 ** y) : pcts(100000),
    real: y ? pcts(100000 * 1.03 ** y) : pcts(100000) })),
  final: { nominal: { p10: 98765.4, p50: 115762.5, p90: 136599.8 }, real: { p10: 91000, p50: 109272.7, p90: 128900 },
    prob_below_start_nominal: 0.1234, prob_below_start_real: 0.004 },
  assumptions: {
    per_ticker: [{ ticker: "XBB.TO", category: "fixed_income", rate: 0.032, team_assumption: false, vol_start: 0.05 },
      { ticker: "GLD", category: "gold", rate: 0.021, team_assumption: true, vol_start: 0.2 },
      { ticker: "RY.TO", category: "canadian_equities", rate: 0.063, team_assumption: false, vol_start: 0.16, note: "Individual stock" }],
    vol_source: { used: "garch", job_name: "alphaforge-garch-1", forecast_as_of: "2026-11-12", reason: "fresh" },
  },
};
const money = (v) => `$${Math.round(v).toLocaleString("en-CA")}`;

test("fanSeries reads one mode of percentiles_by_year", () => {
  const s = pd.fanSeries(PROJ, "nominal");
  assert.deepEqual(s.years, [0, 1, 2, 3]);
  assert.equal(s.start, 100000);
  assert.equal(s.p50[0], 100000);
  assert.ok(s.p50[3] > s.p50[1]);
  assert.ok(pd.fanSeries(PROJ, "real").p50[3] < s.p50[3]);
  for (let i = 0; i < 4; i += 1) assert.ok(s.p10[i] < s.p25[i] && s.p25[i] < s.p50[i] && s.p50[i] < s.p75[i] && s.p75[i] < s.p90[i]);
});

test("fanGeometry: bands are closed paths, the median is a line, the y axis covers every value", () => {
  const s = pd.fanSeries(PROJ, "nominal");
  const g = pd.fanGeometry(s, { width: 400, height: 200 });
  assert.match(g.outer, /^M[\d.]+,[\d.]+( L[\d.]+,[\d.]+)+ Z$/);
  assert.match(g.inner, /Z$/);
  assert.ok(!g.median.includes("Z") && g.median.split("L").length === 4);
  assert.equal(g.points.length, 4);
  assert.equal(g.xTicks[0].label, "0");
  assert.equal(g.xTicks[g.xTicks.length - 1].label, "3");
  const ys = g.yTicks.map((t) => t.value);
  assert.ok(ys[0] <= Math.min(...s.p10) && ys[ys.length - 1] >= Math.max(...s.p90));
  // higher values are drawn higher (smaller y)
  assert.ok(g.points[3].y < g.points[0].y);
  assert.ok(g.startY >= g.plot.top && g.startY <= g.plot.bottom);
});

test("niceTicks gives round, increasing ticks", () => {
  const t = pd.niceTicks(81234, 132100);
  assert.ok(t.every((v, i) => i === 0 || v > t[i - 1]));
  assert.ok(t[0] <= 81234 && t[t.length - 1] >= 132100);
  assert.ok(t.every((v) => v % 5000 === 0 || v % 10000 === 0 || v % 2500 === 0));
});

test("summary sentence states the P10–P90 range and the chance of ending below today", () => {
  assert.equal(pd.summarySentence(PROJ, "nominal", money),
    "In 3 years, under the 2026 planning guidelines, your portfolio ended between $98,765 and $136,600 in 8 of 10 "
    + "simulated scenarios. In 12% of scenarios it was worth less than today.");
  const real = pd.summarySentence(PROJ, "real", money);
  assert.match(real, /between \$91,000 and \$128,900 in today's dollars in 8 of 10 simulated scenarios/);
  assert.match(real, /In less than 1% of scenarios it was worth less than today, after inflation\./);
  assert.ok(!/expect|will be|forecast/i.test(real));
  assert.match(pd.summarySentence({ ...PROJ, horizon_years: 1 }, "nominal", money), /^In 1 year,/);
});

test("assumption rows label team assumptions and volatility source names the job", () => {
  const rows = pd.assumptionRows(PROJ);
  assert.deepEqual(rows.map((r) => r.ticker), ["XBB.TO", "GLD", "RY.TO"]);
  assert.equal(rows[0].category, "Fixed income");
  assert.equal(rows[0].rate, "3.2%");
  assert.match(rows[1].source, /Team assumption/);
  assert.equal(rows[2].note, "Individual stock");
  assert.match(pd.volSourceText(PROJ), /GARCH forecast from job alphaforge-garch-1, as of 2026-11-12/);
  const hist = { ...PROJ, assumptions: { ...PROJ.assumptions, vol_source: { used: "historical", reason: "no GARCH forecast file" } } };
  assert.match(pd.volSourceText(hist), /^Historical volatility/);
});

test("trend chips carry an arrow AND a word, and the numbers in the tooltip", () => {
  const up = pd.trendChip("SPY", { trend: "rising", forecast_vol: 0.222, hist_vol_63d: 0.15 });
  assert.deepEqual([up.arrow, up.word, up.cls], ["↑", "Rising", "rising"]);
  assert.equal(up.tooltip, "GARCH forecast 22.2% vs last 3 months 15.0%");
  assert.equal(pd.trendChip("X", { trend: "easing", forecast_vol: 0.1, hist_vol_63d: 0.2 }).word, "Easing");
  assert.equal(pd.trendChip("X", { trend: "stable", forecast_vol: 0.2, hist_vol_63d: 0.2 }).arrow, "→");
  const none = pd.trendChip("X", { trend: null, forecast_vol: null, hist_vol_63d: 0.3 });
  assert.equal(none.word, "No forecast");
  assert.match(none.tooltip, /last 3 months 30\.0%/);
});

test("horizon from the profile: upper bound of a range, lower bound when open-ended", () => {
  assert.equal(pd.horizonFromProfile({ min: 3, max: 5 }), 5);
  assert.equal(pd.horizonFromProfile({ min: 15, max: null }), 15);
  assert.equal(pd.horizonFromProfile([8, 12]), 12);
  assert.equal(pd.horizonFromProfile(null), null);
  assert.deepEqual(pd.horizonOptions(12).filter((y) => y >= 10 && y <= 15), [10, 12, 15]);
  assert.equal(pd.DISCLAIMER, "Simulation under official planning assumptions. Not a forecast, not advice.");
});
