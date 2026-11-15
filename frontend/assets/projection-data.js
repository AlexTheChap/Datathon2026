/* Projection and volatility-trend helpers for the portfolio page: pure functions (no DOM), tested with node
 * (tests/js/projection-data.test.mjs). The numbers come from POST /v1/projection and GET /v1/vol-forecast; this
 * module only reshapes them: a fan chart's geometry, the plain-language summary, the assumptions rows and the
 * trend chips. A projection is a RANGE of outcomes under the 2026 planning guidelines, never a forecast. */

export const DISCLAIMER = "Simulation under official planning assumptions. Not a forecast, not advice.";
export const HORIZON_CHOICES = [1, 2, 3, 5, 7, 10, 15, 20, 25, 30];
const BANDS = ["p10", "p25", "p50", "p75", "p90"];

/** Profile horizon -> years, like the API: the upper bound of a range, the lower bound when open-ended. */
export function horizonFromProfile(h) {
  if (!h) return null;
  const [lo, hi] = Array.isArray(h) ? h : [h.min, h.max];
  return hi || lo || null;
}

export function horizonOptions(defaultYears) {
  const all = new Set(HORIZON_CHOICES);
  if (defaultYears) all.add(defaultYears);
  return [...all].sort((a, b) => a - b);
}

/** {years, p10..p90, start} for one mode ("nominal" or "real") from the API's percentiles_by_year. */
export function fanSeries(projection, mode = "nominal") {
  const rows = projection.percentiles_by_year || [];
  const out = { years: rows.map((r) => r.year), start: projection.start_value };
  for (const k of BANDS) out[k] = rows.map((r) => r[mode][k]);
  return out;
}

/** Round axis ticks covering [min, max]. */
export function niceTicks(min, max, count = 4) {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

/** SVG geometry of the fan chart: P10–P90 band, P25–P75 band, P50 line, start line, axis ticks. */
export function fanGeometry(series, { width = 640, height = 260, left = 72, right = 16, top = 12, bottom = 28 } = {}) {
  const lo = Math.min(...series.p10, series.start);
  const hi = Math.max(...series.p90, series.start);
  const yTicks = niceTicks(lo, hi);
  const y0 = yTicks[0]; const y1 = yTicks[yTicks.length - 1];
  const n = series.years.length;
  const x = (i) => left + (n > 1 ? (i / (n - 1)) * (width - left - right) : 0);
  const y = (v) => top + (1 - (v - y0) / (y1 - y0 || 1)) * (height - top - bottom);
  const r = (v) => Math.round(v * 10) / 10;
  const line = (vals) => vals.map((v, i) => `${i ? "L" : "M"}${r(x(i))},${r(y(v))}`).join(" ");
  const band = (a, b) => `${line(a)} ${b.map((v, i) => [i, v]).reverse().map(([i, v]) => `L${r(x(i))},${r(y(v))}`).join(" ")} Z`;
  const every = Math.max(1, Math.ceil((n - 1) / 6));
  return {
    width, height,
    outer: band(series.p10, series.p90),
    inner: band(series.p25, series.p75),
    median: line(series.p50),
    startY: r(y(series.start)),
    plot: { left, right: width - right, top, bottom: height - bottom },
    xTicks: series.years.map((yr, i) => ({ x: r(x(i)), label: String(yr) })).filter((_, i) => i % every === 0 || i === n - 1),
    yTicks: yTicks.map((v) => ({ y: r(y(v)), value: v })),
    points: series.years.map((yr, i) => ({ x: r(x(i)), y: r(y(series.p50[i])), year: yr, p10: series.p10[i], p50: series.p50[i], p90: series.p90[i] })),
  };
}

const share = (p) => (p > 0 && p < 0.005 ? "less than 1%" : `${Math.round(p * 100)}%`);

/** The plain-language summary, built from the numbers only (no model involved). */
export function summarySentence(projection, mode, money) {
  const f = projection.final;
  const h = projection.horizon_years;
  const real = mode === "real";
  const pc = f[mode];
  const below = real ? f.prob_below_start_real : f.prob_below_start_nominal;
  return `In ${h} year${h === 1 ? "" : "s"}, under the 2026 planning guidelines, your portfolio ended between `
    + `${money(pc.p10)} and ${money(pc.p90)}${real ? " in today's dollars" : ""} in 8 of 10 simulated scenarios. `
    + `In ${share(below)} of scenarios it was worth less than today${real ? ", after inflation" : ""}.`;
}

const CATEGORY_LABELS = {
  short_term: "Short-term (cash)", fixed_income: "Fixed income", canadian_equities: "Canadian equities",
  us_equities: "U.S. equities", intl_developed_equities: "International developed equities",
  emerging_equities: "Emerging-market equities", gold: "Gold", crypto: "Crypto",
};
export const categoryLabel = (c) => CATEGORY_LABELS[c] || c;

const pct = (v, d = 1) => `${(v * 100).toFixed(d)}%`;

/** Rows of the "Assumptions" disclosure. Team assumptions are labelled as such. */
export function assumptionRows(projection) {
  const a = projection.assumptions;
  return a.per_ticker.map((t) => ({
    ticker: t.ticker, category: categoryLabel(t.category), rate: pct(t.rate),
    source: t.team_assumption ? "Team assumption (no guideline category)" : "Planning guidelines",
    vol: pct(t.vol_start), note: t.note || "",
  }));
}

/** One line on where the volatility came from. */
export function volSourceText(projection) {
  const v = projection.assumptions.vol_source;
  if (v.used === "garch") {
    return `GARCH forecast from job ${v.job_name || "unknown"}, as of ${v.forecast_as_of}, for year 1, moving to `
      + "each asset's long-run level by the end of year 2.";
  }
  return `Historical volatility of the daily returns (${v.reason}).`;
}

const TRENDS = {
  rising: { arrow: "↑", word: "Rising", cls: "rising" },
  stable: { arrow: "→", word: "Stable", cls: "stable" },
  easing: { arrow: "↓", word: "Easing", cls: "easing" },
};

/** A holding's risk-trend chip: arrow AND word (colour is never the only cue), and the tooltip text. */
export function trendChip(ticker, entry) {
  if (!entry || !entry.trend) {
    const hv = entry && entry.hist_vol_63d != null ? `last 3 months ${pct(entry.hist_vol_63d)}` : "no data";
    return { ticker, arrow: "", word: "No forecast", cls: "none", tooltip: `No GARCH forecast; ${hv}` };
  }
  const t = TRENDS[entry.trend];
  return { ticker, arrow: t.arrow, word: t.word, cls: t.cls,
    tooltip: `GARCH forecast ${pct(entry.forecast_vol)} vs last 3 months ${pct(entry.hist_vol_63d)}` };
}
