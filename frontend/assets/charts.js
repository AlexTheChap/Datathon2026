/* Charts for the portfolio dashboard, in plain HTML (no library): every chart is a <figure> with a caption, the
 * marks, a legend when there are two or more series, and a "Show the numbers" table (its accessible twin).
 * Marks follow the data-viz rules: thin bars (<= 16px here), 4px rounded data-end, square at the baseline,
 * a 2px surface gap between touching marks, values in text ink (never in the series colour), and a tooltip on
 * hover AND keyboard focus that never holds the only copy of a value (the table has them all).
 * Colours are CSS custom properties (--viz-1 ... --viz-5, validated light and dark) defined in app.css. */

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c))); // text nodes: data stays text
  }
  return node;
}

/* ---------- One shared tooltip ---------- */

let tip = null;
function tooltip() {
  if (!tip) {
    tip = el("div", { class: "viz-tip", role: "presentation", "aria-hidden": "true" });
    document.body.append(tip);
  }
  return tip;
}
function showTip(target, value, label) {
  const t = tooltip();
  t.replaceChildren(el("strong", null, value), el("span", null, label));
  const r = target.getBoundingClientRect();
  t.hidden = false;
  const w = t.offsetWidth;
  const left = Math.min(window.innerWidth - w - 8, Math.max(8, r.left + r.width / 2 - w / 2));
  t.style.left = `${left + window.scrollX}px`;
  t.style.top = `${r.top + window.scrollY - t.offsetHeight - 8}px`;
}
function hideTip() { if (tip) tip.hidden = true; }

/** A mark that answers hover and keyboard focus with the same tooltip; its accessible name carries the value. */
function mark(cls, style, value, label) {
  return el("span", {
    class: `viz-mark ${cls}`, style, tabindex: "0", role: "img", "aria-label": `${label}: ${value}`,
    onpointerenter: (e) => showTip(e.currentTarget, value, label), onpointerleave: hideTip,
    onfocus: (e) => showTip(e.currentTarget, value, label), onblur: hideTip,
  });
}

/* ---------- Pieces ---------- */

function frame({ title, note, chart, legend, table, id }) {
  const capId = id ? `viz-${id}-title` : null; // prefixed: never collides with a section heading id
  return el("figure", { class: "viz", "aria-labelledby": capId },
    el("figcaption", { class: "viz__caption" },
      el("span", { class: "viz__title", id: capId }, title),
      note ? el("span", { class: "viz__note" }, note) : null),
    chart,
    legend || null,
    table ? el("details", { class: "viz__table" },
      el("summary", null, "Show the numbers"),
      el("table", null,
        el("thead", null, el("tr", null, table.headers.map((th) => el("th", { scope: "col" }, th)))),
        el("tbody", null, table.rows.map((row) => el("tr", null,
          row.map((cell, i) => (i === 0 ? el("th", { scope: "row" }, cell) : el("td", { class: "num" }, cell)))))))) : null);
}

function legendOf(series) {
  return el("ul", { class: "viz__legend" }, series.map((s) => el("li", null,
    el("span", { class: `viz-swatch viz-c${s.slot}`, "aria-hidden": "true" }), s.name)));
}

/* ---------- Charts ---------- */

/** One whole split into <= 6 parts (tall segmented bar): share printed in the block when it fits. */
export function segmentedBar({ id, title, note, parts, format }) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1;
  const shown = parts.filter((p) => p.value > 0);
  const bar = el("div", { class: "viz-seg" }, shown.map((p) => {
    const share = p.value / total;
    const block = mark(`viz-seg__part viz-c${p.slot}`, `flex-grow:${share}`, format(p.value), p.label);
    if (share >= 0.12) block.append(el("span", { class: "viz-seg__label", "aria-hidden": "true" }, format(p.value)));
    return block;
  }));
  return frame({ id, title, note, chart: bar, legend: legendOf(shown.map((p) => ({ name: p.label, slot: p.slot }))),
    table: { headers: ["Part", "Share"], rows: shown.map((p) => [p.label, format(p.value)]) } });
}

/** Ranked horizontal bars, one series (one hue, no legend: the title names it). Largest first. */
export function rankedBars({ id, title, note, rows, format, max, extraColumns = [] }) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 0) ?? 1;
  const chart = el("div", { class: "viz-bars" }, rows.map((r) => el("div", { class: "viz-bars__row" },
    el("span", { class: "viz-bars__label" }, el("span", null, r.label), r.sub ? el("span", { class: "viz-bars__sub" }, r.sub) : null),
    el("span", { class: "viz-bars__track" },
      mark("viz-bar viz-c1", `width:${top ? Math.max(0.5, (r.value / top) * 100) : 0}%`, format(r.value), r.label),
      el("span", { class: "viz-bars__value", "aria-hidden": "true" }, r.display ?? format(r.value))))));
  return frame({ id, title, note, chart,
    table: { headers: ["Item", "Value", ...extraColumns.map((c) => c.header)],
      rows: rows.map((r) => [r.label, r.display ?? format(r.value), ...extraColumns.map((c) => c.value(r))]) } });
}

/** Two series per row (e.g. share of the money vs share of the risk): grouped thin bars, legend, values at tips. */
export function pairedBars({ id, title, note, rows, series, format }) {
  const top = Math.max(...rows.flatMap((r) => [r.a, r.b]), 0) || 1;
  const chart = el("div", { class: "viz-bars viz-bars--paired" }, rows.map((r) => el("div", { class: "viz-bars__row" },
    el("span", { class: "viz-bars__label" }, el("span", null, r.label), r.sub ? el("span", { class: "viz-bars__sub" }, r.sub) : null),
    el("span", { class: "viz-bars__pair" },
      [[r.a, series[0]], [r.b, series[1]]].map(([v, s]) => el("span", { class: "viz-bars__track" },
        mark(`viz-bar viz-c${s.slot}`, `width:${Math.max(0.5, (v / top) * 100)}%`, format(v), `${r.label}, ${s.name}`),
        el("span", { class: "viz-bars__value", "aria-hidden": "true" }, format(v))))))));
  return frame({ id, title, note, chart, legend: legendOf(series),
    table: { headers: ["Item", series[0].name, series[1].name], rows: rows.map((r) => [r.label, format(r.a), format(r.b)]) } });
}

/** One ratio against its whole (a meter): the fill carries the tone, the track is a lighter step of it. */
export function meter({ value, max, label, display, tone = "neutral" }) {
  const share = Math.max(0, Math.min(1, value / max));
  return el("div", { class: `viz-meter viz-meter--${tone}`, role: "meter", "aria-valuemin": "0", "aria-valuemax": String(max),
    "aria-valuenow": String(value), "aria-label": `${label}: ${display}` },
    el("span", { class: "viz-meter__fill", style: `width:${share * 100}%` }));
}

/** Correlation grid, diverging around 0 (blue for moving apart, red for moving together, grey for neither). */
export function heatmap({ id, title, note, labels, matrix, format }) {
  const n = labels.length;
  const cell = (v, name) => {
    const a = Math.min(1, Math.abs(v));
    const pole = v >= 0 ? "var(--viz-div-pos)" : "var(--viz-div-neg)";
    const bg = `color-mix(in oklab, ${pole} ${Math.round(a * 100)}%, var(--viz-div-mid))`;
    const m = mark("viz-heat__cell", `background:${bg}`, format(v), name);
    m.append(el("span", { class: `viz-heat__value${a > 0.55 ? " viz-heat__value--on-dark" : ""}`, "aria-hidden": "true" }, format(v)));
    return m;
  };
  const grid = el("div", { class: "viz-heat", style: `grid-template-columns: auto repeat(${n}, minmax(40px, 1fr))` },
    el("span", null), labels.map((l) => el("span", { class: "viz-heat__col" }, l)),
    labels.map((row, i) => [el("span", { class: "viz-heat__row" }, row),
      labels.map((col, j) => cell(matrix[i][j], `${row} with ${col}`))]));
  const scale = el("div", { class: "viz-heat__scale", "aria-hidden": "true" },
    el("span", null, "Move apart"), el("span", { class: "viz-heat__ramp" }), el("span", null, "Move together"));
  // No "Show the numbers" table: every cell already prints its value and has an accessible name.
  return frame({ id, title, note, chart: el("div", { class: "viz-heat-wrap" }, grid), legend: scale });
}

/* ---------- Fan chart (SVG): a range over time ---------- */

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  for (const c of children.flat(Infinity)) if (c) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

/** P10–P90 band (light), P25–P75 band (darker), P50 line and a dashed start line. `geo` comes from
 * projection-data.js fanGeometry; `rows` feed the "Show the numbers" table. Each year's median point is
 * focusable and shows its P10 / P50 / P90 in the tooltip. */
export function fanChart({ id, title, note, geo, format, rows, startLabel = "Today's value" }) {
  const { width, height, plot } = geo;
  const grid = geo.yTicks.map((t) => [
    svg("line", { class: "fan__grid", x1: plot.left, x2: plot.right, y1: t.y, y2: t.y }),
    svg("text", { class: "fan__axis", x: plot.left - 8, y: t.y, "text-anchor": "end", "dominant-baseline": "middle" }, format(t.value))]);
  const xAxis = geo.xTicks.map((t) => svg("text", { class: "fan__axis", x: t.x, y: height - 8, "text-anchor": "middle" },
    t.label === "0" ? "Now" : `${t.label}y`));
  const chart = svg("svg", { class: "fan", viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" },
    grid, xAxis,
    svg("path", { class: "fan__outer", d: geo.outer }),
    svg("path", { class: "fan__inner", d: geo.inner }),
    svg("line", { class: "fan__start", x1: plot.left, x2: plot.right, y1: geo.startY, y2: geo.startY }),
    svg("path", { class: "fan__median", d: geo.median }));
  // Focus targets on top of the SVG (HTML, so they reuse the shared tooltip and keyboard behaviour).
  const dots = el("div", { class: "fan__dots" }, geo.points.map((pt) => {
    const label = pt.year === 0 ? "Now" : `Year ${pt.year}`;
    const value = `P10 ${format(pt.p10)} · P50 ${format(pt.p50)} · P90 ${format(pt.p90)}`;
    return mark("fan__dot", `left:${(pt.x / width) * 100}%;top:${(pt.y / height) * 100}%`, value, label);
  }));
  const legend = el("ul", { class: "viz__legend" },
    el("li", null, el("span", { class: "viz-swatch fan-swatch--outer", "aria-hidden": "true" }), "8 in 10 scenarios (P10–P90)"),
    el("li", null, el("span", { class: "viz-swatch fan-swatch--inner", "aria-hidden": "true" }), "Half of the scenarios (P25–P75)"),
    el("li", null, el("span", { class: "viz-swatch fan-swatch--median", "aria-hidden": "true" }), "Middle scenario (P50)"),
    el("li", null, el("span", { class: "viz-swatch fan-swatch--start", "aria-hidden": "true" }), startLabel));
  return frame({ id, title, note, chart: el("div", { class: "fan-wrap", style: `aspect-ratio:${width} / ${height}` }, chart, dots), legend,
    table: { headers: ["Year", "P10", "P25", "P50", "P75", "P90"], rows } });
}
