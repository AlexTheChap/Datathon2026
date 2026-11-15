/* The profile as a step-by-step form (#/profile). One question at a time, no chat bubbles.
 *
 * Each question is a card (<fieldset> + <legend>). Only the first unanswered card is open. "Save" validates it, saves
 * the whole form to the server (PUT /v1/profile), locks the card (the saved value stays visible, read-only, with a
 * "Change" button at the bottom right) and opens the next one. An answer is never repeated anywhere else.
 *
 * Two parts, so the logic can be tested without a browser:
 *   - createStepper(): which card is open, locked, being changed (pure state, no DOM);
 *   - buildProfileSteps(): the cards and their controls (DOM built with the app's `h`). */
import { EXPERIENCE_BANDS, RISK_BANDS, scale as makeScale } from "./scale.js";

export const STEP_ORDER = ["age", "risk_band", "experience", "wants_to_learn", "horizon", "money", "objective",
  "preferences", "limits", "analyses"];
export const REQUIRED_STEPS = ["risk_band", "experience"];
// What the announcements and the "Change …" button names call each question ("Risk level saved").
export const STEP_NOUN = {
  age: "Age", risk_band: "Risk level", experience: "Experience", wants_to_learn: "Learning explanations",
  horizon: "Investment horizon", money: "Capital and largest loss", objective: "Goal",
  preferences: "Preferences and constraints", limits: "Hard limits", analyses: "Analyses",
};
export const PREFERENCES_HELP = "In your own words: what you like, what you want to avoid, anything AlphaForge should "
  + "keep in mind. Example: I want some diversification and not only tech; little interest in very speculative investments.";
export const AGE_MIN = 18;
export const AGE_MAX = 120;
export const PREFERENCES_MAX = 2000;

/* ---------------------------------------- State (no DOM) ---------------------------------------- */

/** Which questions count as answered when the page opens.
 *  `saved`: the profile on the server (or null). `record`: what this browser remembers ({answered, skipped}) or null.
 *  A profile saved before this form existed (no record) has had every question asked by the earlier forms, except age. */
export function initialAnswered(saved, record) {
  const out = new Set(record ? record.answered : []);
  if (!saved) return [...out].filter((k) => STEP_ORDER.includes(k));
  if (!record) for (const k of STEP_ORDER) out.add(k);
  if (!record && saved.age == null) out.delete("age");
  if (!saved.risk_band) out.delete("risk_band");
  if (!saved.experience) out.delete("experience");
  const hz = saved.horizon_years || {};
  const filled = {
    age: saved.age != null, risk_band: Boolean(saved.risk_band), experience: Boolean(saved.experience),
    wants_to_learn: saved.wants_to_learn === true, horizon: hz.min != null || hz.max != null,
    money: saved.capital != null || saved.loss_capacity != null, objective: Boolean(saved.objective),
    preferences: Boolean(saved.preferences_text) || (saved.preferences || []).length > 0 || (saved.constraints || []).length > 0,
    limits: (saved.exclusions || []).length > 0, analyses: Object.values(saved.agent_toggles || {}).some((v) => v === false),
  };
  for (const [k, yes] of Object.entries(filled)) if (yes) out.add(k);
  return STEP_ORDER.filter((k) => out.has(k));
}

/** The state of the cards. status(key): "locked" | "open" | "editing" | "pending" | "unsaved" (after "Save all"'s fill). */
export function createStepper({ order = STEP_ORDER, required = REQUIRED_STEPS, answered = [], skipped = [] } = {}) {
  const done = new Set(answered);
  const skips = new Set(skipped);
  const editing = new Set();
  let bulk = false;
  const firstOpen = () => order.find((k) => !done.has(k)) ?? null;
  return {
    order,
    isRequired: (k) => required.includes(k),
    firstOpen,
    isBulk: () => bulk,
    isAnswered: (k) => done.has(k),
    isSkipped: (k) => skips.has(k),
    status(k) {
      if (bulk) return "unsaved";
      if (editing.has(k)) return "editing";
      if (done.has(k)) return "locked";
      return k === firstOpen() ? "open" : "pending";
    },
    /** Would every question be answered once `k` is saved? */
    completesWith: (k) => order.every((x) => done.has(x) || x === k),
    isComplete: () => order.every((k) => done.has(k)),
    save(k) { done.add(k); skips.delete(k); editing.delete(k); return firstOpen(); },
    skip(k) {
      if (required.includes(k)) throw new Error(`${k} is required`);
      done.add(k); skips.add(k); editing.delete(k); return firstOpen();
    },
    change(k) { if (!bulk && done.has(k)) editing.add(k); },
    cancel(k) { editing.delete(k); },
    /** A persona or a document fills every card at once, unsaved. */
    fillAll() { bulk = true; editing.clear(); },
    saveAll() { for (const k of order) { done.add(k); skips.delete(k); } bulk = false; editing.clear(); return null; },
    discardAll() { bulk = false; editing.clear(); },
    snapshot: () => ({ answered: order.filter((k) => done.has(k)), skipped: order.filter((k) => skips.has(k)) }),
  };
}

/* ---------------------------------------- Cards ---------------------------------------- */

const percent = (fraction) => (fraction == null ? "" : String(Math.round(fraction * 100)));

/** The cards, with `read()` (what the card holds, in the profile's own field names) and `write(values)`. */
function makeCards(ctx, uid) {
  const { h, icon, RISK, EXPERIENCE, LIMITS, ANALYSES, AGENTS } = ctx;
  const scale = ctx.scale || makeScale;
  const cards = {};
  // One inline error under each field: errors[name] is its element; `fail(name, msg)` fills it.
  function errorSlot(card, name) {
    return (card.errors[name] = h("p", { class: "field__error", id: `${uid}-${name}-error`, "data-error-for": name }));
  }
  const define = (key, def) => {
    const card = { key, errors: {}, controls: {}, ...def };
    cards[key] = card;
    return card;
  };
  const numberInput = (card, name, label, { help, suffix, attrs = {} } = {}) => {
    const id = `${uid}-${name}`;
    const input = h("input", { class: "input", id, name, type: "number", inputmode: "numeric", autocomplete: "off",
      "aria-describedby": `${id}-help ${id}-error`, ...attrs });
    card.controls[name] = input;
    return h("div", { class: "field" },
      h("label", { class: "field__label", for: id }, label),
      suffix ? h("div", { class: "affix" }, input, h("span", { class: "affix__text", "aria-hidden": "true" }, suffix)) : input,
      h("p", { class: "field__help", id: `${id}-help` }, help || ""),
      errorSlot(card, name));
  };
  const intOrNull = (input) => {
    const raw = input.value.trim();
    return raw === "" ? null : Number(raw);
  };

  // 1. Age
  const age = define("age", { legend: "Your age", optional: true });
  age.body = [numberInput(age, "age", "Age in years", { help: "Context only. It never sets your risk level or what AlphaForge analyses.",
    attrs: { min: String(AGE_MIN), max: String(AGE_MAX), step: "1" } })];
  age.read = () => ({ age: intOrNull(age.controls.age) });
  age.write = (v) => { age.controls.age.value = v.age ?? ""; };
  age.validate = () => {
    const n = intOrNull(age.controls.age);
    return n !== null && (!Number.isInteger(n) || n < AGE_MIN || n > AGE_MAX)
      ? [["age", `Use a whole number of years between ${AGE_MIN} and ${AGE_MAX}.`]] : [];
  };

  // 2. Risk level, 3. Experience: the 1-to-10 scales
  const scaleCard = (key, legend, field, bands, options, label, help, error) => {
    const card = define(key, { legend, required: true });
    const s = scale(h, { id: `${uid}-${field}`, name: field, label, bands, options, help });
    s.input.setAttribute("aria-describedby", `${s.input.getAttribute("aria-describedby")} ${uid}-${field}-error`);
    card.controls[field] = s.input;
    card.scale = s;
    card.body = [s, errorSlot(card, field)];
    card.read = () => ({ [field]: s.value() });
    card.write = (v) => s.setCategory(v[field] || null);
    card.validate = () => (s.value() ? [] : [[field, error]]);
    return card;
  };
  scaleCard("risk_band", "Risk level", "risk_band", RISK_BANDS, RISK, "Slide from 1 (no loss at all) to 10 (big swings are fine)",
    "How much ups and downs you can live with.", "Pick the risk level that fits you best.");
  scaleCard("experience", "Experience", "experience", EXPERIENCE_BANDS, EXPERIENCE,
    "Slide from 1 (new to investing) to 10 (you follow the markets closely)",
    "It changes how answers are explained, not what is analysed.", "Pick your experience level.");

  // 4. Explain things so I learn
  const learn = define("wants_to_learn", { legend: "Explain things so I learn", optional: true });
  const learnBox = h("input", { class: "switch", type: "checkbox", role: "switch", id: `${uid}-wants_to_learn`, name: "wants_to_learn",
    "aria-describedby": `${uid}-wants_to_learn-help` });
  learn.controls.wants_to_learn = learnBox;
  learn.body = [h("div", { class: "switch-row" },
    h("div", { class: "switch-row__text" },
      h("label", { for: `${uid}-wants_to_learn`, class: "choice__title" }, "Explain the terms and the reasoning"),
      h("span", { class: "small muted", id: `${uid}-wants_to_learn-help` },
        "Answers explain the terms they use and why the numbers matter. For beginner and intermediate levels; advanced answers already assume the terms.")),
    learnBox)];
  learn.read = () => ({ wants_to_learn: learnBox.checked });
  learn.write = (v) => { learnBox.checked = Boolean(v.wants_to_learn); };
  learn.validate = () => [];

  // 5. Horizon
  const hz = define("horizon", { legend: "Investment horizon", optional: true });
  hz.body = [h("p", { class: "legend-help" }, "When you expect to need the money. Leave the second box empty for “or more”."),
    h("div", { class: "input-row" },
      numberInput(hz, "horizon_min", "From", { suffix: "years", attrs: { min: "0", max: "60", step: "1" } }),
      numberInput(hz, "horizon_max", "To", { suffix: "years", attrs: { min: "0", max: "60", step: "1" } }))];
  hz.read = () => ({ horizon_years: { min: intOrNull(hz.controls.horizon_min), max: intOrNull(hz.controls.horizon_max) } });
  hz.write = (v) => {
    hz.controls.horizon_min.value = v.horizon_years?.min ?? "";
    hz.controls.horizon_max.value = v.horizon_years?.max ?? "";
  };
  hz.validate = () => {
    const { min, max } = hz.read().horizon_years;
    const bad = [];
    for (const [name, val] of [["horizon_min", min], ["horizon_max", max]]) {
      if (val !== null && (!Number.isInteger(val) || val < 0 || val > 60)) bad.push([name, "Use whole years between 0 and 60."]);
    }
    if (!bad.length && min !== null && max !== null && max < min) bad.push(["horizon_max", "The end can't be before the start."]);
    if (!bad.length && max !== null && min === null) bad.push(["horizon_min", "Add the start of your horizon too."]);
    return bad;
  };

  // 6. Capital and largest loss
  const money = define("money", { legend: "Capital and largest acceptable loss", optional: true });
  money.body = [h("p", { class: "legend-help" }, "Simulated amounts only. Nothing is invested."),
    h("div", { class: "input-row" },
      numberInput(money, "capital", "Capital", { suffix: "CAD", help: "Example: 150000", attrs: { min: "1", step: "1000" } }),
      numberInput(money, "loss_capacity", "Largest loss you could accept", { suffix: "%", help: "Example: 15",
        attrs: { min: "0", max: "100", step: "1", inputmode: "decimal" } }))];
  money.read = () => {
    const loss = intOrNull(money.controls.loss_capacity);
    return { capital: intOrNull(money.controls.capital), loss_capacity: loss === null ? null : loss / 100 };
  };
  money.write = (v) => {
    money.controls.capital.value = v.capital ?? "";
    money.controls.loss_capacity.value = percent(v.loss_capacity);
  };
  money.validate = () => {
    const { capital, loss_capacity: loss } = money.read();
    const bad = [];
    if (capital !== null && !(capital > 0)) bad.push(["capital", "Use an amount above zero, without spaces."]);
    if (loss !== null && !(loss >= 0 && loss <= 1)) bad.push(["loss_capacity", "Use a percentage between 0 and 100."]);
    return bad;
  };

  // 7. Goal
  const goal = define("objective", { legend: "Your goal", optional: true });
  const goalInput = h("input", { class: "input", id: `${uid}-objective`, name: "objective", type: "text", maxlength: "200",
    autocomplete: "off", "aria-describedby": `${uid}-objective-help ${uid}-objective-error` });
  goal.controls.objective = goalInput;
  goal.body = [h("div", { class: "field" },
    h("label", { class: "field__label", for: `${uid}-objective` }, "What is this money for?"), goalInput,
    h("p", { class: "field__help", id: `${uid}-objective-help` }, "Example: grow my savings for a house in ten years."),
    errorSlot(goal, "objective"))];
  goal.read = () => ({ objective: goalInput.value.trim() || null });
  goal.write = (v) => { goalInput.value = v.objective || ""; };
  goal.validate = () => [];

  // 8. Preferences and constraints (free text; the server derives the two lists)
  const prefs = define("preferences", { legend: "Preferences and constraints", optional: true });
  const prefsInput = h("textarea", { class: "textarea", id: `${uid}-preferences_text`, name: "preferences_text", rows: "4",
    maxlength: String(PREFERENCES_MAX), "aria-describedby": `${uid}-preferences_text-help ${uid}-preferences_text-error` });
  prefs.controls.preferences_text = prefsInput;
  const understood = h("div", { class: "understood", hidden: true });
  prefs.body = [h("div", { class: "field" },
    h("label", { class: "field__label", for: `${uid}-preferences_text` }, "Preferences and constraints ", h("span", { class: "optional" }, "(optional)")),
    prefsInput,
    h("p", { class: "field__help", id: `${uid}-preferences_text-help` }, PREFERENCES_HELP),
    errorSlot(prefs, "preferences_text")), understood];
  prefs.read = () => ({ preferences_text: prefsInput.value });
  prefs.write = (v) => { prefsInput.value = v.preferences_text || ""; };
  prefs.validate = () => (prefsInput.value.length > PREFERENCES_MAX
    ? [["preferences_text", `Keep it under ${PREFERENCES_MAX.toLocaleString("en-CA")} characters.`]] : []);
  /** "AlphaForge understood — Preferences: … / Constraints: …": what the server derived, as two plain lists. */
  prefs.showUnderstood = (profile) => {
    const p = (profile && profile.preferences) || [];
    const c = (profile && profile.constraints) || [];
    if (!p.length && !c.length) { understood.hidden = true; understood.replaceChildren(); return; }
    const list = (title, items) => [h("p", { class: "understood__title" }, title),
      items.length ? h("ul", { class: "understood__list" }, items.map((x) => h("li", null, x))) : h("p", { class: "small muted" }, "None")];
    understood.replaceChildren(h("p", { class: "understood__lead" }, "AlphaForge understood"), ...list("Preferences", p), ...list("Constraints", c));
    understood.hidden = false;
  };

  // 9. Hard limits
  const limits = define("limits", { legend: "Hard limits", optional: true });
  const limitBoxes = LIMITS.map((l) => h("input", { type: "checkbox", name: `limit-${l.key}`, id: `${uid}-limit-${l.key}` }));
  limits.controls.limits = limitBoxes[0];
  limits.body = [h("p", { class: "legend-help" }, "Rules applied to your portfolio and to every answer, whatever you ask."),
    h("div", { class: "choices" }, LIMITS.map((l, i) => h("label", { class: "choice" }, limitBoxes[i],
      h("span", { class: "choice__title" }, l.title), h("span", { class: "choice__desc" }, l.desc))))];
  let otherExclusions = [];   // exclusions this form has no checkbox for: kept as they are
  limits.read = () => ({
    exclusions: [...LIMITS.filter((l, i) => limitBoxes[i].checked).map((l) => l.exclusion), ...otherExclusions],
    agent_toggles: Object.fromEntries(LIMITS.filter((l) => l.toggle).map((l) => [l.toggle, limitBoxes[LIMITS.indexOf(l)].checked])),
  });
  limits.write = (v) => {
    LIMITS.forEach((l, i) => { limitBoxes[i].checked = (v.exclusions || []).some((e) => e.type === l.exclusion.type); });
    otherExclusions = (v.exclusions || []).filter((e) => !LIMITS.some((l) => l.exclusion.type === e.type));
  };
  limits.validate = () => [];

  // 10. Analyses
  const analyses = define("analyses", { legend: "Analyses", optional: true });
  const switches = ANALYSES.map((a) => h("input", { class: "switch", type: "checkbox", role: "switch", id: `${uid}-toggle-${a}`, name: `toggle-${a}` }));
  const quotes = {};   // a = the user's words when a chat message changed it
  analyses.body = [h("p", { class: "legend-help" }, "On means AlphaForge may use it when a question needs it: it doesn't run for every question. "
    + "Off means it is skipped, unless a question can't be answered without it (the answer then says why). You can also say it in a question, like “I don't care about the news”."),
  h("div", { class: "panel panel--plain" }, ANALYSES.map((a, i) => h("div", { class: "switch-row" },
    h("div", { class: "switch-row__text" },
      h("label", { for: `${uid}-toggle-${a}`, class: "choice__title" }, AGENTS[a].name),
      h("span", { class: "small muted" }, AGENTS[a].desc),
      (quotes[a] = h("span", { class: "small", hidden: true }))),
    switches[i])))];
  analyses.controls.analyses = switches[0];
  analyses.read = () => ({ agent_toggles: Object.fromEntries(ANALYSES.map((a, i) => [a, switches[i].checked])) });
  analyses.write = (v) => {
    ANALYSES.forEach((a, i) => { switches[i].checked = !v.agent_toggles || v.agent_toggles[a] !== false; });
    const reasons = v.agent_toggle_reasons || {};
    ANALYSES.forEach((a) => {
      quotes[a].hidden = !reasons[a];
      quotes[a].replaceChildren(reasons[a] ? `Changed from chat: “${reasons[a]}”` : "");
    });
  };
  analyses.validate = () => [];
  return cards;
}

/* ---------------------------------------- The step-by-step form ---------------------------------------- */

/**
 * ctx: { h, icon, alertBox, RISK, EXPERIENCE, LIMITS, ANALYSES, AGENTS, scale?,
 *        hasProfile(): boolean, save(payload, {complete}): Promise<{profile, building}|profile>,
 *        persist({answered, skipped, draft}), setDirty(bool),
 *        startChoice?(): element offered when save() answers {choose: true} (a blank profile just completed) }
 * opts: { saved: server profile or null, record: {answered, skipped, draft} or null }
 * Returns { el, cards, stepper, save(key), skip(key), change(key), cancel(key), fillAll(values), saveAll(), discardAll(), read() }.
 */
export function buildProfileSteps(ctx, { saved = null, record = null } = {}) {
  const { h } = ctx;
  const uid = "q";
  const cards = makeCards(ctx, uid);
  const stepper = createStepper({ answered: initialAnswered(saved, record), skipped: record ? record.skipped : [] });
  const live = h("p", { class: "visually-hidden", role: "status", "aria-live": "polite" });
  const status = h("p", { class: "small muted", role: "status" });
  const choice = h("div", { class: "q-choice" });   // a blank profile just completed: how to start (focus moves to it)
  const touched = new Set();   // cards edited since they were last saved
  let savedValues = {};        // the last saved value of each card: what Cancel and Discard restore
  let draft = record && record.draft ? record.draft : null;   // answers waiting for a first save (needs risk and experience)
  let busy = false;

  const noun = (k) => STEP_NOUN[k];
  const announce = (text) => { live.textContent = ""; live.textContent = text; };
  const fieldset = {};
  const slots = {};

  for (const key of stepper.order) {
    const card = cards[key];
    const legendId = `${uid}-${key}-legend`;
    const marker = card.required ? " (required)" : " (optional)";
    slots[key] = { alert: h("div", { class: "q-card__alert" }), actions: h("div", { class: "q-card__actions" }),
      body: h("div", { class: "q-card__body stack-sm" }, card.body) };
    fieldset[key] = h("fieldset", { class: "q-card", id: `${uid}-card-${key}`, "data-step": key },
      h("legend", { id: legendId }, card.legend, h("span", { class: "optional" }, marker)),
      slots[key].body, slots[key].alert, slots[key].actions);
  }

  /** Every input of a card, found from the card element (limits and analyses have several). */
  function inputsOf(key) {
    const out = [];
    const walk = (el) => { for (const c of el.children || []) { if (["INPUT", "TEXTAREA", "SELECT"].includes(c.tagName)) out.push(c); walk(c); } };
    walk(slots[key].body);
    return out;
  }
  const firstControl = (key) => inputsOf(key).find((c) => !c.disabled) || null;

  function readAll() {
    const out = { language: "en" };   // English only (Profile.language)
    for (const key of stepper.order) Object.assign(out, cards[key].read());
    // The ESG limit and the analyses are both agent toggles: merge them (a limit wins for its own agent).
    out.agent_toggles = { ...cards.analyses.read().agent_toggles, ...cards.limits.read().agent_toggles };
    return out;
  }
  /** The body of PUT /v1/profile: the whole form, without the empty values the server would ignore anyway. */
  function payloadOf(data) {
    const p = {};
    for (const [k, v] of Object.entries(data)) if (v !== null && v !== undefined) p[k] = v;
    if (data.horizon_years.min === null) delete p.horizon_years;
    if (typeof p.preferences_text === "string" && !p.preferences_text.trim()) p.preferences_text = "";   // "" clears it on the server
    return p;
  }

  /* ----- drawing ----- */
  function setLocked(key, locked) {
    for (const input of inputsOf(key)) {
      input.disabled = locked;
      if (locked) input.setAttribute("aria-readonly", "true"); else input.removeAttribute("aria-readonly");
    }
  }
  const button = (text, { primary = false, label, type = "button", onclick } = {}) =>
    h("button", { class: `btn ${primary ? "btn--primary" : "btn--secondary"} btn--sm`, type, "aria-label": label, onclick }, text);
  function draw(key) {
    const st = stepper.status(key);
    const fs = fieldset[key];
    const locked = st === "locked";
    fs.classList.toggle("q-card--locked", locked);
    fs.classList.toggle("q-card--open", st === "open" || st === "editing" || st === "unsaved");
    fs.classList.toggle("q-card--pending", st === "pending");
    slots[key].body.hidden = st === "pending";
    setLocked(key, locked);
    if (st === "pending") fs.setAttribute("aria-disabled", "true"); else fs.removeAttribute("aria-disabled");
    const acts = [];
    const n = noun(key).toLowerCase();
    if (locked && stepper.isSkipped(key)) acts.push(h("span", { class: "small muted" }, "Skipped. Press Change to answer."));
    if (locked) acts.push(button("Change", { label: `Change ${n}`, onclick: () => change(key) }));
    else if (st === "editing") {
      acts.push(button("Cancel", { label: `Cancel changing ${n}`, onclick: () => cancel(key) }),
        button("Save", { primary: true, label: `Save ${n}`, onclick: () => save(key) }));
    } else if (st === "open") {
      if (!stepper.isRequired(key)) acts.push(button("Skip", { label: `Skip ${n}`, onclick: () => skip(key) }));
      acts.push(button("Save", { primary: true, label: `Save ${n}`, onclick: () => save(key) }));
    }
    slots[key].actions.replaceChildren(...acts);
    slots[key].actions.hidden = acts.length === 0;
  }
  const drawAll = () => { for (const key of stepper.order) draw(key); bar.hidden = !stepper.isBulk(); };

  function clearErrors(key) {
    for (const el of Object.values(cards[key].errors)) el.replaceChildren();
    for (const input of inputsOf(key)) input.removeAttribute("aria-invalid");
    slots[key].alert.replaceChildren();
  }
  function showError(key, name, message) {
    const slot = cards[key].errors[name];
    if (!slot) return null;
    slot.replaceChildren(ctx.icon("alert", 14), h("span", null, message));
    const input = cards[key].controls[name];
    if (input) input.setAttribute("aria-invalid", "true");
    return input || null;
  }
  /** Errors of one card, inline under each field. Returns the first control with an error. */
  function validateCard(key) {
    clearErrors(key);
    const bad = cards[key].validate();
    const targets = bad.map(([name, msg]) => showError(key, name, msg));
    return bad.length ? (targets.find(Boolean) || firstControl(key) || true) : null;
  }
  /** After a save: the next open question gets the focus; when none is left, the saved card's own button does. */
  function focusNext(from) {
    const next = stepper.firstOpen();
    const target = next ? firstControl(next) : null;
    if (target) { target.focus(); return; }
    const own = slots[from].actions.children[0];
    if (own) own.focus();
  }
  const setDirty = () => ctx.setDirty(stepper.isBulk() || touched.size > 0);
  const persist = () => ctx.persist({ ...stepper.snapshot(), draft });

  /* ----- actions ----- */
  async function persistToServer(data, complete) {
    // The server needs a risk level and experience for a first profile: until then the answers wait here, in this browser.
    if (!ctx.hasProfile() && !(data.risk_band && data.experience)) { draft = payloadOf(data); return null; }
    const res = await ctx.save(payloadOf(data), { complete });
    draft = null;
    return res && res.profile ? res : { profile: res, building: false };
  }
  function applyServerLists(res) {
    if (res && res.profile) cards.preferences.showUnderstood(res.profile);
  }
  /** A server refusal (422): each message goes under its own field when the form has it, else in an alert.
   *  `key` is the card being saved (null for "Save all"). Returns the control to focus. */
  function serverErrors(err, key = null) {
    const detail = err && err.status === 422 && Array.isArray(err.detail) ? err.detail : null;
    let focus = null;
    let unmatched = !detail;
    for (const d of detail || []) {
      const field = (d.loc || []).slice(-1)[0];
      const owner = key && cards[key].errors[field] ? key : stepper.order.find((k) => cards[k].errors[field]);
      const el = owner ? showError(owner, field, String(d.msg || "").replace(/^Value error, /, "")) : null;
      if (el) focus = focus || el; else unmatched = true;
    }
    if (unmatched) (key ? slots[key].alert : topAlert).replaceChildren(ctx.alertBox(err, "your profile"));
    return focus;
  }

  async function save(key) {
    if (busy || stepper.isBulk()) return;
    const bad = validateCard(key);
    if (bad) { if (bad !== true) bad.focus(); announce(`${noun(key)}: please check the answer.`); return; }
    busy = true;
    const buttons = slots[key].actions.children;
    for (const b of buttons) b.disabled = true;
    try {
      const complete = stepper.completesWith(key);
      const res = await persistToServer(readAll(), complete);
      applyServerLists(res);
      savedValues[key] = cards[key].read();
      touched.delete(key);
      stepper.save(key);
      persist();
      drawAll();
      setDirty();
      showBuilding(res);
      announce(`${noun(key)} saved`);
      focusNext(key);
    } catch (err) {
      for (const b of buttons) b.disabled = false;
      const target = serverErrors(err, key);
      if (target) target.focus(); else if (slots[key].alert.firstChild) slots[key].alert.firstChild.focus();
    } finally {
      busy = false;
    }
  }
  async function skip(key) {
    if (stepper.isRequired(key) || busy) return;
    clearErrors(key);
    stepper.skip(key);
    persist();
    drawAll();
    announce(`${noun(key)} skipped`);
    focusNext(key);
    // Skipping the last open question completes the profile: save once more so the portfolio gets built from it.
    if (stepper.isComplete() && ctx.hasProfile()) {
      try { showBuilding(await persistToServer(readAll(), true)); } catch (err) { serverErrors(err, key); }
    }
  }
  function showBuilding(res) {
    if (res && res.choose && ctx.startChoice) {
      choice.replaceChildren(h("section", { class: "panel" }, ctx.startChoice()));
      const first = choice.querySelector("button");
      if (first) first.focus();
    } else if (res) {
      choice.replaceChildren();
    }
    status.replaceChildren(...(res && res.building
      ? ["Building your portfolio (about a minute). ", h("a", { href: ctx.homeHref || "#/portfolio" }, "See my portfolio")] : []));
  }
  function change(key) {
    stepper.change(key);
    drawAll();
    const target = firstControl(key);
    if (target) target.focus();
  }
  function cancel(key) {
    cards[key].write(savedValues[key] || {});
    clearErrors(key);
    touched.delete(key);
    stepper.cancel(key);
    drawAll();
    setDirty();
    announce(`${noun(key)} unchanged`);
    const btn = slots[key].actions.children[0];
    if (btn) btn.focus();
  }
  /** A persona or a document: every card gets the values, unsaved, until "Save all". */
  function fillAll(values) {
    for (const key of stepper.order) cards[key].write(values);
    cards.preferences.showUnderstood(null);
    stepper.fillAll();
    for (const key of stepper.order) { clearErrors(key); touched.add(key); }
    drawAll();
    setDirty();
  }
  async function saveAll() {
    if (busy || !stepper.isBulk()) return;
    let first = null;
    for (const key of stepper.order) first = first || validateCard(key);
    if (first) { if (first !== true) first.focus(); announce("Some answers need attention."); return; }
    busy = true;
    saveAllBtn.disabled = true;
    try {
      const res = await persistToServer(readAll(), true);
      applyServerLists(res);
      for (const key of stepper.order) savedValues[key] = cards[key].read();
      touched.clear();
      stepper.saveAll();
      persist();
      drawAll();
      setDirty();
      showBuilding(res);
      announce("Profile saved");
      const own = slots[stepper.order[0]].actions.children[0];
      if (own) own.focus();
    } catch (err) {
      const target = serverErrors(err);
      if (target) target.focus(); else if (topAlert.firstChild) topAlert.firstChild.focus();
    } finally {
      busy = false;
      saveAllBtn.disabled = false;
    }
  }
  function discardAll() {
    for (const key of stepper.order) { cards[key].write(savedValues[key] || {}); clearErrors(key); }
    touched.clear();
    stepper.discardAll();
    drawAll();
    setDirty();
    announce("Changes discarded");
  }

  /* ----- assemble ----- */
  const saveAllBtn = button("Save all", { primary: true, label: "Save all answers", onclick: saveAll });
  const discardBtn = h("button", { class: "btn btn--quiet", type: "button", onclick: discardAll }, "Discard changes");
  const bar = h("div", { class: "form-actions", hidden: true }, saveAllBtn, discardBtn);
  const topAlert = h("div");
  const form = h("form", { class: "stack profile-form", novalidate: true, "aria-label": "Your investor profile",
    onsubmit: (e) => {   // Enter in a one-line field saves the card it is in
      e.preventDefault();
      const active = typeof document !== "undefined" ? document.activeElement : null;
      const card = active && active.closest ? active.closest(".q-card") : null;
      const key = card && card.getAttribute("data-step");
      if (key && ["open", "editing"].includes(stepper.status(key))) save(key);
    } },
  stepper.order.map((k) => fieldset[k]), topAlert, bar, status, choice, live);
  const markTouched = (e) => {
    const card = e.target && e.target.closest ? e.target.closest(".q-card") : null;
    const key = card && card.getAttribute("data-step");
    if (!key || stepper.status(key) === "locked" || stepper.status(key) === "pending") return;
    touched.add(key);
    setDirty();
  };
  form.addEventListener("input", markTouched);
  form.addEventListener("change", markTouched);

  // Start from the saved profile, or from the answers kept in this browser while there is no profile yet.
  const start = saved || draft || {};
  for (const key of stepper.order) { cards[key].write(start); savedValues[key] = cards[key].read(); }
  if (saved) cards.preferences.showUnderstood(saved);
  drawAll();

  return { el: form, cards, stepper, status, save, skip, change, cancel, fillAll, saveAll, discardAll, readAll,
    firstControl, fieldset, announceText: () => live.textContent, understood: (profile) => cards.preferences.showUnderstood(profile),
    /** After the server changed something on its own (e.g. a toggle set from chat): show it in the locked cards. */
    refresh(profile) { for (const key of stepper.order) if (stepper.status(key) === "locked") { cards[key].write(profile); savedValues[key] = cards[key].read(); } cards.preferences.showUnderstood(profile); } };
}
