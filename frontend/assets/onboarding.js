/* Profile set-up as a conversation (#/profile when there is no profile, #/profile/assistant any time).
 *
 * AlphaForge asks, the person answers in the chat: risk level and experience on 1-to-10 scales (each lands in a
 * category), horizon, money, goal, whether to explain so they learn, hard limits and every analysis (on or off).
 * Or the person drops a PDF / Word file: the API reads it (POST /v1/profile/draft/file), the chat says what it
 * found and what was irrelevant, then asks only what is still missing. Nothing is saved until "Save my profile"
 * (PUT /v1/profile), which also starts building the portfolio. The form (#/profile/form) stays for edits. */

import { EXPERIENCE_BANDS, RISK_BANDS, scale } from "./scale.js";

const STEPS = ["risk_band", "experience", "horizon_years", "money", "objective", "wants_to_learn", "exclusions", "agent_toggles"];
const sleep = (ms) => new Promise((r) => { window.setTimeout(r, ms); });

export function renderOnboarding(main, ctx) {
  const { h, icon } = ctx;
  const answers = { language: "en" };
  const log = h("div", { class: "chat__log onboarding", role: "log", "aria-label": "Setting up your profile" });
  let queue = [];        // the steps still to ask
  const done = [];       // [{step, aiEl, youEl}] in order, for "Change"

  main.append(
    ctx.band("head", h("div", { class: "page-head page-head--split" },
      h("div", { class: "stack-sm" }, h("h1", null, "Set up your profile"),
        h("p", null, "A short conversation: AlphaForge asks, you answer. Your profile decides what is analysed and how it is explained.")),
      h("a", { class: "btn btn--secondary btn--sm", href: "#/profile/form" }, "Use the form instead"))),
    ctx.band("body", h("div", { class: "onboarding__wrap" }, log)));

  const ai = (...content) => h("div", { class: "msg msg--ai" }, h("span", { class: "msg__avatar", "aria-hidden": "true" }, "A"),
    h("div", { class: "msg__body" }, h("span", { class: "visually-hidden" }, "AlphaForge: "), content));
  const you = (text, step) => h("div", { class: "msg msg--user" }, h("div", { class: "msg__bubble" },
    h("span", { class: "visually-hidden" }, "You answered: "), text,
    step ? h("button", { class: "msg__change", type: "button", onclick: () => rewind(step) }, "Change") : null));
  const add = (el) => { log.append(el); el.scrollIntoView({ block: "end", behavior: "smooth" }); return el; };
  const thinking = (text) => add(ai(h("div", { class: "thinking", role: "status" },
    h("span", { class: "thinking__dots", "aria-hidden": "true" }, h("span"), h("span"), h("span")), h("span", null, text))));

  /* ---------- Start: questions or a document ---------- */
  function start() {
    const file = h("input", { type: "file", id: "profile-file", accept: ".pdf,.docx,.txt,.md", class: "visually-hidden",
      onchange: () => { if (file.files[0]) readDocument(file.files[0]); } });
    // A small round "attach" button next to the main choice; a file can also be dropped on this message.
    const attach = h("label", { class: "attach-btn", for: "profile-file", title: "Attach a PDF or Word file" },
      icon("file", 20), h("span", { class: "visually-hidden" }, "Attach a PDF or Word file about your situation"), file);
    const ask = h("button", { class: "btn btn--primary", type: "button", onclick: () => { lock(); queue = [...STEPS]; add(you("I'll answer the questions.")); next(); } }, "Answer the questions");
    const lock = () => { ask.disabled = true; file.disabled = true; attach.classList.add("is-done"); msg.classList.remove("is-over"); };
    const msg = add(ai(h("p", null, "Hi, I'm AlphaForge. Let's set up your investor profile. It takes about two minutes."),
      h("p", null, "Answer a few questions, or attach a document about your situation (PDF or Word, for example notes from an advisor): I'll read it first, then ask only what's missing."),
      h("div", { class: "onboarding__choices" }, ask, attach),
      h("p", { class: "small muted" }, "A document is read to fill in your profile and is not kept. Nothing is saved until you review and save.")));
    for (const ev of ["dragenter", "dragover"]) msg.addEventListener(ev, (e) => { if (file.disabled) return; e.preventDefault(); msg.classList.add("is-over"); });
    for (const ev of ["dragleave", "drop"]) msg.addEventListener(ev, (e) => { e.preventDefault(); msg.classList.remove("is-over"); });
    msg.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f && !file.disabled) readDocument(f); });
    start.lock = lock;
  }

  async function readDocument(f) {
    start.lock();
    add(you(h("span", { class: "msg__file" }, icon("file", 16), f.name)));
    const wait = thinking("Reading your document…");
    const form = new FormData();
    form.append("file", f);
    let r;
    try {
      r = (await ctx.upload("/v1/profile/draft/file", form)).data;
    } catch (err) {
      wait.replaceWith(ai(ctx.alertBox(err, "your document", null),
        h("div", { class: "notice" }, h("button", { class: "btn btn--secondary btn--sm", type: "button",
          onclick: () => { queue = [...STEPS]; next(); } }, "Answer the questions instead"))));
      return;
    }
    if (r.refused) {
      wait.replaceWith(ai(h("p", null, "I can't use this document. Let's do it with questions instead.")));
      queue = [...STEPS]; next(); return;
    }
    Object.assign(answers, r.fields);
    const found = r.found.map((k) => describe(k)).filter(Boolean);
    wait.replaceWith(ai(
      r.summary ? h("p", null, `This looks like: ${r.summary}`) : null,
      found.length ? [h("p", null, "Here's what I found:"), h("dl", { class: "facts" }, found.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]))]
        : h("p", null, "I couldn't find anything about your investing profile in it."),
      r.irrelevant.length ? [h("p", null, "Not relevant to your profile, so I ignored it:"),
        h("ul", { class: "warning-list" }, r.irrelevant.map((x) => h("li", null, x)))]
        : found.length ? h("p", { class: "small muted" }, "Everything in it was relevant.") : null,
      r.missing.length ? h("p", null, "A few questions to complete it.") : null));
    // Ask what is missing, and always the analyses (the person decides what AlphaForge looks at).
    queue = STEPS.filter((s) => s === "agent_toggles" || (s === "money" ? r.missing.includes("capital") || r.missing.includes("loss_capacity") : r.missing.includes(s)));
    await sleep(400);
    next();
  }

  /* ---------- One question at a time ---------- */
  function next() {
    const step = queue.shift();
    if (!step) { review(); return; }
    const q = QUESTIONS[step](ctx, answers, (text, values) => {
      Object.assign(answers, values);
      q.querySelectorAll("input, button, select, textarea").forEach((x) => { x.disabled = true; });
      q.classList.add("is-answered");
      const bubble = add(you(text, step));
      done.push({ step, aiEl: q, youEl: bubble });
      window.setTimeout(next, 250);
    });
    add(ai(q));
    const first = q.querySelector("input, button");
    if (first) first.focus({ preventScroll: true });
  }

  /** "Change": go back to a step, drop what came after, ask again from there. */
  function rewind(step) {
    const i = done.findIndex((d) => d.step === step);
    if (i < 0) return;
    const keep = done.slice(0, i);
    const from = done[i].aiEl.closest(".msg");
    while (from.nextSibling) from.nextSibling.remove();
    from.remove();
    queue = [step, ...done.slice(i + 1).map((d) => d.step)];
    done.length = 0; done.push(...keep);
    next();
  }

  function review() {
    const items = ["risk_band", "experience", "horizon_years", "capital", "loss_capacity", "objective", "wants_to_learn", "exclusions", "agent_toggles"]
      .map(describe).filter(Boolean);
    const status = h("p", { class: "small", role: "status" });
    const save = h("button", { class: "btn btn--primary", type: "button", onclick: async () => {
      save.disabled = true;
      save.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving");
      try {
        const before = await ctx.loadProfile(true).catch(() => null);
        const fresh = (await ctx.api("PUT", "/v1/profile", answers)).data;
        ctx.onSaved(before, fresh);
        add(ai(h("p", null, "Saved. I'm building your portfolio from it now: it takes about a minute."),
          h("div", { class: "notice" }, h("a", { class: "btn btn--primary btn--sm", href: "#/portfolio" }, "See my portfolio"))));
        ctx.announce("Profile saved. Building your portfolio.");
        window.setTimeout(() => { if (window.location.hash.startsWith("#/profile")) window.location.hash = "#/portfolio"; }, 1600);
      } catch (err) {
        save.disabled = false; save.replaceChildren("Save my profile");
        status.replaceChildren(ctx.alertBox(err, "your profile", null));
      }
    } }, "Save my profile");
    add(ai(h("p", null, "Here's your profile. Check it, then save it."),
      h("dl", { class: "facts" }, items.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
      h("div", { class: "notice" }, save, h("a", { class: "link-btn", href: "#/profile/form" }, "Edit it in the form")), status));
  }

  /** A saved answer in words ([label, value]); null when not answered. */
  function describe(k) {
    const a = answers;
    const words = (list, v) => (list.find((o) => o.value === v) || {}).title || v;
    if (k === "risk_band" && a.risk_band) return ["Risk level", words(ctx.RISK, a.risk_band)];
    if (k === "experience" && a.experience) return ["Experience", words(ctx.EXPERIENCE, a.experience)];
    if (k === "horizon_years" && a.horizon_years) return ["Horizon", ctx.horizonText(a.horizon_years)];
    if (k === "capital" && a.capital) return ["Capital", ctx.money(a.capital)];
    if (k === "loss_capacity" && a.loss_capacity != null) return ["Largest loss you accept", `${Math.round(a.loss_capacity * 100)}%`];
    if (k === "objective" && a.objective) return ["Goal", a.objective];
    if (k === "wants_to_learn" && a.wants_to_learn != null) return ["Answers", a.wants_to_learn ? "Explained so you learn" : "Straight to the point"];
    if (k === "exclusions" && a.exclusions) {
      const names = ctx.LIMITS.filter((l) => a.exclusions.some((e) => e.type === l.exclusion.type && (e.value || null) === (l.exclusion.value || null))).map((l) => l.title);
      return ["Limits", names.length ? names.join(", ") : "None"];
    }
    if (k === "agent_toggles" && a.agent_toggles) {
      const off = ctx.AGENT_ORDER.filter((x) => a.agent_toggles[x] === false).map((x) => ctx.AGENTS[x].name);
      return ["Analyses off", off.length ? off.join(", ") : "None"];
    }
    return null;
  }

  start();
}

/* ---------- The questions: each returns the AI message's content; `answer(text, values)` records the reply ---------- */

const QUESTIONS = {
  risk_band(ctx, a, answer) {
    const { h } = ctx;
    const s = scale(h, { id: "ob-risk", name: "risk_score", label: "Your risk level, from 1 (no loss at all) to 10 (big swings are fine)",
      bands: RISK_BANDS, options: ctx.RISK, value: a.risk_band || null });
    return step(h, "How much up and down can you live with?", s, () => s.value() && answer(`Risk ${s.score()}/10: ${titleOf(ctx.RISK, s.value())}`, { risk_band: s.value() }));
  },
  experience(ctx, a, answer) {
    const { h } = ctx;
    const s = scale(h, { id: "ob-exp", name: "experience_score", label: "Your experience, from 1 (new to investing) to 10 (you follow the markets closely)",
      bands: EXPERIENCE_BANDS, options: ctx.EXPERIENCE, value: a.experience || null });
    return step(h, "How much do you know about investing? It changes how answers are explained, not what is analysed.", s,
      () => s.value() && answer(`Experience ${s.score()}/10: ${titleOf(ctx.EXPERIENCE, s.value())}`, { experience: s.value() }));
  },
  horizon_years(ctx, a, answer) {
    const { h } = ctx;
    const start = (a.horizon_years && a.horizon_years.min) || 10;
    const out = h("output", { class: "scale__out" });
    const input = h("input", { type: "range", class: "scale__input", id: "ob-horizon", min: "1", max: "30", step: "1", value: String(start),
      "aria-describedby": "ob-horizon-out" });
    const show = () => { const n = Number(input.value); input.style.setProperty("--fill", `${((n - 1) / 29) * 100}%`);
      out.id = "ob-horizon-out"; out.replaceChildren(h("strong", null, n >= 30 ? "30 years or more" : `${n} year${n > 1 ? "s" : ""}`)); };
    input.addEventListener("input", show); show();
    return step(h, "When will you need this money?",
      h("div", { class: "scale" }, h("label", { class: "field__label", for: "ob-horizon" }, "Years until you need the money"),
        h("div", { class: "scale__track" }, input), out),
      () => { const n = Number(input.value); answer(n >= 30 ? "30 years or more" : `In about ${n} years`, { horizon_years: { min: n, max: n >= 30 ? null : n } }); },
      { skip: () => answer("I don't know yet", {}) });
  },
  money(ctx, a, answer) {
    const { h } = ctx;
    const capital = h("input", { class: "input", id: "ob-capital", type: "number", inputmode: "numeric", min: "1", step: "1000", placeholder: "e.g. 150000",
      value: a.capital ?? "" });
    const loss = h("input", { type: "range", class: "scale__input", id: "ob-loss", min: "0", max: "50", step: "5",
      value: String(a.loss_capacity != null ? Math.round(a.loss_capacity * 100) : 15) });
    const lossOut = h("output", { class: "scale__out", id: "ob-loss-out" });
    const show = () => { loss.style.setProperty("--fill", `${(Number(loss.value) / 50) * 100}%`); lossOut.replaceChildren(h("strong", null, `${loss.value}% in a bad year`)); };
    loss.addEventListener("input", show); show();
    return step(h, "How much money is this about, and what loss could you accept? Both are simulated: nothing is invested.",
      h("div", { class: "stack-sm" },
        h("div", { class: "field" }, h("label", { class: "field__label", for: "ob-capital" }, "Capital (CAD, optional)"), capital),
        h("div", { class: "scale" }, h("label", { class: "field__label", for: "ob-loss" }, "Largest loss you could accept"),
          h("div", { class: "scale__track" }, loss), lossOut)),
      () => {
        const c = Number(capital.value);
        const values = { loss_capacity: Number(loss.value) / 100 };
        if (capital.value && c > 0) values.capital = c;
        answer(`${values.capital ? `${ctx.money(values.capital)}, ` : ""}at most ${loss.value}% down`, values);
      },
      { skip: () => answer("I'd rather not say", {}) });
  },
  objective(ctx, a, answer) {
    const { h } = ctx;
    const text = h("input", { class: "input", id: "ob-goal", type: "text", maxlength: "200", placeholder: "e.g. buy a house in ten years", value: a.objective || "" });
    return step(h, "What is this money for? One sentence is enough.",
      h("div", { class: "field" }, h("label", { class: "field__label", for: "ob-goal" }, "Your goal"), text),
      () => (text.value.trim() ? answer(text.value.trim(), { objective: text.value.trim() }) : text.focus()),
      { skip: () => answer("No particular goal", {}) });
  },
  wants_to_learn(ctx, a, answer) {
    const { h } = ctx;
    return h("div", { class: "stack-sm" },
      h("p", null, "Should I explain the terms and the reasoning as we go, so you learn?"),
      h("div", { class: "onboarding__pick" },
        h("button", { class: "btn btn--secondary", type: "button", onclick: () => answer("Yes, explain things so I learn", { wants_to_learn: true }) }, "Yes, teach me as we go"),
        h("button", { class: "btn btn--secondary", type: "button", onclick: () => answer("No, just the answers", { wants_to_learn: false }) }, "No, just the answers")));
  },
  exclusions(ctx, a, answer) {
    const { h } = ctx;
    const boxes = ctx.LIMITS.map((l) => h("label", { class: "choice" },
      h("input", { type: "checkbox", name: `ob-limit-${l.key}`, checked: (a.exclusions || []).some((e) => e.type === l.exclusion.type) }),
      h("span", { class: "choice__title" }, l.title), h("span", { class: "choice__desc" }, l.desc)));
    return step(h, "Is there anything AlphaForge must never include? These are hard limits, checked on every answer.",
      h("div", { class: "choices" }, boxes),
      () => {
        const picked = ctx.LIMITS.filter((l, i) => boxes[i].querySelector("input").checked);
        answer(picked.length ? picked.map((l) => l.title).join(", ") : "No hard limits", { exclusions: picked.map((l) => l.exclusion) });
      });
  },
  agent_toggles(ctx, a, answer) {
    const { h } = ctx;
    const rows = ctx.AGENT_ORDER.map((x) => h("div", { class: "switch-row" },
      h("div", { class: "switch-row__text" }, h("label", { for: `ob-toggle-${x}`, class: "choice__title" }, ctx.AGENTS[x].name),
        h("span", { class: "small muted" }, ctx.AGENTS[x].desc)),
      h("input", { class: "switch", type: "checkbox", role: "switch", id: `ob-toggle-${x}`,
        checked: !(a.agent_toggles && a.agent_toggles[x] === false) })));
    return step(h, "Last one: which analyses should AlphaForge run? Turn off what you don't want. If a question can't be answered without one, it still runs and the answer says why.",
      h("div", { class: "panel panel--plain onboarding__switches" }, rows),
      () => {
        const toggles = Object.fromEntries(ctx.AGENT_ORDER.map((x, i) => [x, rows[i].querySelector("input").checked]));
        const off = ctx.AGENT_ORDER.filter((x) => !toggles[x]).map((x) => ctx.AGENTS[x].name);
        answer(off.length ? `All on except ${off.join(", ")}` : "All analyses on", { agent_toggles: toggles });
      });
  },
};

function titleOf(list, v) { return (list.find((o) => o.value === v) || {}).title || v; }

/** A question with its control, a Continue button and, when allowed, a Skip. */
function step(h, question, control, onContinue, { skip } = {}) {
  const hint = h("p", { class: "field__error", role: "alert" });
  return h("form", { class: "stack-sm onboarding__step", novalidate: true, onsubmit: (e) => {
    e.preventDefault();
    hint.replaceChildren();
    if (onContinue() === null) hint.textContent = "Move the slider to choose first.";
  } },
    h("p", null, question), control, hint,
    h("div", { class: "notice" }, h("button", { class: "btn btn--primary btn--sm", type: "submit" }, "Continue"),
      skip ? h("button", { class: "link-btn", type: "button", onclick: skip }, "Skip") : null));
}
