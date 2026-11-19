// Ekhtem AlQur'an: in-app bug reports (owner, 19 Nov 2026, decision 265 in the app repository's docs/OWNER_DECISIONS_3.0.5.md).
//
// POST https://audio.ekhtemalquran.stream/report with JSON
//   {"subject": "...", "message": "...", "email": "optional", "app": {"version", "build", "platform", "system", "device", "language"}}
// is checked (sizes, the email's form, a few reports a minute from one address) and emailed through Cloudflare Email
// Routing's `send_email` binding to the one verified destination address in wrangler.toml, the user's email (when given)
// as Reply-To. Answers: 200 {"ok":true}; 400 invalid; 405 not a POST; 413 too large; 415 not JSON; 429 too many; 502 not
// sent. Nothing is stored: no database, no log of the text; the IP address is used only as the rate limit's key.
// Deployed by .github/workflows/deploy-report-worker.yml; the rules below are tested by test.mjs before each deploy.

export const LIMITS = { subject: 120, message: 4000, email: 254, field: 80, body: 16 * 1024 };
const EMAIL_FORM = /^[^\s@<>()",;:]+@[^\s@<>()",;:]+\.[^\s@<>()",;:]+$/;
const PLATFORMS = new Set(["android", "ios"]);
const LANGUAGES = new Set(["ar", "en"]);

// Characters that must never reach a header line or the plain-text body as controls (CR/LF are kept in the message only).
const CONTROLS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

function text(value) {
  return typeof value === "string" ? value.replace(CONTROLS, "").trim() : "";
}

function oneLine(value) {
  return text(value).replace(/[\r\n\u2028\u2029]+/g, " ");
}

/** The report from the parsed JSON, or {error} when it breaks a rule. Lengths count characters as the apps do (UTF-16 units). */
export function validate(body) {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { error: "invalid" };
  const subject = oneLine(body.subject);
  const message = text(body.message).replace(/\r\n?/g, "\n");
  const email = oneLine(body.email);
  if (!subject || subject.length > LIMITS.subject) return { error: "invalid" };
  if (!message || message.length > LIMITS.message) return { error: "invalid" };
  if (email && (email.length > LIMITS.email || !EMAIL_FORM.test(email))) return { error: "invalid" };
  const raw = body.app && typeof body.app === "object" && !Array.isArray(body.app) ? body.app : {};
  const app = {};
  for (const key of ["version", "build", "platform", "system", "device", "language"]) {
    app[key] = oneLine(raw[key]).slice(0, LIMITS.field);
  }
  if (!PLATFORMS.has(app.platform)) return { error: "invalid" };
  if (app.language && !LANGUAGES.has(app.language)) app.language = "";
  return { report: { subject, message, email: email || null, app } };
}

/** The email for a valid report: to the one verified address, from the zone's address, the user's email as Reply-To. */
export function buildEmail(report, { to, from }) {
  const a = report.app;
  const platform = a.platform === "ios" ? "iOS app" : "Android app";
  const lines = [
    `Subject: ${report.subject}`,
    `Reply to: ${report.email ?? "(no email given)"}`,
    `App: Ekhtem AlQur'an ${a.version || "?"} (${a.build || "?"}), ${platform}`,
    `System: ${a.system || "?"}`,
    `Device: ${a.device || "?"}`,
    `App language: ${a.language || "?"}`,
    "",
    "Message:",
    report.message,
    "",
  ];
  const mail = {
    to,
    from: { email: from, name: "Ekhtem AlQur'an bug report" },
    subject: `[Ekhtem bug] ${report.subject}`,
    text: lines.join("\n"),
  };
  if (report.email) mail.replyTo = report.email;
  return mail;
}

function answer(status, ok, error) {
  const body = ok ? { ok: true } : { ok: false, error };
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return answer(405, false, "method");
    if (!(request.headers.get("content-type") || "").toLowerCase().startsWith("application/json")) return answer(415, false, "type");
    const declared = Number(request.headers.get("content-length") || "0");
    if (declared > LIMITS.body) return answer(413, false, "size");
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    if (env.REPORTS) {
      const { success } = await env.REPORTS.limit({ key: ip });
      if (!success) return answer(429, false, "rate");
    }
    const raw = await request.text();
    if (raw.length > LIMITS.body) return answer(413, false, "size");
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return answer(400, false, "invalid");
    }
    const { report, error } = validate(parsed);
    if (error) return answer(400, false, error);
    try {
      await env.MAIL.send(buildEmail(report, { to: env.REPORT_TO, from: env.REPORT_FROM }));
    } catch (e) {
      // The code only (E_...), never the report's text.
      console.error("send failed", e && e.code ? e.code : String(e && e.name));
      return answer(502, false, "send");
    }
    return answer(200, true);
  },
};
