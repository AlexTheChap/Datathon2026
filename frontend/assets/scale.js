/* A 1-to-10 scale that still lands in a category (risk level, experience): the person slides, the category shows
 * live under the track with its meaning. Starts unset (no value) until the person moves it, so a form can still
 * ask "pick one". Keyboard: arrows, Home, End (native range input). Screen readers hear "6 out of 10, Moderate". */

export const RISK_BANDS = [
  { value: "low", from: 1, to: 2, score: 2 },
  { value: "low_moderate", from: 3, to: 4, score: 4 },
  { value: "moderate", from: 5, to: 7, score: 6 },
  { value: "high", from: 8, to: 10, score: 9 },
];
export const EXPERIENCE_BANDS = [
  { value: "beginner", from: 1, to: 3, score: 2 },
  { value: "intermediate", from: 4, to: 7, score: 5 },
  { value: "advanced", from: 8, to: 10, score: 9 },
];

/** bands: [{value, from, to, score}] · options: [{value, title, desc}] (the categories' words, from app.js). */
export function scale(h, { id, name, label, help, bands, options, value = null, onChange }) {
  const titleOf = (v) => (options.find((o) => o.value === v) || {}).title || v;
  const descOf = (v) => (options.find((o) => o.value === v) || {}).desc || "";
  const bandOf = (n) => bands.find((b) => n >= b.from && n <= b.to);
  const input = h("input", { type: "range", class: "scale__input", id, name, min: "1", max: "10", step: "1",
    "aria-describedby": `${id}-out${help ? ` ${id}-help` : ""}` });
  const out = h("output", { class: "scale__out", id: `${id}-out`, for: id, "aria-live": "polite" });
  // One red bar under the categories, moved and resized to the current one (transform only, so it slides).
  const bar = h("span", { class: "scale__bar" });
  const ticks = h("div", { class: "scale__bands", "aria-hidden": "true" }, bar, bands.map((b) => h("span", {
    class: "scale__band", style: `grid-column: ${b.from} / ${b.to + 1}`, "data-value": b.value }, titleOf(b.value))));
  const el = h("div", { class: "scale is-unset" },
    h("label", { class: "field__label", for: id }, label),
    help ? h("p", { class: "field__help", id: `${id}-help` }, help) : null,
    h("div", { class: "scale__track" }, input, ticks),
    out);

  let set = false;
  let shown = null; // the category on screen: its words slide in only when it changes
  function show(n, fromUser) {
    const b = bandOf(n);
    set = true;
    el.classList.remove("is-unset");
    bar.style.transform = `translateX(${((b.from - 1) / 10) * 100}%) scaleX(${(b.to - b.from + 1) / 10})`;
    const changed = shown !== b.value;
    shown = b.value;
    input.value = String(n);
    input.style.setProperty("--fill", `${((n - 1) / 9) * 100}%`);
    input.setAttribute("aria-valuetext", `${n} out of 10, ${titleOf(b.value)}`);
    out.replaceChildren(h("strong", null, `${n} / 10 · ${titleOf(b.value)}`), h("span", null, descOf(b.value)));
    if (changed) { out.classList.remove("is-new"); void out.offsetWidth; out.classList.add("is-new"); }
    for (const t of ticks.children) t.classList.toggle("is-on", t.dataset.value === b.value);
    if (fromUser && onChange) onChange(b.value, n);
  }
  input.addEventListener("input", () => show(Number(input.value), true));
  input.addEventListener("keydown", () => { if (!set) show(Number(input.value), true); });
  input.addEventListener("pointerdown", () => { if (!set) window.requestAnimationFrame(() => show(Number(input.value), true)); });

  el.input = input;
  el.value = () => (set ? bandOf(Number(input.value)).value : null);
  el.score = () => (set ? Number(input.value) : null);
  /** Show a saved category at its usual place on the scale (or clear it). */
  el.setCategory = (v) => {
    const b = bands.find((x) => x.value === v);
    if (b) show(b.score, false);
    else { set = false; shown = null; el.classList.add("is-unset"); input.value = "5"; input.removeAttribute("aria-valuetext");
      out.replaceChildren(h("span", null, "Move the slider to choose.")); for (const t of ticks.children) t.classList.remove("is-on"); }
  };
  el.setCategory(value);
  return el;
}
