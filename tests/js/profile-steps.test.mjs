// node --test tests/js/: the step-by-step profile form (frontend/assets/profile-steps.js), on a tiny fake DOM.
import { test } from "node:test";
import assert from "node:assert/strict";
import { h } from "./fake-dom.mjs";
import { STEP_ORDER, buildProfileSteps, createStepper, initialAnswered } from "../../frontend/assets/profile-steps.js";

const RISK = [{ value: "low", title: "Low" }, { value: "moderate", title: "Moderate" }, { value: "high", title: "High" }];
const EXPERIENCE = [{ value: "beginner", title: "Beginner" }, { value: "advanced", title: "Advanced" }];
const LIMITS = [
  { key: "crypto", exclusion: { type: "asset_class", value: "crypto" }, title: "No cryptocurrencies", desc: "d" },
  { key: "esg", exclusion: { type: "controversy", min_severity: "major" }, toggle: "esg", title: "Avoid controversies", desc: "d" },
];
const ANALYSES = ["risk", "news"];
const AGENTS = { risk: { name: "Portfolio risk", desc: "d" }, news: { name: "News", desc: "d" } };

/** A scale stand-in: the real one needs a browser. `pick(value)` is the person moving the slider. */
function fakeScale(hh, { id, name, label }) {
  const input = hh("input", { type: "range", id, name, "aria-describedby": `${id}-out` });
  const el = hh("div", { class: "scale" }, hh("label", { for: id }, label), input);
  let current = null;
  el.input = input;
  el.value = () => current;
  el.setCategory = (v) => { current = v; };
  el.pick = (v) => { current = v; input.dispatchEvent("input"); };
  return el;
}

/** The form with a fake server. `profile`: what the server already holds; `record`: what this browser remembers. */
function setup({ profile = null, record = null, existing = null } = {}) {
  const calls = { save: [], persist: [], dirty: [] };
  const server = { profile: profile || existing };
  const ctx = {
    h, icon: () => h("svg"), alertBox: (err) => h("div", { class: "alert", tabindex: "-1" }, String(err.message || err)),
    RISK, EXPERIENCE, LIMITS, ANALYSES, AGENTS, scale: fakeScale,
    hasProfile: () => Boolean(server.profile),
    persist: (rec) => calls.persist.push(rec),
    setDirty: (on) => calls.dirty.push(on),
    async save(payload, meta) {
      calls.save.push({ payload: structuredClone(payload), meta });
      if (server.fail) throw server.fail;
      server.profile = { ...(server.profile || {}), ...payload, ...(server.derived || {}) };
      return { profile: server.profile, building: false };
    },
  };
  const steps = buildProfileSteps(ctx, { saved: profile, record });
  return { steps, calls, server, ctx };
}

const card = (steps, key) => steps.fieldset[key];
const buttonNamed = (steps, key, label) => card(steps, key).find((e) => e.tagName === "BUTTON" && e.getAttribute("aria-label") === label);
const inputs = (steps, key) => card(steps, key).all().filter((e) => ["INPUT", "TEXTAREA"].includes(e.tagName));
const statuses = (steps) => Object.fromEntries(STEP_ORDER.map((k) => [k, steps.stepper.status(k)]));
const openCards = (steps) => STEP_ORDER.filter((k) => steps.stepper.status(k) === "open");
const age = (steps) => steps.cards.age.controls.age;
const fillAge = (steps, v) => { age(steps).value = String(v); age(steps).dispatchEvent("input"); };
const FULL = { risk_band: "moderate", experience: "advanced", language: "en", age: 41 };

/* ---------- the stepper (no DOM) ---------- */

test("the questions come in the agreed order", () => {
  assert.deepEqual(STEP_ORDER, ["age", "risk_band", "experience", "wants_to_learn", "horizon", "money", "objective",
    "preferences", "limits", "analyses"]);
  const { steps } = setup();
  const legends = steps.el.all().filter((e) => e.tagName === "LEGEND").map((e) => e.textContent);
  assert.deepEqual(legends, [
    "Your age (optional)", "Risk level (required)", "Experience (required)", "Explain things so I learn (optional)",
    "Investment horizon (optional)", "Capital and largest acceptable loss (optional)", "Your goal (optional)",
    "Preferences and constraints (optional)", "Hard limits (optional)", "Analyses (optional)"]);
});

test("only the first unanswered question is open, the others wait", () => {
  const s = createStepper();
  assert.equal(s.firstOpen(), "age");
  assert.deepEqual(STEP_ORDER.map((k) => s.status(k)), ["open", ...Array(9).fill("pending")]);
  s.save("age");
  assert.equal(s.status("age"), "locked");
  assert.equal(s.firstOpen(), "risk_band");
});

test("a required question cannot be skipped, an optional one can", () => {
  const s = createStepper();
  assert.throws(() => s.skip("risk_band"));
  assert.equal(s.skip("age"), "risk_band");
  assert.equal(s.isSkipped("age"), true);
  assert.equal(s.isAnswered("age"), true);
});

test("Change unlocks one card and Cancel locks it again, without reopening later questions", () => {
  const s = createStepper({ answered: ["age", "risk_band"] });
  assert.equal(s.status("experience"), "open");
  s.change("age");
  assert.deepEqual([s.status("age"), s.status("risk_band"), s.status("experience"), s.status("wants_to_learn")],
    ["editing", "locked", "open", "pending"]);
  s.cancel("age");
  assert.equal(s.status("age"), "locked");
  s.change("risk_band");
  s.save("risk_band");
  assert.equal(s.firstOpen(), "experience");   // saving an earlier answer does not reopen what follows it
});

test("a question that was never answered cannot be changed", () => {
  const s = createStepper();
  s.change("experience");
  assert.equal(s.status("experience"), "pending");
});

test("a returning user has every answered question locked and the first unanswered open", () => {
  const saved = { risk_band: "high", experience: "beginner", age: null, wants_to_learn: false };
  // saved before this form existed (nothing remembered in the browser): only the new question, age, is asked
  const s = createStepper({ answered: initialAnswered(saved, null) });
  assert.equal(s.firstOpen(), "age");
  assert.deepEqual(STEP_ORDER.filter((k) => s.status(k) === "locked"), STEP_ORDER.filter((k) => k !== "age"));
  // with a record, the browser's memory decides (the person skipped age earlier)
  const r = createStepper({ answered: initialAnswered(saved, { answered: ["age", "risk_band", "experience"], skipped: ["age"] }) });
  assert.equal(r.firstOpen(), "wants_to_learn");
  assert.equal(initialAnswered(null, null).length, 0);
});

/* ---------- the form ---------- */

test("a new user sees one open card, and every card is a fieldset with a legend", () => {
  const { steps } = setup();
  assert.deepEqual(openCards(steps), ["age"]);
  assert.equal(statuses(steps).risk_band, "pending");
  for (const key of STEP_ORDER) {
    assert.equal(card(steps, key).tagName, "FIELDSET");
    assert.ok(card(steps, key).children.some((c) => c.tagName === "LEGEND"));
  }
  assert.ok(buttonNamed(steps, "age", "Skip age"));
  assert.ok(buttonNamed(steps, "age", "Save age"));
  assert.equal(buttonNamed(steps, "risk_band", "Skip risk level"), null);
});

test("Save validates first: an invalid age shows an error under the field and keeps the focus there", async () => {
  const { steps, calls } = setup();
  fillAge(steps, 17);
  await steps.save("age");
  assert.equal(calls.save.length, 0);
  assert.equal(steps.stepper.status("age"), "open");
  assert.match(card(steps, "age").find((e) => e.getAttribute("data-error-for") === "age").textContent, /between 18 and 120/);
  assert.equal(age(steps).getAttribute("aria-invalid"), "true");
  assert.equal(globalThis.document.activeElement, age(steps));
});

test("Save locks the card, opens the next one and moves the focus to it", async () => {
  const { steps } = setup({ existing: { ...FULL, age: null } });   // a profile exists on the server, nothing is answered here
  fillAge(steps, 41);
  await steps.save("age");
  assert.equal(steps.stepper.status("age"), "locked");
  assert.equal(age(steps).disabled, true);
  assert.equal(age(steps).getAttribute("aria-readonly"), "true");
  assert.equal(age(steps).value, "41");                                      // the saved value stays visible
  assert.ok(buttonNamed(steps, "age", "Change age"));
  assert.equal(buttonNamed(steps, "age", "Change age").textContent, "Change");
  assert.deepEqual(openCards(steps), ["risk_band"]);
  assert.equal(globalThis.document.activeElement, steps.cards.risk_band.controls.risk_band);
  assert.equal(steps.announceText(), "Age saved");
  assert.equal(card(steps, "age").find((e) => e.getAttribute("class") === "q-card__actions").children.length, 1);   // Change, alone
});

test("every Save sends the whole form, and the first profile waits for risk level and experience", async () => {
  const { steps, calls, server } = setup();
  fillAge(steps, 41);
  await steps.save("age");
  steps.cards.risk_band.scale.pick("moderate");
  await steps.save("risk_band");
  assert.equal(calls.save.length, 0);                                        // the server needs both to create a profile
  const waiting = calls.persist.at(-1);
  assert.equal(waiting.draft.age, 41);                                        // ...but nothing is lost: kept in this browser
  assert.equal(waiting.draft.risk_band, "moderate");
  steps.cards.experience.scale.pick("advanced");
  await steps.save("experience");
  assert.equal(calls.save.length, 1);
  assert.equal(calls.save[0].payload.age, 41);
  assert.equal(calls.save[0].payload.language, "en");                        // required by the server's Profile
  assert.equal(calls.save[0].payload.risk_band, "moderate");
  assert.equal(calls.save[0].payload.experience, "advanced");
  assert.equal(calls.save[0].meta.complete, false);
  assert.equal(calls.persist.at(-1).draft, null);
  assert.equal(server.profile.age, 41);
  steps.cards.objective.controls.objective.value = "a house";
  await steps.save("wants_to_learn");
  assert.equal(calls.save.at(-1).payload.objective, "a house");              // later saves carry what is already typed
});

test("a required question needs an answer before it can be saved", async () => {
  const { steps, calls } = setup({ existing: { ...FULL }, record: { answered: ["age"], skipped: [] } });
  assert.deepEqual(openCards(steps), ["risk_band"]);
  await steps.save("risk_band");
  assert.equal(calls.save.length, 0);
  assert.match(card(steps, "risk_band").find((e) => e.getAttribute("data-error-for") === "risk_band").textContent, /Pick the risk level/);
});

test("Skip answers an optional question without a value and opens the next one", () => {
  const { steps } = setup();
  buttonNamed(steps, "age", "Skip age").click();
  assert.equal(steps.stepper.status("age"), "locked");
  assert.deepEqual(openCards(steps), ["risk_band"]);
  assert.equal(steps.announceText(), "Age skipped");
});

test("Change unlocks only that card; Cancel puts the saved value back and locks it", async () => {
  const profile = { ...FULL };
  const { steps } = setup({ profile, record: { answered: STEP_ORDER, skipped: [] } });
  assert.deepEqual(openCards(steps), []);
  assert.ok(STEP_ORDER.every((k) => steps.stepper.status(k) === "locked"));
  buttonNamed(steps, "age", "Change age").click();
  assert.equal(steps.stepper.status("age"), "editing");
  assert.equal(age(steps).disabled, false);
  assert.equal(globalThis.document.activeElement, age(steps));
  assert.ok(STEP_ORDER.filter((k) => k !== "age").every((k) => steps.stepper.status(k) === "locked"));
  const names = card(steps, "age").find((e) => e.getAttribute("class") === "q-card__actions").children.map((b) => b.textContent);
  assert.deepEqual(names, ["Cancel", "Save"]);
  fillAge(steps, 70);
  buttonNamed(steps, "age", "Cancel changing age").click();
  assert.equal(age(steps).value, "41");
  assert.equal(steps.stepper.status("age"), "locked");
  assert.equal(age(steps).disabled, true);
  assert.equal(globalThis.document.activeElement, buttonNamed(steps, "age", "Change age"));
});

test("Change then Save re-saves that card only, and no later card opens", async () => {
  const { steps, calls } = setup({ profile: { ...FULL }, record: { answered: ["age", "risk_band", "experience"], skipped: [] } });
  assert.deepEqual(openCards(steps), ["wants_to_learn"]);
  buttonNamed(steps, "age", "Change age").click();
  fillAge(steps, 52);
  await steps.save("age");
  assert.equal(calls.save.at(-1).payload.age, 52);
  assert.equal(steps.stepper.status("age"), "locked");
  assert.deepEqual(openCards(steps), ["wants_to_learn"]);
  assert.equal(steps.stepper.status("horizon"), "pending");
});

test("no answer is ever echoed: no bubble, and a saved value appears only inside its own card", async () => {
  const { steps } = setup({ existing: { ...FULL, age: null } });
  fillAge(steps, 59);
  await steps.save("age");
  const all = steps.el.all();
  assert.equal(all.filter((e) => /\b(msg|bubble|chat)/.test(e.className)).length, 0);
  const outside = steps.el.children.filter((e) => e.tagName !== "FIELDSET");
  assert.ok(outside.every((e) => !e.textContent.includes("59")));
  assert.ok(card(steps, "age").all().some((e) => e.value === "59"));
});

test("a refusal from the server stays on the card, under the right field", async () => {
  const { steps, server } = setup({ profile: { ...FULL }, record: { answered: ["age", "risk_band", "experience"], skipped: [] } });
  server.fail = Object.assign(new Error("x"), { status: 422, detail: [{ loc: ["preferences_text"], msg: "Too long for the server" }] });
  for (const k of ["wants_to_learn", "horizon", "money", "objective"]) steps.stepper.save(k);   // earlier questions answered
  steps.cards.preferences.controls.preferences_text.value = "text";
  await steps.save("preferences");
  assert.equal(steps.stepper.status("preferences"), "open");
  assert.match(card(steps, "preferences").find((e) => e.getAttribute("data-error-for") === "preferences_text").textContent, /Too long/);
  assert.equal(globalThis.document.activeElement, steps.cards.preferences.controls.preferences_text);
});

test("Preferences and constraints: the raw text goes to the server and what was understood shows under the field", async () => {
  const text = "I want some diversification and not only tech;\nlittle interest in very speculative investments.";
  const profile = { ...FULL };
  const { steps, calls, server } = setup({ profile, record: { answered: ["age", "risk_band", "experience", "wants_to_learn", "horizon", "money", "objective"], skipped: [] } });
  server.derived = { preferences: ["I want some diversification"], constraints: ["not only tech", "little interest in very speculative investments"] };
  const box = steps.cards.preferences.controls.preferences_text;
  assert.equal(box.tagName, "TEXTAREA");
  box.value = text;
  box.dispatchEvent("input");
  await steps.save("preferences");
  assert.equal(calls.save.at(-1).payload.preferences_text, text);             // unchanged, not trimmed or rewritten
  const shown = card(steps, "preferences").find((e) => e.className === "understood");
  assert.equal(shown.hidden, false);
  assert.match(shown.textContent, /AlphaForge understood/);
  assert.match(shown.textContent, /Preferences.*I want some diversification/);
  assert.match(shown.textContent, /Constraints.*not only tech.*little interest in very speculative investments/);
  assert.equal(shown.all().filter((e) => e.tagName === "UL").length, 2);       // two plain lists
});

test("clearing the text sends an empty string, which empties the lists on the server", async () => {
  const { steps, calls } = setup({ profile: { ...FULL, preferences_text: "I like dividends" }, record: { answered: STEP_ORDER, skipped: [] } });
  buttonNamed(steps, "preferences", "Change preferences and constraints").click();
  steps.cards.preferences.controls.preferences_text.value = "  ";
  await steps.save("preferences");
  assert.equal(calls.save.at(-1).payload.preferences_text, "");
});

test("the help text and label of the preferences field are the agreed words", () => {
  const { steps } = setup();
  const text = card(steps, "preferences").textContent;
  assert.match(text, /Preferences and constraints/);
  assert.match(text, /In your own words: what you like, what you want to avoid, anything AlphaForge should keep in mind\. Example: I want some diversification and not only tech; little interest in very speculative investments\./);
  assert.ok(!/Interests/.test(steps.el.textContent));
});

test("a persona fills every card unsaved; Save all saves them together and locks them", async () => {
  const mateo = { age: 41, risk_band: "moderate", experience: "intermediate", language: "en", horizon_years: { min: 8, max: 12 },
    capital: 150000, loss_capacity: null, objective: "Grow", preferences_text: "ESG matters to me.", wants_to_learn: false,
    exclusions: [{ type: "controversy", min_severity: "major" }], agent_toggles: { risk: true, news: true, esg: true } };
  const { steps, calls } = setup({ profile: { ...FULL }, record: { answered: STEP_ORDER, skipped: [] } });
  steps.fillAll(mateo);
  assert.ok(STEP_ORDER.every((k) => steps.stepper.status(k) === "unsaved"));
  assert.equal(calls.dirty.at(-1), true);                                      // the unsaved-changes guard is armed
  assert.equal(age(steps).disabled, false);
  assert.equal(age(steps).value, "41");
  assert.equal(calls.save.length, 0);
  const saveAll = steps.el.find((e) => e.getAttribute("aria-label") === "Save all answers");
  assert.equal(saveAll.textContent, "Save all");
  assert.equal(saveAll.parentNode.hidden, false);
  saveAll.click();
  await new Promise((r) => { setTimeout(r, 0); });
  assert.equal(calls.save.length, 1);                                          // one request for everything
  const p = calls.save[0].payload;
  assert.equal(p.age, 41);
  assert.equal(p.preferences_text, "ESG matters to me.");
  assert.deepEqual(p.horizon_years, { min: 8, max: 12 });
  assert.equal(p.capital, 150000);
  assert.deepEqual(p.exclusions, [{ type: "controversy", min_severity: "major" }]);
  assert.equal(calls.save[0].meta.complete, true);
  assert.ok(STEP_ORDER.every((k) => steps.stepper.status(k) === "locked"));
  assert.equal(calls.dirty.at(-1), false);
  assert.equal(saveAll.parentNode.hidden, true);
  assert.equal(steps.announceText(), "Profile saved");
});

test("Discard changes after a persona fill puts the saved answers back", () => {
  const { steps } = setup({ profile: { ...FULL }, record: { answered: STEP_ORDER, skipped: [] } });
  steps.fillAll({ ...FULL, age: 70 });
  assert.equal(age(steps).value, "70");
  steps.discardAll();
  assert.equal(age(steps).value, "41");
  assert.ok(STEP_ORDER.every((k) => steps.stepper.status(k) === "locked"));
  assert.equal(age(steps).disabled, true);
});

test("typing in an open card arms the unsaved-changes guard, saving clears it", async () => {
  const { steps, calls } = setup({ existing: { ...FULL, age: null } });
  assert.deepEqual(calls.dirty, []);
  fillAge(steps, 30);
  assert.equal(calls.dirty.at(-1), true);
  await steps.save("age");
  assert.equal(calls.dirty.at(-1), false);
});

test("Enter in a one-line field saves its card", async () => {
  const { steps, calls } = setup({ existing: { ...FULL, age: null } });
  fillAge(steps, 33);
  age(steps).focus();
  steps.el.dispatchEvent("submit");
  await new Promise((r) => { setTimeout(r, 0); });
  assert.equal(calls.save.at(-1).payload.age, 33);
});

test("every control of a locked card is disabled and read-only, and nothing is left to focus but Change", async () => {
  const { steps } = setup({ profile: { ...FULL, exclusions: [{ type: "asset_class", value: "crypto" }] }, record: { answered: STEP_ORDER, skipped: [] } });
  for (const key of STEP_ORDER) {
    for (const input of inputs(steps, key)) {
      assert.equal(input.disabled, true, `${key} control is disabled`);
      assert.equal(input.getAttribute("aria-readonly"), "true");
    }
  }
  const crypto = steps.el.find((e) => e.getAttribute("name") === "limit-crypto");
  assert.equal(crypto.checked, true);
  assert.ok(STEP_ORDER.every((k) => buttonNamed(steps, k, `Change ${{
    age: "age", risk_band: "risk level", experience: "experience", wants_to_learn: "learning explanations",
    horizon: "investment horizon", money: "capital and largest loss", objective: "goal",
    preferences: "preferences and constraints", limits: "hard limits", analyses: "analyses" }[k]}`)));
});

test("the ESG hard limit and the analyses are merged into one set of agent toggles", async () => {
  const { steps, calls } = setup({ profile: { ...FULL }, record: { answered: ["age", "risk_band", "experience"], skipped: [] } });
  steps.el.find((e) => e.getAttribute("name") === "limit-esg").checked = true;
  steps.el.find((e) => e.getAttribute("name") === "toggle-news").checked = false;
  await steps.save("wants_to_learn");
  assert.deepEqual(calls.save.at(-1).payload.agent_toggles, { risk: true, news: false, esg: true });
});

test("a live region announces each save politely", () => {
  const { steps } = setup();
  const live = steps.el.find((e) => e.getAttribute("aria-live") === "polite");
  assert.ok(live);
  assert.equal(live.getAttribute("role"), "status");
});

test("skipping the last open question completes the profile and saves once more (the portfolio build starts from it)", async () => {
  const { steps, calls } = setup({ profile: { ...FULL }, record: { answered: STEP_ORDER.slice(0, -1), skipped: [] } });
  assert.deepEqual(openCards(steps), ["analyses"]);
  await steps.skip("analyses");
  assert.equal(calls.save.length, 1);
  assert.equal(calls.save[0].meta.complete, true);
});
