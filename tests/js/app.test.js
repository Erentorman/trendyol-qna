// Run: node --test tests/js/app.test.js   (no npm dependencies; uses a tiny fake DOM)
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const ui = require(path.join(__dirname, "../../app/static/app.js"));

class FakeClassList {
  constructor(el) { this.el = el; }
  get set() { return new Set(this.el.className.split(/\s+/).filter(Boolean)); }
  add(c) { const s = this.set; s.add(c); this.el.className = [...s].join(" "); }
  remove(c) { const s = this.set; s.delete(c); this.el.className = [...s].join(" "); }
  toggle(c, on) { (on === undefined ? !this.contains(c) : on) ? this.add(c) : this.remove(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(className = "") {
    this.className = className; this.textContent = ""; this.value = "";
    this.disabled = false; this.readOnly = false; this.dataset = {};
    this.classList = new FakeClassList(this); this.handlers = {}; this.children = {};
  }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  querySelector(sel) { return this.children[sel] || null; }
  fire(type) { return this.handlers[type]({ preventDefault() {} }); }
}

function makeCard(id = 7, status = "NEW") {
  const card = new El("card");
  card.dataset = { questionId: String(id), status };
  const parts = {
    ".answer-form": new El("answer-form"), "textarea": new El(),
    '[data-role="submit"]': new El(), '[data-role="counter"]': new El("counter"),
    '[data-role="message"]': new El("message"), ".status": new El("status"),
  };
  card.children = parts;
  return { card, form: parts[".answer-form"], textarea: parts["textarea"],
           button: parts['[data-role="submit"]'], counter: parts['[data-role="counter"]'],
           message: parts['[data-role="message"]'], status: parts[".status"] };
}

function jsonResponse(body) { return { json: async () => body }; }

function deferredFetch() {
  const calls = [];
  let resolve;
  const fetchImpl = (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return new Promise((r) => { resolve = r; });
  };
  return { fetchImpl, calls, respond: (body) => resolve(jsonResponse(body)) };
}

function setup(fetchImpl, extra = {}) {
  const c = makeCard();
  ui.setupCard(c.card, { fetchImpl, min: 10, max: 2000, ...extra });
  return c;
}

function type(c, text) { c.textarea.value = text; c.textarea.fire("input"); }

const VALID = "Evet, ürün %100 pamukludur.";

test("charCount strips and counts code points like Python len()", () => {
  assert.equal(ui.charCount("  abc  "), 3);
  assert.equal(ui.charCount("😀😀"), 2); // UTF-16 length would be 4
  assert.equal(ui.charCount(""), 0);
});

test("validateAnswer enforces empty / min / max", () => {
  assert.equal(ui.validateAnswer("   ", 10, 2000).code, "EMPTY_ANSWER");
  assert.equal(ui.validateAnswer("  kısa  ", 10, 2000).code, "ANSWER_TOO_SHORT");
  assert.equal(ui.validateAnswer("x".repeat(2001), 10, 2000).code, "ANSWER_TOO_LONG");
  assert.equal(ui.validateAnswer("x".repeat(10), 10, 2000).ok, true);
  assert.equal(ui.validateAnswer("x".repeat(2000), 10, 2000).ok, true);
});

test("live counter updates and flags invalid length; button follows validity", () => {
  const c = setup(async () => {});
  assert.equal(c.counter.textContent, "0 / 2000");
  assert.equal(c.button.disabled, true);

  type(c, "kısa");
  assert.equal(c.counter.textContent, "4 / 2000");
  assert.ok(c.counter.classList.contains("is-invalid"));
  assert.equal(c.button.disabled, true);

  type(c, VALID);
  assert.ok(!c.counter.classList.contains("is-invalid"));
  assert.equal(c.button.disabled, false);

  type(c, "x".repeat(2001));
  assert.equal(c.counter.textContent, "2001 / 2000");
  assert.equal(c.button.disabled, true);
});

test("too short answer is blocked before any request", async () => {
  let called = false;
  const c = setup(async () => { called = true; });
  type(c, "kısa");
  await c.form.fire("submit");
  assert.equal(called, false);
  assert.equal(c.message.textContent, ui.MESSAGES.ANSWER_TOO_SHORT);
  assert.ok(c.message.classList.contains("is-error"));
});

test("submit disables button, shows loading, success updates the card", async () => {
  const f = deferredFetch();
  let answered = 0;
  const c = setup(f.fetchImpl, { onAnswered: () => { answered += 1; } });
  type(c, "  " + VALID + "  ");

  const pending = c.form.fire("submit");
  assert.equal(c.button.disabled, true);
  assert.equal(c.textarea.readOnly, true);
  assert.ok(c.button.classList.contains("is-loading"));
  assert.equal(c.message.textContent, ui.MESSAGES.SENDING);
  assert.deepEqual(f.calls[0], { url: "/api/answer", body: { question_id: 7, answer: VALID } });

  f.respond({ ok: true, data: { question_id: 7, local_status: "ANSWERED", answer_id: 1 } });
  await pending;
  assert.equal(c.status.textContent, "ANSWERED");
  assert.equal(c.card.dataset.status, "ANSWERED");
  assert.ok(c.card.classList.contains("is-done"));
  assert.equal(c.button.disabled, true);
  assert.equal(c.message.textContent, ui.MESSAGES.SUCCESS);
  assert.equal(answered, 1);
});

test("double click sends only one request", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  const first = c.form.fire("submit");
  const second = c.form.fire("submit");
  const third = c.form.fire("submit");
  f.respond({ ok: true, data: {} });
  await Promise.all([first, second, third]);
  assert.equal(f.calls.length, 1);
  // Also after success no further submit is possible.
  await c.form.fire("submit");
  assert.equal(f.calls.length, 1);
});

test("backend error code is shown with our message, never raw bodies", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  const p = c.form.fire("submit");
  f.respond({ ok: false, error: { code: "FORBIDDEN_WORD", message: "Answer contains forbidden words." } });
  await p;
  assert.equal(c.message.textContent, ui.MESSAGES.FORBIDDEN_WORD);
  assert.ok(c.message.classList.contains("is-error"));
  // Question is FAILED on the backend now: card stops accepting submits.
  assert.equal(c.button.disabled, true);
  assert.ok(c.card.classList.contains("is-done"));
});

test("ALREADY_LOCKED and QUESTION_ALREADY_ANSWERED get clear messages", () => {
  for (const code of ["ALREADY_LOCKED", "QUESTION_ALREADY_ANSWERED", "RATE_LIMITED",
                      "SERVER_ERROR", "NETWORK_ERROR", "ANSWER_TOO_LONG"]) {
    assert.equal(ui.messageFor({ code, message: "x" }), ui.MESSAGES[code]);
  }
  // Unknown code: fall back to the backend's normalized message.
  assert.equal(ui.messageFor({ code: "SOMETHING_NEW", message: "Normalized text." }), "Normalized text.");
  assert.equal(ui.messageFor(null), ui.MESSAGES.UNKNOWN);
});

test("editable (pre-lock) error re-enables the form", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  const p = c.form.fire("submit");
  f.respond({ ok: false, error: { code: "TRENDYOL_NOT_CONFIGURED", message: "m" } });
  await p;
  assert.equal(c.button.disabled, false);
  assert.equal(c.textarea.readOnly, false);
  assert.ok(!c.card.classList.contains("is-done"));
});

test("non-JSON response and network failure are normalized", async () => {
  const html = await ui.postAnswer(async () => ({ json: async () => { throw new SyntaxError("<html>"); } }), 1, VALID);
  assert.deepEqual(html, { ok: false, error: { code: "BAD_RESPONSE" } });

  const down = await ui.postAnswer(async () => { throw new TypeError("Failed to fetch"); }, 1, VALID);
  assert.deepEqual(down, { ok: false, error: { code: "CLIENT_NETWORK" } });
});

test("retryable backend error keeps the card open for another try", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  let p = c.form.fire("submit");
  f.respond({ ok: false, error: { code: "SERVER_ERROR", message: "m", retryable: true } });
  await p;
  assert.equal(c.message.textContent, ui.MESSAGES.SERVER_ERROR);
  assert.equal(c.button.disabled, false);
  assert.equal(c.textarea.readOnly, false);
  assert.ok(!c.card.classList.contains("is-done"));

  p = c.form.fire("submit");                     // second attempt goes out
  f.respond({ ok: true, data: {} });
  await p;
  assert.equal(f.calls.length, 2);
  assert.equal(c.status.textContent, "ANSWERED");
});

test("non-retryable backend error closes the card", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  const p = c.form.fire("submit");
  f.respond({ ok: false, error: { code: "ALREADY_LOCKED", message: "m", retryable: false } });
  await p;
  assert.equal(c.button.disabled, true);
  assert.ok(c.card.classList.contains("is-done"));
});

test("empty or whitespace-only answer is blocked before any request", async () => {
  let called = false;
  const c = setup(async () => { called = true; });
  type(c, "   \n  ");
  assert.equal(c.button.disabled, true);
  await c.form.fire("submit");
  assert.equal(called, false);
  assert.equal(c.message.textContent, ui.MESSAGES.EMPTY_ANSWER);
});

test("infrastructure block (Cloudflare/IP) gets a clear message and keeps the card open", async () => {
  const f = deferredFetch();
  const c = setup(f.fetchImpl);
  type(c, VALID);
  const p = c.form.fire("submit");
  f.respond({ ok: false, error: { code: "ACCESS_BLOCKED", message: "x", retryable: true } });
  await p;
  assert.equal(c.message.textContent, ui.MESSAGES.ACCESS_BLOCKED);
  assert.match(c.message.textContent, /Cloudflare/);
  assert.ok(c.message.classList.contains("is-error"));
  assert.equal(c.button.disabled, false);           // can resend once access is back
  assert.notEqual(c.status.textContent, "ANSWERED"); // never looks answered
  assert.ok(!c.card.classList.contains("is-done"));
});

test("failed request never marks the card ANSWERED", async () => {
  for (const error of [{ code: "AUTH_ERROR" }, { code: "RATE_LIMITED", retryable: true },
                       { code: "SERVER_ERROR", retryable: true }, { code: "NETWORK_ERROR", retryable: true }]) {
    const f = deferredFetch();
    const c = setup(f.fetchImpl);
    type(c, VALID);
    const p = c.form.fire("submit");
    f.respond({ ok: false, error });
    await p;
    assert.notEqual(c.status.textContent, "ANSWERED");
    assert.notEqual(c.card.dataset.status, "ANSWERED");
    assert.ok(c.message.classList.contains("is-error"));
  }
});

test("HTML body from a proxy is never shown", async () => {
  const f = async () => ({ json: async () => { throw new SyntaxError("Unexpected token <"); } });
  const c = setup(f);
  type(c, VALID);
  await c.form.fire("submit");
  assert.equal(c.message.textContent, ui.MESSAGES.BAD_RESPONSE);
  assert.ok(!c.message.textContent.includes("<"));
});
