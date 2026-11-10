// Runs the real app logic with an in-memory DOM/API double. This is not browser QA.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "mailhub/static/index.html"), "utf8");
const source = ["translations.js", "i18n.js", "app.js"].map((name) => fs.readFileSync(path.join(root, "mailhub/static", name), "utf8")).join("\n").replace("void boot();", "globalThis.testBoot = boot;");

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
const revealButtons = Object.fromEntries([...html.matchAll(/data-reveal="([\w-]+)"/g)].map(([, id]) => {
  const button = new Element("button");
  button.dataset.reveal = id;
  elements[id].type = "password";
  return [id, button];
}));
elements["ai-client"].value = "codex";
elements["ai-auth"].value = "oauth";
const document = {
  documentElement: { lang: "zh-Hant" }, createTreeWalker: () => ({ nextNode: () => false }),
  hidden: false, title: "", activeElement: new Element("button"),
  getElementById: (id) => elements[id],
  createElement: (tag) => new Element(tag),
  querySelector: (selector) => revealButtons[/^\[data-reveal="([\w-]+)"\]$/.exec(selector)?.[1]] || new Element("button"),
  querySelectorAll: (selector) => {
    if (selector === "[data-reveal]") return Object.values(revealButtons);
    if (selector === "dialog[open]") return Object.values(elements).filter((item) => item.open);
    return [];
  },
  addEventListener() {},
};
let authenticated = true;
let sessionUser = { username: "admin", role: "admin" };
let messagesForbidden = false;
let accountsUnavailable = false;
let calls = [];
let assigned = [];
let deferredToken = null;
let deferredOAuth = null;
let deferredSettingsRead = null;
let consentScope = "mail.read";
let grantRows = [{ id: "grant-1", client_name: "Test AI", created_at: "2026-10-29T08:00:00Z", last_used_at: null }];
const settings = { public_url: "https://mail.example.test", sync_interval_seconds: 300, sync_days: 30, sync_limit: 200,
  google: { client_id: "google-test-id", secret_configured: true, redirect_uri: "https://mail.example.test/api/oauth/gmail/callback" },
  microsoft: { client_id: "", secret_configured: false, redirect_uri: "https://mail.example.test/api/oauth/microsoft/callback" },
  storage: { encrypted: true }, restart_required: false };
const account = { id: "a1", provider: "icloud", email: "example@example.test", label: "測試郵箱", status: "ready", last_sync: "2026-10-29T08:00:00Z", message_count: 1, folder: "INBOX" };
const message = { id: "m1", account_id: "a1", account_email: account.email, provider: "icloud", subject: "<script>bad()</script>", sender: "Test", recipients: "User", received_at: "2026-10-29T08:00:00Z", snippet: "test", body: "<img src=x onerror=bad()>", body_truncated: true, attachments: [] };
const context = vm.createContext({
  document, NodeFilter: { SHOW_TEXT: 4 }, window: { addEventListener() {} }, console, URL, URLSearchParams, Intl, AbortController,
  location: { hash: "#inbox", search: "", pathname: "/", origin: "https://mail.example.test", assign(url) { assigned.push(url); } },
  history: { replaceState() {} }, navigator: { clipboard: { async writeText() {} } },
  Option: class extends Element { constructor(text, value) { super("option"); this.textContent = text; this.value = value; } },
  setTimeout, clearTimeout, setInterval() {},
  fetch: async (url, options) => {
    calls.push({ url, options });
    const pathname = url.split("?")[0];
    if (accountsUnavailable && pathname === "/api/accounts") return { ok: false, status: 503, json: async () => ({ detail: "Unavailable" }) };
    if (!authenticated && pathname !== "/api/session" && pathname !== "/api/login") return { ok: false, status: 401, json: async () => ({ detail: "Session expired" }) };
    if (messagesForbidden && pathname === "/api/messages") return { ok: false, status: 403, json: async () => ({ detail: "拒絕存取" }) };
    if (pathname === "/api/tokens" && options.method === "POST" && deferredToken) return deferredToken;
    if (pathname === "/api/oauth/gmail/start" && deferredOAuth) return deferredOAuth;
    if (pathname === "/api/settings" && options.method === "GET" && deferredSettingsRead) return deferredSettingsRead;
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
      "/api/session": { authenticated, user: sessionUser, csrf_token: "csrf-test", public_url: "https://mail.example.test", sync_interval_seconds: 300, sync_days: 30, sync_limit: 200 },
      "/api/providers": { providers: [{ id: "icloud", name: "iCloud", kind: "imap", host: "imap.mail.me.com", port: 993, note: "test", configured: true }] },
      "/api/accounts": { accounts: [account] },
      "/api/messages": { messages: [message], total: 1, coverage: { days: 30, per_account_limit: 200 } },
      "/api/messages/m1": { message },
      "/api/tokens": { tokens: [] },
      "/api/settings": settings,
      "/api/setup": { steps: [{ id: "service", title: "服務設定", complete: true, detail: "設定已載入，外部連線未驗證。" }, { id: "ai", title: "AI 讀取", complete: false, detail: "尚未取得客戶端讀取記錄。" }], ready: false, counts: { accounts: 1, tokens: 0 }, ai: { mcp_url: "https://mail.example.test/mcp", oauth_supported: true } },
      "/api/diagnostics": { checks: [{ id: "database", name: "資料庫", status: "pass", detail: "僅本機檢查" }], checked_at: "2026-10-29T08:00:00Z" },
      "/api/oauth/grants": { grants: grantRows },
      "/api/oauth/consent": { request_id: "request-1", client_name: "<script>Untrusted AI</script>", redirect_uri: "http://127.0.0.1:7777/callback", scope: consentScope, resource: "https://mail.example.test/mcp", expires_at: "2999-10-29T08:00:00Z" },
    };
    return { ok: true, status: 200, json: async () => responses[pathname] || { ok: true } };
  },
});
function flatten(element) { return String(element.textContent || "") + element.children.map(flatten).join(" "); }

(async () => {
  vm.runInContext(source, context);
  const catalog = vm.runInContext("translations", context);
  const supported = vm.runInContext("Object.keys(languageNames)", context);
  for (const [key, row] of Object.entries(catalog)) {
    for (const lang of supported.filter((value) => value !== "zh-TW")) {
      assert.ok(row[lang]?.trim(), `Missing ${lang} translation: ${key}`);
      assert.deepEqual((row[lang].match(/\{\d+\}/g) || []).sort(), (key.match(/\{\d+\}/g) || []).sort(), `Broken ${lang} placeholders: ${key}`);
    }
  }
  for (const spec of Object.values(vm.runInContext("accessLists", context))) {
    for (const field of ["createdLabel", "loading", "empty", "revoke", "title", "confirm", "success", "consequence"]) {
      assert.ok(Object.hasOwn(catalog, spec[field]), `Missing access-list text: ${spec[field]}`);
    }
  }
  for (const labels of Object.values(vm.runInContext("secretLabels", context))) {
    for (const label of labels) assert.ok(Object.hasOwn(catalog, label), `Missing reveal label: ${label}`);
  }
  accountsUnavailable = true;
  await context.testBoot();
  assert.match(flatten(elements["accounts-list"]), /無法載入郵箱/, "Loading provider names must preserve the account retry state");
  accountsUnavailable = false;
  await context.testBoot();
  await vm.runInContext("loadMessages()", context);
  assert.equal(elements["app-view"].hidden, false);
  assert.equal(elements["login-view"].hidden, true);
  assert.doesNotMatch(elements["codex-config"].textContent, /bearer_token/);
  elements["ai-auth"].value = "token";
  vm.runInContext("updateConnectionConfig()", context);
  assert.match(elements["codex-config"].textContent, /MAILHUB_MCP_TOKEN/);
  assert.equal(elements["account-host"].readOnly, true);
  vm.runInContext('state.providers.push({id:"gmail", name:"Gmail", kind:"oauth", configured:true})', context);
  vm.runInContext('openAccount(); changeProvider("gmail")', context);
  assert.equal(elements["account-dialog"].open, false, "Selecting Gmail opens its settings directly");
  assert.equal(elements["group-accounts"].hidden, false);
  assert.equal(elements["google-settings"].hidden, false);
  assert(!calls.some((call) => call.url.startsWith("/api/oauth/gmail/start")), "Routing must not grant access");
  // This in-memory location double has no native hashchange event.
  vm.runInContext('location.hash = "#settings"; choosePage()', context);
  await vm.runInContext("loadSettings()", context);
  for (const cancel of ['selectSettingsCategory("mcp")', 'selectSettingsProvider("microsoft")', 'markSettingsDirty()', 'location.hash = "#accounts"; choosePage()']) {
    vm.runInContext('location.hash = "#settings"; choosePage(); selectSettingsCategory("accounts"); selectSettingsProvider("google")', context);
    await vm.runInContext("loadSettings()", context);
    let resolveOAuth;
    deferredOAuth = new Promise((resolve) => { resolveOAuth = resolve; });
    const pendingOAuth = elements["google-login"].listeners.click();
    assert.equal(elements["google-login"].disabled, true);
    vm.runInContext(cancel, context);
    resolveOAuth({ok: true, json: async () => ({url:"https://oauth.example.test/old-request"})});
    await pendingOAuth;
    vm.runInContext("discardSettings()", context);
    assert.equal(assigned.length, 0, "An abandoned OAuth start must not redirect");
  }
  vm.runInContext('location.hash = "#settings"; choosePage(); selectSettingsCategory("accounts"); selectSettingsProvider("google")', context);
  await vm.runInContext("loadSettings()", context);
  deferredOAuth = Promise.resolve({ok: true, json: async () => ({url:"https://oauth.example.test/current-request"})});
  await elements["google-login"].listeners.click();
  assert.deepEqual(assigned, ["https://oauth.example.test/current-request"]);
  vm.runInContext('location.hash = "#inbox"; choosePage()', context);
  await vm.runInContext("loadMessages()", context);
  assert.match(elements["coverage"].textContent, /^同步目標：/);
  assert.match(flatten(elements["accounts-list"]), /天數未記錄/);
  assert.match(flatten(elements["mail-list"]), /<script>bad\(\)<\/script>/);
  assert.equal(elements["mail-list"].children[0].tagName, "button");
  const readerTrigger = elements["mail-list"].children[0];
  readerTrigger.focus();
  await vm.runInContext('openMessage("m1")', context);
  assert.match(flatten(elements["reader-content"]), /此郵件僅同步部分內容/);
  assert.match(flatten(elements["reader-content"]), /<img src=x onerror=bad\(\)>/);
  assert.match(flatten(elements["reader-content"]), /天數未記錄/);
  message.sync = { last_sync: "2026-10-29T08:00:00Z", days: 7, per_account_limit: 50 };
  await vm.runInContext('openMessage("m1")', context);
  assert.match(flatten(elements["reader-content"]), /近 7 天 · 最多 50 封/);
  vm.runInContext("closeReader()", context);
  assert.equal(document.activeElement, readerTrigger, "Retrying the reader must retain its original focus target");
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
  const googleReveal = revealButtons["google-client-secret"];
  googleReveal.listeners.click();
  assert.equal(elements["google-client-secret"].type, "text");
  vm.runInContext('applyLanguage("en"); refreshInterfaceLanguage("zh-TW")', context);
  assert.equal(googleReveal.attributes["aria-label"], "Hide Google client secret");
  assert.equal(googleReveal.attributes["aria-pressed"], "true");
  elements["clear-google-secret"].checked = true;
  elements["clear-google-secret"].listeners.change();
  assert.equal(elements["google-client-secret"].type, "password", "Clearing a revealed secret must mask future drafts");
  assert.equal(googleReveal.attributes["aria-label"], "Show Google client secret");
  assert.equal(googleReveal.attributes["aria-pressed"], "false");
  elements["clear-google-secret"].checked = false;
  elements["clear-google-secret"].listeners.change();
  vm.runInContext('discardSettings(); applyLanguage("zh-TW"); refreshInterfaceLanguage("en")', context);
  const blankSettings = vm.runInContext("collectSettings()", context);
  assert.equal(Object.hasOwn(blankSettings, "google_client_secret"), false, "Blank secret must be omitted");
  elements["clear-google-secret"].checked = true;
  const cleared = vm.runInContext("collectSettings()", context);
  assert.equal(cleared.clear_google_secret, true);
  assert.equal(Object.hasOwn(cleared, "google_client_secret"), false);
  elements["clear-google-secret"].checked = false;
  const previousSettings = structuredClone(settings);
  let returnPreviousSettings;
  deferredSettingsRead = new Promise((resolve) => { returnPreviousSettings = resolve; });
  const oldReload = vm.runInContext("loadSettings()", context);
  elements["google-client-secret"].value = "never-export-this-secret";
  elements["sync-days"].value = "90";
  await vm.runInContext("saveSettings({preventDefault(){}})", context);
  const saved = calls.findLast((call) => call.url === "/api/settings" && call.options.method === "POST");
  assert.equal(saved.options.headers["X-CSRF-Token"], "csrf-test");
  assert.equal(JSON.parse(saved.options.body).sync_days, 90);
  assert.equal(elements["google-client-secret"].value, "", "Secret clears after successful save");
  assert.equal(vm.runInContext("state.settingsDirty", context), false);
  returnPreviousSettings({ ok: true, json: async () => previousSettings });
  await oldReload;
  deferredSettingsRead = null;
  assert.equal(elements["sync-days"].value, "90", "A late reload must not replace the saved settings");
  assert.equal(elements["google-login"].disabled, false);
  for (const client of ["codex", "cursor", "deepseek", "claude", "rest", "oauth"]) {
    elements["ai-client"].value = client;
    vm.runInContext("updateConnectionConfig()", context);
    assert.doesNotMatch(elements["codex-config"].textContent, /never-export-this-secret/);
    assert.match(elements["codex-config"].textContent, /mail\.example\.test/);
    if (client === "deepseek") {
      const example = fs.readFileSync(path.join(root, "integrations/deepseek-harness.cordis.yml.example"), "utf8");
      assert.equal(elements["codex-config"].textContent, example.trimEnd().replace("https://mail.example.com/mcp", elements.endpoint.value));
      assert.equal(elements["ai-auth"].value, "token");
      assert.equal(elements["ai-auth"].disabled, true);
    }
  }
  await vm.runInContext("runDiagnostics()", context);
  assert.match(flatten(elements["diagnostics-results"]), /僅本機檢查/);
  await vm.runInContext('loadAccessList("grants")', context);
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
  assert.equal(elements["consent-approve"].textContent, "允許唯讀存取");
  consentScope = "mail.read mail.reply";
  await vm.runInContext("loadConsent()", context);
  assert.equal(elements["consent-approve"].textContent, "允許讀取與回覆");
  assert.match(elements["consent-permissions"].textContent, /傳送郵件回覆/);
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
  // Message content and owner-entered labels can be identical to interface copy.
  vm.runInContext('applyLanguage("fr")', context);
  message.subject = "設定";
  message.sender = "我的郵箱";
  account.label = "介面語言";
  context.localizedMail = message;
  context.localizedAccount = account;
  vm.runInContext("state.accounts = [localizedAccount]; renderAccounts(); renderMessages([localizedMail]);", context);
  assert.match(flatten(elements["accounts-list"]), /介面語言/);
  assert.match(flatten(elements["mail-list"]), /設定/);
  assert.match(flatten(elements["mail-list"]), /我的郵箱/);
  assert.equal(vm.runInContext('t("設定")', context), "Paramètres");
  assert.equal(vm.runInContext('numberFormat.resolvedOptions().locale', context), "fr");
  assert.equal(vm.runInContext('dateFormat.resolvedOptions().timeZone', context), "Europe/Belgrade");
  sessionUser = { username: "mail-reader", role: "member" };
  calls = [];
  context.location.hash = "#settings";
  await vm.runInContext("enterApp()", context);
  assert.equal(elements["workspace-username"].textContent, sessionUser.username);
  assert.equal(elements["settings-form"].hidden, true);
  assert.equal(elements["group-display"].hidden, false);
  assert.equal(elements["category-service"].hidden, true);
  assert(!calls.some((call) => call.url === "/api/settings"), "Member settings must not request global configuration");
  vm.runInContext('state.providers.push({id:"gmail", name:"Gmail", kind:"oauth", configured:true}); location.hash="#accounts"; choosePage(); openAccount(); changeProvider("gmail")', context);
  assert.equal(elements["account-dialog"].open, true);
  assert.equal(elements["mailbox-oauth-pane"].hidden, false);
  assert.equal(elements["mailbox-oauth-login"].disabled, false);
  assert(!calls.some((call) => call.url === "/api/oauth/gmail/start"), "Choosing a provider does not initiate sign-in");
  let releaseMemberOAuth;
  deferredOAuth = new Promise((resolve) => { releaseMemberOAuth = resolve; });
  const memberStart = vm.runInContext('startProviderLogin("gmail", $("mailbox-oauth-login"), $("mailbox-oauth-error"))', context);
  vm.runInContext("closeAccount()", context);
  const redirectsBefore = assigned.length;
  releaseMemberOAuth({ok: true, json: async () => ({url:"https://oauth.example.test/abandoned-member"})});
  await memberStart;
  assert.equal(assigned.length, redirectsBefore, "Closing a member's mailbox dialog cancels its redirect");
  vm.runInContext('openAccount(); changeProvider("gmail")', context);
  deferredOAuth = Promise.resolve({ok: true, json: async () => ({url:"https://oauth.example.test/member"})});
  await vm.runInContext('startProviderLogin("gmail", $("mailbox-oauth-login"), $("mailbox-oauth-error"))', context);
  assert.equal(assigned.at(-1), "https://oauth.example.test/member");
  console.log("UI logic harness: passed (mail rendering, settings/secret rules, client configs, consent/revoke, CSRF, 401/403 and late-secret race)");
})().catch((error) => { console.error(error); process.exitCode = 1; });
