// Runs the real app logic with an in-memory DOM/API double. This is not browser QA.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "mailhub/static/index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "mailhub/static/app.js"), "utf8").replace("void boot();", "globalThis.testBoot = boot;");

class Element {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.value = ""; this.type = ""; this.disabled = false;
    this.hidden = false; this.open = false; this.isConnected = true; this.textContent = "";
    this.attributes = {}; this.dataset = {}; this.listeners = {};
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  add(child) { if (!this.children.length) this.value = child.value; this.children.push(child); }
  addEventListener(name, action) { this.listeners[name] = action; }
  querySelector() { return new Element("button"); }
  querySelectorAll() { return []; }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  reset() {}
  select() {}
}
const elements = Object.fromEntries([...html.matchAll(/id="([\w-]+)"/g)].map((match) => [match[1], new Element()]));
elements["ai-client"].value = "codex";
elements["ai-auth"].value = "oauth";
const document = {
  hidden: false, title: "", activeElement: new Element("button"),
  getElementById: (id) => elements[id],
  createElement: (tag) => new Element(tag),
  querySelector: () => new Element("button"),
  querySelectorAll: (selector) => selector === "dialog[open]" ? Object.values(elements).filter((item) => item.open) : [],
  addEventListener() {},
};
let authenticated = true;
let messagesForbidden = false;
let calls = [];
let assigned = [];
let deferredToken = null;
let grantRows = [{ id: "grant-1", client_name: "Test AI", created_at: "2026-10-29T08:00:00Z", last_used_at: null }];
const settings = { public_url: "https://mail.example.test", sync_interval_seconds: 300, sync_days: 30, sync_limit: 200,
  google: { client_id: "google-test-id", secret_configured: true, redirect_uri: "https://mail.example.test/api/oauth/gmail/callback" },
  microsoft: { client_id: "", secret_configured: false, redirect_uri: "https://mail.example.test/api/oauth/microsoft/callback" },
  storage: { encrypted: true }, restart_required: false };
const account = { id: "a1", provider: "icloud", email: "example@example.test", label: "測試郵箱", status: "ready", last_sync: "2026-10-29T08:00:00Z", message_count: 1, folder: "INBOX" };
const message = { id: "m1", account_id: "a1", account_email: account.email, provider: "icloud", subject: "<script>bad()</script>", sender: "Test", recipients: "User", received_at: "2026-10-29T08:00:00Z", snippet: "test", body: "<img src=x onerror=bad()>", body_truncated: true, attachments: [] };
const context = vm.createContext({
  document, window: { addEventListener() {} }, console, URL, URLSearchParams, Intl, AbortController,
  location: { hash: "#inbox", search: "", pathname: "/", origin: "https://mail.example.test", assign(url) { assigned.push(url); } },
  history: { replaceState() {} }, navigator: { clipboard: { async writeText() {} } },
  Option: class extends Element { constructor(text, value) { super("option"); this.textContent = text; this.value = value; } },
  setTimeout, clearTimeout, setInterval() {},
  fetch: async (url, options) => {
    calls.push({ url, options });
    const pathname = url.split("?")[0];
    if (!authenticated && pathname !== "/api/session" && pathname !== "/api/login") return { ok: false, status: 401, json: async () => ({ detail: "Session expired" }) };
    if (messagesForbidden && pathname === "/api/messages") return { ok: false, status: 403, json: async () => ({ detail: "拒絕存取" }) };
    if (pathname === "/api/tokens" && options.method === "POST" && deferredToken) return deferredToken;
    if (pathname === "/api/settings" && options.method === "POST") {
      const body = JSON.parse(options.body);
      settings.sync_interval_seconds = body.sync_interval_seconds; settings.sync_days = body.sync_days; settings.sync_limit = body.sync_limit;
      for (const provider of ["google", "microsoft"]) {
        settings[provider].client_id = body[provider + "_client_id"];
        if (body["clear_" + provider + "_secret"]) settings[provider].secret_configured = false;
        else if (body[provider + "_client_secret"]) settings[provider].secret_configured = true;
      }
    }
    if (pathname.startsWith("/api/oauth/grants/") && options.method === "DELETE") grantRows = [];
    if (pathname === "/api/oauth/consent" && options.method === "POST") return { ok: true, status: 200, json: async () => ({ redirect_url: JSON.parse(options.body).approve ? "http://127.0.0.1:7777/callback?code=test-code" : "http://127.0.0.1:7777/callback?error=access_denied" }) };
    const responses = {
      "/api/session": { authenticated, csrf_token: "csrf-test", public_url: "https://mail.example.test", sync_interval_seconds: 300, sync_days: 30, sync_limit: 200 },
      "/api/providers": { providers: [{ id: "icloud", name: "iCloud", kind: "imap", host: "imap.mail.me.com", port: 993, note: "test", configured: true }] },
      "/api/accounts": { accounts: [account] },
      "/api/messages": { messages: [message], total: 1, coverage: { days: 30, per_account_limit: 200 } },
      "/api/messages/m1": { message },
      "/api/tokens": { tokens: [] },
      "/api/settings": settings,
      "/api/setup": { steps: [{ id: "service", title: "服務設定", complete: true, detail: "設定已載入，外部連線未驗證。" }, { id: "ai", title: "AI 讀取", complete: false, detail: "尚未取得客戶端讀取記錄。" }], ready: false, counts: { accounts: 1, tokens: 0 }, ai: { mcp_url: "https://mail.example.test/mcp", oauth_supported: true } },
      "/api/diagnostics": { checks: [{ id: "database", name: "資料庫", status: "pass", detail: "僅本機檢查" }], checked_at: "2026-10-29T08:00:00Z" },
      "/api/oauth/grants": { grants: grantRows },
      "/api/oauth/consent": { request_id: "request-1", client_name: "<script>Untrusted AI</script>", redirect_uri: "http://127.0.0.1:7777/callback", scope: "mail.read", resource: "https://mail.example.test/mcp", expires_at: "2999-10-29T08:00:00Z" },
    };
    return { ok: true, status: 200, json: async () => responses[pathname] || { ok: true } };
  },
});
function flatten(element) { return String(element.textContent || "") + element.children.map(flatten).join(" "); }

(async () => {
  vm.runInContext(source, context);
  await context.testBoot();
  await vm.runInContext("loadMessages()", context);
  assert.equal(elements["app-view"].hidden, false);
  assert.equal(elements["login-view"].hidden, true);
  assert.doesNotMatch(elements["codex-config"].textContent, /bearer_token/);
  elements["ai-auth"].value = "token";
  vm.runInContext("updateConnectionConfig()", context);
  assert.match(elements["codex-config"].textContent, /MAILHUB_MCP_TOKEN/);
  assert.equal(elements["account-host"].readOnly, true);
  assert.match(elements["coverage"].textContent, /^同步目標：/);
  assert.match(flatten(elements["accounts-list"]), /天數未記錄/);
  assert.match(flatten(elements["mail-list"]), /<script>bad\(\)<\/script>/);
  assert.equal(elements["mail-list"].children[0].tagName, "button");
  await vm.runInContext('openMessage("m1")', context);
  assert.match(flatten(elements["reader-content"]), /此郵件僅同步部分內容/);
  assert.match(flatten(elements["reader-content"]), /<img src=x onerror=bad\(\)>/);
  assert.match(flatten(elements["reader-content"]), /天數未記錄/);
  message.sync = { last_sync: "2026-10-29T08:00:00Z", days: 7, per_account_limit: 50 };
  await vm.runInContext('openMessage("m1")', context);
  assert.match(flatten(elements["reader-content"]), /近 7 天 · 最多 50 封/);
  assert.match(elements["coverage"].textContent, /近 30 天/, "Actual message snapshot must not alter the target");
  await vm.runInContext('api("/api/logout", {method: "POST"})', context);
  assert.equal(calls.at(-1).options.headers["X-CSRF-Token"], "csrf-test");
  messagesForbidden = true;
  await vm.runInContext("loadMessages()", context);
  assert.match(flatten(elements["mail-list"]), /拒絕存取/);
  assert.equal(elements["login-view"].hidden, true, "403 must not masquerade as session expiry");
  await vm.runInContext("loadSettings()", context);
  assert.equal(elements["google-client-secret"].value, "");
  assert.equal(elements["google-configured"].textContent, "已儲存 Secret");
  assert.equal(elements["google-redirect"].value, settings.google.redirect_uri);
  const blankSettings = vm.runInContext("collectSettings()", context);
  assert.equal(Object.hasOwn(blankSettings, "google_client_secret"), false, "Blank secret must be omitted");
  elements["clear-google-secret"].checked = true;
  const cleared = vm.runInContext("collectSettings()", context);
  assert.equal(cleared.clear_google_secret, true);
  assert.equal(Object.hasOwn(cleared, "google_client_secret"), false);
  elements["clear-google-secret"].checked = false;
  elements["google-client-secret"].value = "never-export-this-secret";
  elements["sync-days"].value = "90";
  await vm.runInContext("saveSettings({preventDefault(){}})", context);
  const saved = calls.findLast((call) => call.url === "/api/settings" && call.options.method === "POST");
  assert.equal(saved.options.headers["X-CSRF-Token"], "csrf-test");
  assert.equal(JSON.parse(saved.options.body).sync_days, 90);
  assert.equal(elements["google-client-secret"].value, "", "Secret clears after successful save");
  assert.equal(vm.runInContext("state.settingsDirty", context), false);
  for (const client of ["codex", "cursor", "claude", "rest", "oauth"]) {
    elements["ai-client"].value = client;
    vm.runInContext("updateConnectionConfig()", context);
    assert.doesNotMatch(elements["codex-config"].textContent, /never-export-this-secret/);
    assert.match(elements["codex-config"].textContent, /mail\.example\.test/);
  }
  await vm.runInContext("runDiagnostics()", context);
  assert.match(flatten(elements["diagnostics-results"]), /僅本機檢查/);
  await vm.runInContext("loadGrants()", context);
  const revoke = elements["grants-list"].children[0].children[1];
  const deletesBefore = calls.filter((call) => call.options.method === "DELETE").length;
  revoke.listeners.click();
  assert.equal(calls.filter((call) => call.options.method === "DELETE").length, deletesBefore, "Revoke requires confirmation");
  await elements["confirm-action"].listeners.click();
  assert.match(calls.find((call) => call.options.method === "DELETE").url, /\/api\/oauth\/grants\/grant-1/);
  vm.runInContext('state.oauthRequest = "request-1"', context);
  const consentPostsBefore = calls.filter((call) => call.url === "/api/oauth/consent" && call.options.method === "POST").length;
  await vm.runInContext("loadConsent()", context);
  assert.match(flatten(elements["consent-details"]), /<script>Untrusted AI<\/script>/);
  assert.equal(calls.filter((call) => call.url === "/api/oauth/consent" && call.options.method === "POST").length, consentPostsBefore, "Loading consent must never approve it");
  assert.equal(vm.runInContext('safeOAuthRedirect("http://evil.test/callback")', context), null);
  assert.equal(vm.runInContext('safeOAuthRedirect("javascript:alert(1)")', context), null);
  await vm.runInContext("resolveConsent(false)", context);
  assert.match(assigned.at(-1), /error=access_denied/);
  assert.equal(vm.runInContext("state.oauthRequest", context), "");
  // An active save must finish before logout can discard a draft or end the session.
  const logoutPostsBefore = calls.filter((call) => call.url === "/api/logout" && call.options.method === "POST").length;
  for (const dirty of [false, true]) {
    vm.runInContext(`state.settingsBusy = true; state.settingsDirty = ${dirty};`, context);
    elements["logout"].listeners.click();
    assert.equal(elements["confirm-dialog"].open, false, "Saving must block logout confirmation");
    assert.equal(calls.filter((call) => call.url === "/api/logout" && call.options.method === "POST").length, logoutPostsBefore, "Saving must not dispatch logout");
  }
  vm.runInContext("state.settingsBusy = false; state.settingsDirty = true;", context);
  elements["logout"].listeners.click();
  assert.equal(elements["confirm-dialog"].open, true, "An idle dirty draft still requires confirmation");
  assert.equal(calls.filter((call) => call.url === "/api/logout" && call.options.method === "POST").length, logoutPostsBefore);
  elements["confirm-cancel"].listeners.click();
  assert.equal(vm.runInContext("state.settingsDirty", context), true, "Cancel keeps the draft");
  vm.runInContext("state.settingsDirty = false;", context);
  elements["logout"].listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.filter((call) => call.url === "/api/logout" && call.options.method === "POST").length, logoutPostsBefore + 1, "Idle clean logout still works");
  assert.equal(elements["login-view"].hidden, false);
  await context.testBoot();
  elements["new-token"].value = "sensitive-test-token";
  elements["google-client-secret"].value = "sensitive-google-secret";
  authenticated = false;
  await vm.runInContext("loadAccounts()", context);
  assert.equal(elements["login-view"].hidden, false);
  assert.equal(elements["app-view"].hidden, true);
  assert.equal(elements["new-token"].value, "");
  assert.equal(elements["google-client-secret"].value, "");
  assert.equal(elements["mail-list"].children.length, 0);
  // A response that arrives after session expiry must not re-open a token dialog.
  authenticated = true; messagesForbidden = false;
  await context.testBoot();
  let release;
  deferredToken = new Promise((resolve) => { release = resolve; });
  elements["token-name"].value = "race test";
  const pending = elements["token-form"].listeners.submit({ preventDefault() {}, currentTarget: elements["token-form"] });
  vm.runInContext("expireSession()", context);
  release({ ok: true, status: 200, json: async () => ({ token: "late-secret", id: "late-id", name: "race test" }) });
  await pending;
  assert.equal(elements["new-token"].value, "");
  assert.equal(elements["secret-dialog"].open, false);
  console.log("UI logic harness: passed (mail rendering, settings/secret rules, client configs, consent/revoke, CSRF, 401/403 and late-secret race)");
})().catch((error) => { console.error(error); process.exitCode = 1; });
