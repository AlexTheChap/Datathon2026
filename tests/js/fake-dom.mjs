// A tiny DOM, just enough to run frontend/assets/profile-steps.js under `node --test` (no jsdom, no browser).
// It supports: elements with attributes / properties, children, events that bubble, focus, classList, closest(".class").
export class Text {
  constructor(data) { this.data = String(data); this.parentNode = null; }
  get textContent() { return this.data; }
}

export class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this._value = "";
    this.checked = false;
    if (["input", "textarea", "select"].includes(tag)) {   // like a real form control: the value is always a string
      Object.defineProperty(this, "value", { get: () => this._value, set: (v) => { this._value = String(v ?? ""); } });
    }
    const self = this;
    this.classList = {
      add: (c) => self.#setClasses([...self.#classes(), c]),
      remove: (c) => self.#setClasses(self.#classes().filter((x) => x !== c)),
      toggle: (c, force) => {
        const has = self.#classes().includes(c);
        const on = force === undefined ? !has : Boolean(force);
        if (on && !has) self.classList.add(c);
        if (!on && has) self.classList.remove(c);
        return on;
      },
      contains: (c) => self.#classes().includes(c),
    };
  }
  #classes() { return (this.attrs.get("class") || "").split(/\s+/).filter(Boolean); }
  #setClasses(list) { this.attrs.set("class", [...new Set(list)].join(" ")); }
  get className() { return this.attrs.get("class") || ""; }
  set className(v) { this.attrs.set("class", v); }
  get children() { return this.childNodes.filter((n) => n instanceof El); }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.replaceChildren(String(v)); }
  get disabled() { return this.attrs.has("disabled"); }
  set disabled(v) { if (v) this.attrs.set("disabled", ""); else this.attrs.delete("disabled"); }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  removeAttribute(k) { this.attrs.delete(k); }
  hasAttribute(k) { return this.attrs.has(k); }
  append(...nodes) {
    for (const n of nodes) {
      const node = typeof n === "string" ? new Text(n) : n;
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const c of this.childNodes) c.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatchEvent(type) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (let el = this; el; el = el.parentNode) for (const fn of el.listeners[type] || []) fn(event);
    return event;
  }
  click() { if (!this.disabled) this.dispatchEvent("click"); }
  focus() { if (!this.disabled) globalThis.document.activeElement = this; }
  closest(selector) {
    const cls = selector.replace(/^\./, "");
    for (let el = this; el instanceof El; el = el.parentNode) if (el.classList.contains(cls)) return el;
    return null;
  }
  /** Every descendant element, depth first. */
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  /** The first descendant matching a predicate. */
  find(fn) { return this.all().find(fn) || null; }
}

globalThis.document = { activeElement: null };

/** The same contract as the app's `h(tag, props, ...children)`. */
export function h(tag, props, ...children) {
  const el = new El(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = Boolean(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof El ? c : String(c));
  }
  return el;
}
