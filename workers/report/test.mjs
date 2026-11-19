// The bug-report Worker's rules (decision 265), run by the deploy workflow before each deploy: node --test test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { validate, buildEmail, LIMITS } from "./src/index.js";

const app = { version: "4.7.15", build: "15", platform: "android", system: "Android 16", device: "Google Pixel 9", language: "ar" };

test("a valid report is trimmed and kept", () => {
  const { report, error } = validate({ subject: "  الصفحة ٥  ", message: " نص\r\nسطر ثان ", email: " a@b.co ", app });
  assert.equal(error, undefined);
  assert.equal(report.subject, "الصفحة ٥");
  assert.equal(report.message, "نص\nسطر ثان");
  assert.equal(report.email, "a@b.co");
  assert.equal(report.app.platform, "android");
});

test("the email is optional", () => {
  assert.equal(validate({ subject: "s", message: "m", app }).report.email, null);
  assert.equal(validate({ subject: "s", message: "m", email: "  ", app }).report.email, null);
});

test("required fields, limits and the email's form", () => {
  assert.equal(validate({ subject: "", message: "m", app }).error, "invalid");
  assert.equal(validate({ subject: "s", message: "   ", app }).error, "invalid");
  assert.equal(validate({ subject: "x".repeat(LIMITS.subject + 1), message: "m", app }).error, "invalid");
  assert.equal(validate({ subject: "x".repeat(LIMITS.subject), message: "m", app }).error, undefined);
  assert.equal(validate({ subject: "s", message: "x".repeat(LIMITS.message + 1), app }).error, "invalid");
  assert.equal(validate({ subject: "s", message: "x".repeat(LIMITS.message), app }).error, undefined);
  for (const bad of ["a", "a@b", "a b@c.d", "a@b.c\nBcc: x@y.z", "<a@b.c>"]) {
    assert.equal(validate({ subject: "s", message: "m", email: bad, app }).error, "invalid", bad);
  }
  assert.equal(validate({ subject: "s", message: "m", app: { ...app, platform: "web" } }).error, "invalid");
  assert.equal(validate(null).error, "invalid");
  assert.equal(validate([]).error, "invalid");
});

test("no line breaks or controls reach a header", () => {
  const { report } = validate({ subject: "a\r\nBcc: x@y.z\u0007", message: "m", app });
  assert.equal(report.subject, "a Bcc: x@y.z");
  const mail = buildEmail(report, { to: "t@x.y", from: "f@x.y" });
  assert.ok(!mail.subject.includes("\n"));
});

test("the email's shape", () => {
  const { report } = validate({ subject: "Crash", message: "Steps", email: "u@e.co", app: { ...app, platform: "ios" } });
  const mail = buildEmail(report, { to: "t@x.y", from: "report@x.y" });
  assert.equal(mail.to, "t@x.y");
  assert.deepEqual(mail.from, { email: "report@x.y", name: "Ekhtem AlQur'an bug report" });
  assert.equal(mail.subject, "[Ekhtem bug] Crash");
  assert.equal(mail.replyTo, "u@e.co");
  assert.match(mail.text, /iOS app/);
  assert.match(mail.text, /Steps/);
  const noEmail = buildEmail(validate({ subject: "s", message: "m", app }).report, { to: "t", from: "f" });
  assert.equal(noEmail.replyTo, undefined);
});

function request(body, { method = "POST", type = "application/json" } = {}) {
  return new Request("https://audio.ekhtemalquran.stream/report", {
    method,
    headers: { "content-type": type, "cf-connecting-ip": "203.0.113.9" },
    body: method === "POST" ? body : undefined,
  });
}

function env(send, allowed = true) {
  return { REPORT_TO: "t@x.y", REPORT_FROM: "f@x.y", MAIL: { send }, REPORTS: { limit: async () => ({ success: allowed }) } };
}

test("the Worker's answers", async () => {
  const sent = [];
  const ok = env(async (m) => { sent.push(m); return { messageId: "1" }; });
  const good = JSON.stringify({ subject: "s", message: "m", app });
  let r = await worker.fetch(request(good), ok);
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true });
  assert.equal(sent.length, 1);
  assert.equal((await worker.fetch(request(good, { method: "GET" }), ok)).status, 405);
  assert.equal((await worker.fetch(request(good, { type: "text/plain" }), ok)).status, 415);
  assert.equal((await worker.fetch(request("{"), ok)).status, 400);
  assert.equal((await worker.fetch(request(JSON.stringify({ subject: "s", message: "x".repeat(20000), app })), ok)).status, 413);
  assert.equal((await worker.fetch(request(good), env(async () => ({}), false))).status, 429);
  r = await worker.fetch(request(good), env(async () => { const e = new Error("no"); e.code = "E_DELIVERY_FAILED"; throw e; }));
  assert.equal(r.status, 502);
  assert.deepEqual(await r.json(), { ok: false, error: "send" });
});
