"use strict";

// All mailbox content is untrusted: build nodes with textContent, never HTML.
const $ = (id) => document.getElementById(id);
const state = {
  authenticated: false, csrf: "", session: {}, accounts: [], providers: [],
  registering: false,
  page: "inbox", query: "", accountId: "", offset: 0, limit: 30, total: 0,
  searchEpoch: 0, accountEpoch: 0, accessEpoch: { tokens: 0, grants: 0 }, readerEpoch: 0,
  searchController: null, readerController: null, searchTimer: null, composing: false,
  reply: null,
  accountDirty: false, accountBusy: false, pendingDiscard: null, selectedProvider: "", oauthStart: null,
  detectedImapAddress: "",
  oneTimeSecret: false, confirmTask: null, triggerMap: new WeakMap(), liveTimer: null,
  pollBusy: false, lastSyncSignature: "", lastAccountRenderSignature: "", requests: new Set(),
  settings: null, settingsDirty: false, settingsBusy: false, settingsEpoch: 0, setupEpoch: 0,
  diagnostics: null, settingsCategory: "sync", settingsLoading: false, settingsFocus: "",
  oauthRequest: "", consentData: null, consentBusy: false, consentEpoch: 0,
};
const sessionChanges = typeof BroadcastChannel === "function" ? new BroadcastChannel("mailharbor.session") : null;
sessionChanges?.addEventListener("message", () => {
  // The session cookie is shared across tabs. Clear any previous workspace before reloading.
  expireSession("");
  location.reload();
});
window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
function isAdmin() {
  return state.session.user?.role === "admin";
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function date(value, long = false) {
  if (!value) return t("尚未同步");
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? t("時間不明") : (long ? longDateFormat : dateFormat).format(parsed);
}
function safeURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}
function providerName(id) {
  return t(state.providers.find((provider) => provider.id === id)?.name || id);
}
function snapshotRange(days, limit) {
  const range = Number.isInteger(days) && days > 0 ? t("近 {0} 天", numberFormat.format(days)) : t("天數未記錄");
  const count = Number.isInteger(limit) && limit > 0 ? t("最多 {0} 封", numberFormat.format(limit)) : t("封數未記錄");
  return range + " · " + count;
}
function notify(message) {
  clearTimeout(state.liveTimer);
  $("live-status").textContent = message;
  state.liveTimer = setTimeout(() => { $("live-status").textContent = ""; }, 4500);
}
function setBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
}
function errorText(error) {
  if (error.name === "AbortError") return "";
  return t(error.message || "") || t("無法完成要求，請稍後重試。");
}
function setFieldError(input, message) {
  input.setAttribute("aria-invalid", message ? "true" : "false");
  const target = $(input.id + "-error");
  if (target) target.textContent = message;
}
function clearValidation(form) {
  for (const input of form.querySelectorAll("input, textarea")) setFieldError(input, "");
}
function validateForm(form) {
  clearValidation(form);
  let first = null;
  for (const input of form.querySelectorAll("input, textarea")) {
    let message = "";
    if (input.required && !(input.id === "login-password" ? input.value : input.value.trim())) message = t("請填寫此欄位。");
    else if (input.id === "reply-body" && Array.from(input.value).length > 12000) message = t("回覆內容最多為 12,000 個字元。");
    else if (input.id === "login-username" && state.registering && !/^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/.test(input.value.trim())) message = t("使用者名稱須為 3–64 個字元，以英文字母或數字開頭，可包含點、底線及連字號。");
    else if (input.id === "login-password" && Array.from(input.value).length > 1024) message = t("內容過長，請縮短後重試。");
    else if (input.id === "login-password" && state.registering && Array.from(input.value).length < 12) message = t("密碼至少需要 12 個字元。");
    else if (input.type === "email" && input.value && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(input.value)) message = t("請填寫完整的郵箱地址。");
    else if (input.type === "number" && (!Number.isInteger(Number(input.value)) || Number(input.value) < Number(input.min) || Number(input.value) > Number(input.max))) message = t("請填寫 {0}–{1} 之間的整數。", input.min, input.max);
    else if (input.maxLength > 0 && input.value.length > input.maxLength) message = t("內容過長，請縮短後重試。");
    setFieldError(input, message);
    if (message && !first) first = input;
  }
  if (first) {
    revealSettingsField(first);
    const details = first.closest("details");
    if (details) details.open = true;
    first.focus();
  }
  return !first;
}
const secretLabels = {
  "login-password": ["顯示密碼", "隱藏密碼"],
  "account-password": ["顯示郵箱授權碼", "隱藏郵箱授權碼"],
  "new-token": ["顯示存取金鑰", "隱藏存取金鑰"],
  "google-client-secret": ["顯示 Google Client Secret", "隱藏 Google Client Secret"],
  "microsoft-client-secret": ["顯示 Microsoft Client Secret", "隱藏 Microsoft Client Secret"],
};
function setSecretVisibility(id, shown) {
  const button = document.querySelector(`[data-reveal="${id}"]`);
  const [showLabel, hideLabel] = secretLabels[id];
  $(id).type = shown ? "text" : "password";
  button.textContent = shown ? t("隱藏") : t("顯示");
  button.setAttribute("aria-pressed", String(shown));
  button.setAttribute("aria-label", t(shown ? hideLabel : showLabel));
}
function clearSecret(id) {
  $(id).value = "";
  setSecretVisibility(id, false);
}
function resetSecrets() {
  for (const id of Object.keys(secretLabels)) clearSecret(id);
  state.oneTimeSecret = false;
}
function openDialog(dialog, focusTarget) {
  if (!dialog.open) {
    state.triggerMap.set(dialog, document.activeElement);
    dialog.showModal();
  }
  (focusTarget || dialog.querySelector("button, input, select"))?.focus();
}
function closeDialog(dialog) {
  if (dialog.open) dialog.close();
  const trigger = state.triggerMap.get(dialog);
  if (trigger?.isConnected && !trigger.disabled) trigger.focus();
  else if (state.authenticated) $("main").focus();
}
function expireSession(message = t("登入已逾時，請重新登入。")) {
  state.authenticated = false;
  state.session = {};
  state.csrf = "";
  state.searchEpoch += 1;
  state.accountEpoch += 1;
  state.accessEpoch.tokens += 1;
  state.readerEpoch += 1;
  cancelOAuthStart();
  clearTimeout(state.searchTimer);
  for (const controller of state.requests) controller.abort();
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  resetReply();
  state.accountDirty = false;
  state.settingsDirty = false;
  state.settingsBusy = false;
  state.settings = null;
  state.settingsLoading = false;
  state.settingsFocus = "";
  state.diagnostics = null;
  state.consentData = null;
  state.settingsEpoch += 1;
  state.setupEpoch += 1;
  state.accessEpoch.grants += 1;
  state.consentEpoch += 1;
  state.accounts = [];
  state.providers = [];
  state.detectedImapAddress = "";
  state.settingsCategory = "sync";
  state.confirmTask = null;
  state.pendingDiscard = null;
  state.lastSyncSignature = "";
  state.lastAccountRenderSignature = "";
  state.query = "";
  state.offset = 0;
  state.accountId = "";
  $("mail-search").value = "";
  $("clear-search").hidden = true;
  for (const id of ["mail-list", "accounts-list", "tokens-list", "reader-content", "grants-list", "setup-steps", "diagnostics-results", "consent-details", "account-filter", "workspace-username", "inbox-count", "account-count", "page-range", "confirm-title", "confirm-description", "confirm-error", "live-status"]) $(id).replaceChildren();
  $("callback-banner").hidden = true;
  $("setup-panel").hidden = true;
  $("account-form").reset();
  $("token-form").reset();
  $("settings-form").reset();
  $("settings-fields").disabled = true;
  resetSecrets();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("boot").hidden = true;
  $("login-form").reset();
  state.registering = false;
  renderAuth();
  clearValidation($("login-form"));
  $("login-error").textContent = message;
  $("login-username").focus();
}
async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), options.timeout || 45000);
  const external = options.signal;
  const abortExternal = () => controller.abort();
  external?.addEventListener("abort", abortExternal, { once: true });
  if (external?.aborted) controller.abort();
  state.requests.add(controller);
  const method = options.method || "GET";
  const csrfAtDispatch = state.csrf;
  const headers = { "Accept": "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  try {
    const response = await fetch(path, {
      method, credentials: "same-origin", cache: "no-store", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: controller.signal,
    });
    let data;
    try { data = await response.json(); } catch { data = null; }
    if (csrfAtDispatch && !["/api/login", "/api/session"].includes(path) && state.csrf !== csrfAtDispatch) {
      const stale = new Error(t("登入狀態已變更，忽略舊要求的回應。")); stale.name = "AbortError"; throw stale;
    }
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/login") expireSession();
      const defaultMessage = response.status === 403 ? t("沒有權限完成這項操作，請重新登入後重試。") : t("無法完成要求，請稍後重試。");
      const detail = typeof data?.detail === "string" ? data.detail : defaultMessage;
      const error = new Error(detail);
      error.status = response.status;
      error.code = data?.code;
      throw error;
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      const invalid = new Error(method === "GET" ? t("伺服器回應格式無效，請重新整理。") : t("伺服器回應格式無效，操作結果尚未確認。請先重新整理列表確認。"));
      invalid.uncertain = method !== "GET";
      throw invalid;
    }
    $("network-banner").hidden = true;
    return data;
  } catch (error) {
    if (controller.signal.aborted && !external?.aborted && controller.signal.reason === "timeout") {
      const timed = new Error(method === "GET" ? t("伺服器回應逾時，請重新整理。") : t("等待逾時，操作可能已完成。請先重新整理列表確認，再嘗試一次。"));
      timed.uncertain = method !== "GET";
      throw timed;
    }
    if (error instanceof TypeError && !controller.signal.aborted) {
      $("network-banner").textContent = t("暫時無法連接伺服器。現有資料可能不是最新狀態，請檢查網路後重新整理。");
      $("network-banner").hidden = false;
      const network = new Error(method === "GET" ? t("無法連接伺服器，請檢查網路後重試。") : t("連線中斷，操作結果尚未確認。請先重新整理列表確認。"));
      network.uncertain = method !== "GET";
      throw network;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    external?.removeEventListener("abort", abortExternal);
    state.requests.delete(controller);
  }
}
function emptyState(container, title, description, action, actionLabel, symbol = "▤") {
  const wrapper = node("div", "empty-state");
  const icon = node("span", "empty-symbol", symbol);
  icon.setAttribute("aria-hidden", "true");
  wrapper.append(icon, node("h2", "", title), node("p", "", description));
  if (action) {
    const button = node("button", "button outline", actionLabel);
    button.type = "button";
    button.addEventListener("click", action);
    wrapper.append(button);
  }
  container.replaceChildren(wrapper);
}
function updateCoverage(coverage) {
  const days = coverage?.days || state.session.sync_days || 30;
  const limit = coverage?.per_account_limit || state.session.sync_limit || 200;
  $("coverage").textContent = t("同步目標：近 {0} 天 · 每個郵箱最多 {1} 封", numberFormat.format(days), numberFormat.format(limit));
  const seconds = state.session.sync_interval_seconds || 300;
  const interval = seconds % 60 === 0 ? t("{0} 分鐘", seconds / 60) : t("{0} 秒", seconds);
  const failed = state.accounts.filter((account) => account.status === "error").length;
  const syncing = state.accounts.filter((account) => account.status === "syncing").length;
  const latest = state.accounts.map((account) => account.last_sync).filter(Boolean).sort().at(-1);
  const pieces = [t("自動同步約每 {0}", interval), t("各帳號設定的資料夾")];
  if (latest) pieces.push(t("最近完成 {0}", date(latest)));
  if (syncing) pieces.push(t("{0} 個同步中", syncing));
  if (failed) pieces.push(t("{0} 個郵箱需要處理", failed));
  $("sync-summary").textContent = pieces.join(" · ") + t("。目前快取依各郵箱上次同步；目標變更於下次成功同步後套用。");
}
function choosePage() {
  const desired = location.hash.slice(1);
  const destination = ["inbox", "accounts", "connect", "settings"].includes(desired) ? desired : "inbox";
  if (state.settingsBusy && state.page === "settings" && destination !== "settings") {
    history.replaceState(null, "", location.pathname + location.search + "#settings");
    notify(t("正在儲存設定，請稍候。")); return;
  }
  if (state.settingsDirty && state.page === "settings" && destination !== "settings") {
    history.replaceState(null, "", location.pathname + location.search + "#settings");
    askConfirmation(t("離開未儲存的設定？"), t("你填寫的同步設定與授權資料尚未儲存。離開會捨棄這些變更。"), t("捨棄變更並離開"), async () => {
      discardSettings(); location.hash = destination;
    });
    return;
  }
  if (state.page !== destination) cancelOAuthStart();
  state.page = destination;
  for (const page of ["inbox", "accounts", "connect", "settings"]) $("page-" + page).hidden = page !== state.page;
  if (destination === "settings" && state.settingsFocus) {
    $(state.settingsFocus).focus();
    state.settingsFocus = "";
  }
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === state.page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const titles = { inbox: t("統一收件匣"), accounts: t("我的郵箱"), connect: t("連接 AI"), settings: t("設定") };
  document.title = state.authenticated ? `${titles[state.page]} · MailHarbor` : t("登入 · MailHarbor");
  if (state.authenticated) {
    if (state.page === "inbox") void loadMessages();
    if (state.page === "accounts") void loadAccounts();
    if (state.page === "connect") { void loadAccessList("tokens"); void loadAccessList("grants"); }
    if (state.page === "settings" && isAdmin() && !state.settingsDirty) void loadSettings();
  }
}
function updateConnectionConfig() {
  const origin = (state.session.public_url || location.origin).replace(/\/$/, "");
  const url = origin + "/mcp";
  const client = $("ai-client").value || "codex";
  const authSelect = $("ai-auth");
  const tokenOnly = ["deepseek", "claude", "rest"].includes(client);
  if (tokenOnly) authSelect.value = "token";
  if (client === "oauth") authSelect.value = "oauth";
  authSelect.disabled = tokenOnly || client === "oauth";
  const token = authSelect.value === "token";
  let config, title, help, note;
  $("endpoint").value = client === "rest" ? origin + "/v1" : url;
  $("endpoint-label").textContent = client === "rest" ? t("REST API 位址") : t("MCP 伺服器位址");
  $("transport-label").textContent = client === "rest" ? "HTTPS · OpenAPI" : client === "claude" ? "stdio → HTTPS" : "Streamable HTTP";
  $("openapi-link").hidden = client !== "rest";
  $("openapi-link").href = origin + "/openapi.json";
  if (client === "codex") {
    title = "Codex · config.toml";
    config = `[mcp_servers.mailharbor]\nurl = ${JSON.stringify(url)}` + (token ? '\nbearer_token_env_var = "MAILHUB_MCP_TOKEN"' : "");
    help = token ? t("將設定加入 Codex 的 config.toml，並在啟動 Codex 的環境中設定 MAILHUB_MCP_TOKEN。") : t("將設定加入 config.toml。若要讀取郵件，執行 codex mcp login mailharbor --scopes mail.read。若也要回覆，請使用 --scopes mail.read,mail.reply。");
    note = token ? t("在右側建立存取金鑰。設定範例只引用環境變數，不會包含金鑰。") : t("OAuth 由 Codex 管理登入憑證；不需要把管理密碼或郵箱密碼填入 Codex。");
  } else if (client === "cursor") {
    title = "Cursor · mcp.json";
    const entry = { url };
    if (token) entry.headers = { Authorization: "Bearer ${env:MAILHUB_MCP_TOKEN}" };
    config = JSON.stringify({ mcpServers: { mailharbor: entry } }, null, 2);
    help = token ? t("加入 Cursor 的 MCP 設定，並確保 Cursor 啟動環境能讀到 MAILHUB_MCP_TOKEN。") : t("加入 Cursor 的 MCP 設定後，從 Cursor 啟動連線／登入，在此工作台確認授權。");
    note = token ? t("環境變數插值會由 Cursor 處理；此範例不含實際金鑰。") : t("實際授權完成前，新增設定不代表已成功連接。");
  } else if (client === "deepseek") {
    title = "DeepSeek Harness · mailharbor.cordis.yml";
    config = `- insert:
    - id: mailharbor-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: mailharbor
        transport: streamable-http
        url: ${JSON.stringify(url)}
        headers:
          Authorization: !!js "'Bearer ' + process.env.MAILHUB_MCP_TOKEN"`;
    help = t("建立 MailHarbor 存取金鑰，將它設為啟動 DeepSeek Harness 的環境變數 MAILHUB_MCP_TOKEN。");
    note = t("將設定存為 mailharbor.cordis.yml，在同一目錄執行 dsh web --patch mailharbor.cordis.yml，再請 DeepSeek 呼叫 mcp__mailharbor__get_sync_status。");
  } else if (client === "claude") {
    title = t("Claude Desktop · 本機 stdio 設定");
    config = JSON.stringify({ mcpServers: { mailharbor: {
      command: "/path/to/MailHarbor/.venv/bin/python",
      args: ["/path/to/MailHarbor/scripts/stdio_bridge.py", "--url", url],
      env: { MAILHUB_MCP_TOKEN: "REPLACE_IN_PRIVATE_LOCAL_CONFIG_ONLY" },
    } } }, null, 2);
    help = t("在使用 Claude Desktop 的電腦安裝本專案與依賴，將兩個 /path/to 路徑換成該電腦的絕對路徑，再加入 Claude Desktop 的 MCP 設定。");
    note = t("這是本機橋接器，不是 VPS 上的檔案路徑。token 欄位只是佔位字；填入專用金鑰，或改由安全啟動器注入 MAILHUB_MCP_TOKEN。不要分享填入金鑰後的設定。");
  } else if (client === "rest") {
    title = t("REST 呼叫範例");
    config = `curl --fail-with-body \\\n  --header "Authorization: Bearer $MAILHUB_MCP_TOKEN" \\\n  "${origin}/v1/status"`;
    help = t("供自訂 AI 工具或 API 用戶端使用。讀取 /openapi.json 的唯讀路由定義，並在 Authorization 標頭傳入 Bearer 金鑰。");
    note = t("可用路由：GET /v1/accounts、/v1/messages、/v1/messages/{id}、/v1/status。搜尋與閱讀仍限於已同步快取。");
  } else {
    title = t("OAuth MCP 連線資料");
    config = JSON.stringify({ mcp_url: url, authorization_server: origin, protected_resource_metadata: origin + "/.well-known/oauth-protected-resource/mcp", scope: "mail.read" }, null, 2);
    help = t("在支援遠端 MCP 與 OAuth 的 AI 工具中，新增上方 MCP 位址並啟動登入。工具需支援動態註冊、授權碼流程與 S256 PKCE。");
    note = t("預設範圍為 mail.read。需要回覆功能的客戶端應要求 mail.read mail.reply，並由你在登入時確認授權。");
  }
  $("config-title").textContent = title;
  $("client-instructions").textContent = help;
  $("config-note").textContent = note;
  $("codex-config").textContent = config;
}
async function enterApp() {
  const session = await api("/api/session");
  if (!session.authenticated) { expireSession(""); return; }
  state.session = session;
  state.authenticated = true;
  state.csrf = session.csrf_token;
  $("workspace-username").textContent = session.user.username;
  const admin = isAdmin();
  for (const item of document.querySelectorAll("[data-admin-only]")) item.hidden = !admin;
  for (const name of ["sync", "accounts", "service"]) $("category-" + name).hidden = !admin;
  $("settings-form").hidden = !admin;
  $("settings-status").hidden = true;
  $("retry-settings").hidden = true;
  selectSettingsCategory(admin ? "sync" : "display");
  $("login-view").hidden = true;
  $("boot").hidden = true;
  $("app-view").hidden = false;
  clearSecret("login-password");
  updateConnectionConfig();
  updateCoverage();
  const results = await Promise.allSettled([api("/api/providers"), loadAccounts()]);
  if (!state.authenticated) return;
  if (results[0].status === "fulfilled") {
    state.providers = results[0].value.providers;
    renderProviders();
    if (state.accounts.length) renderAccounts();
  } else {
    $("callback-banner").textContent = t("無法取得郵箱服務清單，請重新整理頁面後再連接郵箱。");
    $("callback-banner").hidden = false;
  }
  choosePage();
  void loadSetup();
  if (state.oauthRequest) void loadConsent();
}
async function loadAccounts() {
  const epoch = ++state.accountEpoch;
  try {
    const { accounts } = await api("/api/accounts");
    if (epoch !== state.accountEpoch || !state.authenticated) return;
    state.accounts = accounts;
    const total = accounts.reduce((sum, account) => sum + Number(account.message_count || 0), 0);
    $("inbox-count").textContent = numberFormat.format(total);
    $("account-count").textContent = numberFormat.format(accounts.length);
    const filter = $("account-filter");
    filter.replaceChildren(new Option(t("所有郵箱"), ""));
    for (const account of accounts) filter.add(new Option(account.label ? `${account.label} · ${account.email}` : account.email, account.id));
    if (state.accountId && !accounts.some((account) => account.id === state.accountId)) {
      state.accountId = "";
      state.offset = 0;
    }
    filter.value = state.accountId;
    const renderSignature = JSON.stringify(accounts);
    if (renderSignature !== state.lastAccountRenderSignature) {
      renderAccounts();
      state.lastAccountRenderSignature = renderSignature;
    }
    updateCoverage();
    const signature = JSON.stringify(accounts.map((account) => [account.id, account.last_sync, account.message_count]));
    if (state.lastSyncSignature && signature !== state.lastSyncSignature && state.page === "inbox") void loadMessages();
    state.lastSyncSignature = signature;
  } catch (error) {
    if (epoch !== state.accountEpoch || !state.authenticated) return;
    if (!state.accounts.length) {
      state.lastAccountRenderSignature = "";
      emptyState($("accounts-list"), t("無法載入郵箱"), errorText(error), () => void loadAccounts(), t("重新載入"), "!");
    }
  }
}
function renderAccounts() {
  const container = $("accounts-list");
  if (!state.accounts.length) {
    emptyState(container, t("先連接第一個郵箱"), t("選擇郵箱服務完成授權。之後可以繼續加入其他服務或同一服務的不同帳號。"), openAccount, t("連接郵箱"), "◫");
    return;
  }
  container.replaceChildren();
  for (const account of state.accounts) {
    const card = node("article", "account-card panel");
    const header = node("div", "account-card-header");
    const initials = providerName(account.provider).slice(0, 1).toUpperCase();
    const status = account.status === "error" ? ["error", t("需要處理")] : account.status === "syncing" ? ["pending", t("同步中")] : ["", account.last_sync ? t("已連接") : t("等待同步")];
    header.append(node("span", "provider-icon", initials), node("span", "tag " + status[0], status[1]));
    card.append(header, node("h2", "", account.label || account.email), node("p", "email", account.label ? account.email : providerName(account.provider)));
    const metadata = node("dl", "account-meta");
    for (const [label, value] of [[t("郵箱服務"), providerName(account.provider)], [t("快取郵件"), t("{0} 封", numberFormat.format(account.message_count || 0))], [t("同步資料夾"), account.folder || "INBOX"], [t("快取同步範圍"), snapshotRange(account.snapshot_days, account.snapshot_limit)], [t("上次同步"), date(account.last_sync, true)]]) {
      const row = node("div"); row.append(node("dt", "", label), node("dd", "", value)); metadata.append(row);
    }
    card.append(metadata);
    if (account.error) card.append(node("p", "banner error", t(account.error)));
    const actions = node("div", "account-actions");
    const sync = node("button", "button outline small", account.status === "syncing" ? t("同步中") : t("立即同步"));
    sync.type = "button"; sync.disabled = account.status === "syncing";
    sync.addEventListener("click", () => void syncAccount(account, sync));
    const disconnect = node("button", "button danger-text small", t("斷開連接"));
    disconnect.type = "button";
    disconnect.addEventListener("click", () => askConfirmation(t("斷開郵箱連接"), t("斷開「{0}」後，會移除本服務保存的登入資料和郵件快取。原郵箱中的郵件不會刪除。", account.email), t("斷開連接"), async () => {
      await api(`/api/accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" });
      await loadAccounts();
      await loadSetup();
      if (state.page === "inbox") await loadMessages();
      notify(t("已斷開郵箱連接。"));
    }));
    actions.append(sync, disconnect); card.append(actions); container.append(card);
  }
}
async function syncAccount(account, button) {
  setBusy(button, true);
  try {
    await api(`/api/accounts/${encodeURIComponent(account.id)}/sync`, { method: "POST" });
    notify(t("已開始同步，可在郵箱狀態查看進度。"));
    await loadAccounts();
  } catch (error) { notify(errorText(error)); await loadAccounts(); }
  finally { if (button.isConnected) setBusy(button, false); }
}
async function loadMessages() {
  if (!state.authenticated) return;
  state.searchController?.abort();
  const controller = new AbortController();
  state.searchController = controller;
  const epoch = ++state.searchEpoch;
  const list = $("mail-list");
  list.setAttribute("aria-busy", "true");
  $("prev-page").disabled = true;
  $("next-page").disabled = true;
  if (!list.children.length) emptyState(list, t("正在載入郵件"), t("從你的中轉站讀取已同步的郵件。"), null, "", "↻");
  const query = new URLSearchParams({ q: state.query, account_id: state.accountId, offset: String(state.offset), limit: String(state.limit) });
  try {
    const data = await api(`/api/messages?${query}`, { signal: controller.signal });
    if (epoch !== state.searchEpoch || !state.authenticated) return;
    state.total = Number(data.total || 0);
    if (state.offset > 0 && state.offset >= state.total) {
      state.offset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
      void loadMessages(); return;
    }
    updateCoverage(data.coverage);
    renderMessages(data.messages);
    const start = state.total ? state.offset + 1 : 0;
    const end = Math.min(state.offset + data.messages.length, state.total);
    $("page-range").textContent = t("{0}–{1} ／ {2} 封", numberFormat.format(start), numberFormat.format(end), numberFormat.format(state.total));
    $("prev-page").disabled = state.offset === 0;
    $("next-page").disabled = state.offset + state.limit >= state.total;
  } catch (error) {
    if (epoch !== state.searchEpoch || controller.signal.aborted || !state.authenticated) return;
    emptyState(list, t("無法載入郵件"), errorText(error), () => void loadMessages(), t("重新載入"), "!");
    $("page-range").textContent = t("尚未取得最新結果");
  } finally { if (epoch === state.searchEpoch) list.setAttribute("aria-busy", "false"); }
}
function renderMessages(messages) {
  const list = $("mail-list");
  if (!messages.length) {
    if (state.query) emptyState(list, t("沒有符合的郵件"), t("試試較短的關鍵字，或切換郵箱。搜尋僅涵蓋目前已同步的郵件。"), clearSearch, t("清除搜尋"), "⌕");
    else if (!state.accounts.length) emptyState(list, t("郵件會在這裡匯合"), t("連接你的第一個郵箱，開始跨帳號閱讀與搜尋。"), openAccount, t("連接郵箱"));
    else emptyState(list, t("目前沒有快取郵件"), t("郵箱可能尚未完成同步，或同步範圍內沒有郵件。到「我的郵箱」查看狀態。"), () => { location.hash = "accounts"; }, t("查看我的郵箱"));
    return;
  }
  list.replaceChildren();
  for (const message of messages) {
    const row = node("button", "mail-row" + (message.unread ? " unread" : ""));
    row.type = "button";
    row.setAttribute("aria-label", `${message.unread ? t("未讀，") : ""}${message.subject || t("無主旨")}，${message.sender || t("寄件者不明")}，${message.account_email || ""}`);
    const sender = node("span", "mail-sender", message.sender || t("寄件者不明"));
    const summary = node("span", "mail-summary");
    const title = node("span", "mail-subject", message.subject || t("（無主旨）"));
    const snippet = node("span", "mail-snippet", message.snippet || t("開啟以閱讀內文"));
    const origin = node("span", "mail-origin");
    origin.append(node("span", "tag neutral", providerName(message.provider)), node("span", "", message.account_email || ""));
    if (message.unread) origin.append(node("span", "", t("未讀")));
    if (message.attachments?.length) origin.append(node("span", "", t("{0} 個附件", message.attachments.length)));
    summary.append(title, snippet, origin);
    const received = node("time", "mail-date", date(message.received_at));
    if (message.received_at) received.dateTime = message.received_at;
    row.append(sender, summary, received);
    row.addEventListener("click", () => void openMessage(message.id));
    list.append(row);
  }
}
function searchNow() {
  clearTimeout(state.searchTimer);
  state.query = $("mail-search").value.trim();
  state.offset = 0;
  $("clear-search").hidden = !$("mail-search").value;
  void loadMessages();
}
function scheduleSearch() {
  clearTimeout(state.searchTimer);
  $("clear-search").hidden = !$("mail-search").value;
  if (state.composing) return;
  state.searchController?.abort();
  state.searchEpoch += 1;
  state.searchTimer = setTimeout(searchNow, 300);
}
function clearSearch() {
  $("mail-search").value = "";
  state.composing = false;
  state.searchController?.abort();
  searchNow();
  $("mail-search").focus();
}
async function openMessage(id) {
  resetReply();
  state.readerController?.abort();
  const controller = new AbortController();
  state.readerController = controller;
  const epoch = ++state.readerEpoch;
  const content = $("reader-content");
  const title = node("h2", "", t("正在讀取郵件")); title.id = "reader-title";
  content.replaceChildren(title, node("p", "muted", t("正在讀取郵件快取…")));
  openDialog($("reader-dialog"), $("close-reader"));
  try {
    const { message } = await api(`/api/messages/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (epoch !== state.readerEpoch || !$("reader-dialog").open || !state.authenticated) return;
    title.textContent = message.subject || t("（無主旨）");
    const metadata = node("dl", "reader-meta");
    const recipients = Array.isArray(message.recipients) ? message.recipients.join(", ") : message.recipients;
    for (const [label, value] of [[t("寄件者"), message.sender], [t("收件者"), recipients], [t("時間"), date(message.received_at, true) + " · Europe/Belgrade"], [t("來源"), message.account_email], [t("快取同步"), message.sync?.last_sync ? date(message.sync.last_sync, true) + " · Europe/Belgrade" : t("未記錄")], [t("同步範圍"), snapshotRange(message.sync?.days, message.sync?.per_account_limit)]]) {
      metadata.append(node("dt", "", label), node("dd", "", value || "—"));
    }
    const body = node("div", "reader-body", message.body || t("此郵件沒有可顯示的文字內文。"));
    content.replaceChildren(title, metadata);
    if (message.body_truncated) content.append(node("p", "banner warning", t("此郵件僅同步部分內容。完整內容請至原郵箱查看。")));
    content.append(body);
    if (message.attachments?.length) {
      const section = node("section", "attachment-list");
      section.append(node("h3", "", t("附件（{0}）", message.attachments.length)), node("p", "small muted", t("僅列附件資料，不提供附件內容。")));
      for (const attachment of message.attachments) section.append(node("div", "attachment", attachment.filename || t("未命名附件")));
      content.append(section);
    }
    const original = safeURL(message.source_url);
    if (original) {
      const link = node("a", "button outline", t("在原郵箱開啟 ↗"));
      link.href = original; link.target = "_blank"; link.rel = "noopener noreferrer";
      content.append(link);
    }
    const reply = message.reply;
    if (reply?.available && reply.recipients?.length) {
      state.reply = {id, busy: false, requestId: null};
      $("reply-from").textContent = message.account_email;
      $("reply-to").textContent = reply.recipients.join(", ");
      $("open-reply").hidden = false;
    } else if (reply) {
      $("reply-status").textContent = replyReason(reply.reason);
    }
  } catch (error) {
    if (epoch !== state.readerEpoch || controller.signal.aborted || !state.authenticated) return;
    title.textContent = t("無法讀取郵件");
    const retry = node("button", "button outline", t("重新載入")); retry.type = "button"; retry.addEventListener("click", () => void openMessage(id));
    content.replaceChildren(title, node("p", "error-line", errorText(error)), retry);
  }
}
function closeReader() {
  discardReply(() => {
    state.readerController?.abort(); state.readerEpoch += 1;
    resetReply();
    closeDialog($("reader-dialog"));
    $("reader-content").replaceChildren();
  });
}
function replyReason(reason) {
  switch (reason) {
    case "reauth_required": return t("重新連接此郵箱時，勾選「允許此郵箱寄送回覆」並完成授權。");
    case "smtp_not_configured": return t("重新連接此郵箱並設定 SMTP 伺服器後，即可回覆。");
    case "sync_required": return t("請先同步此郵箱，再重新開啟郵件以回覆。");
    case "invalid_recipient": return t("此郵件沒有有效的回覆地址。");
    case "unsupported": return t("此郵箱服務不支援回覆。");
    case "thread_unavailable": return t("原郵件缺少 Gmail 對話識別資料，請在 Gmail 中回覆。");
    default: return "";
  }
}
function resetReply() {
  state.reply = null;
  $("open-reply").hidden = true;
  $("open-reply").setAttribute("aria-expanded", "false");
  $("reply-form").hidden = true;
  $("reply-body").value = "";
  $("reply-body").readOnly = false;
  for (const id of ["reply-from", "reply-to", "reply-status", "reply-error"]) $(id).textContent = "";
  setFieldError($("reply-body"), "");
  setBusy($("send-reply"), false);
  $("cancel-reply").disabled = false;
  $("close-reader").disabled = false;
}
function discardReply(action) {
  if (state.reply?.busy) return;
  if (!$("reply-form").hidden && $("reply-body").value) {
    askConfirmation(t("捨棄回覆？"), t("關閉後將清除這份回覆草稿。"), t("捨棄草稿"), action);
  } else action();
}
async function sendReply(event) {
  event.preventDefault();
  const draft = state.reply;
  if (!draft || draft.busy || $("reply-form").hidden || !validateForm($("reply-form"))) return;
  draft.requestId ??= crypto.randomUUID();
  draft.busy = true;
  $("reply-error").textContent = "";
  $("reply-body").readOnly = true;
  $("cancel-reply").disabled = true;
  $("close-reader").disabled = true;
  setBusy($("send-reply"), true);
  let uncertain = false;
  try {
    const result = await api(`/api/messages/${encodeURIComponent(draft.id)}/reply`, {
      method: "POST", body: {body: $("reply-body").value, request_id: draft.requestId}, timeout: 90000,
    });
    if (state.reply !== draft) return;
    if (result.accepted !== true) throw Object.assign(new Error(), {uncertain: true});
    resetReply();
    $("reply-status").textContent = t("寄件服務已接受回覆。");
    $("close-reader").focus();
  } catch (error) {
    if (state.reply !== draft) return;
    uncertain = error.uncertain || ["delivery_unknown", "reply_pending", "request_conflict"].includes(error.code) || error.status >= 500;
    $("reply-error").textContent = uncertain ? t("傳送結果尚未確認。請先查看原郵箱的寄件備份，再決定是否重試。") : replyReason(error.code) || errorText(error);
    if (!uncertain) draft.requestId = null;
  } finally {
    if (state.reply === draft) {
      draft.busy = false;
      $("reply-body").readOnly = Boolean(uncertain);
      $("close-reader").disabled = false;
      $("cancel-reply").disabled = false;
      setBusy($("send-reply"), false);
    }
  }
}
function renderProviders() {
  const current = state.providers.find((provider) => provider.id === state.selectedProvider);
  const select = $("provider-select"); select.replaceChildren();
  for (const provider of state.providers) select.add(new Option(t(provider.name), provider.id));
  if ($("account-dialog").open) {
    if (current) {
      select.value = current.id;
      if (current.kind === "imap") return; // A settings refresh must preserve the active IMAP draft.
    } else {
      $("account-form").reset();
      clearSecret("account-password");
      state.accountDirty = false;
    }
  }
  configureProvider();
}
function cancelOAuthStart() {
  if (!state.oauthStart) return;
  state.oauthStart.controller.abort();
  setBusy(state.oauthStart.button, false);
  state.oauthStart.permission.disabled = false;
  state.oauthStart = null;
  refreshSettingsLoginButtons();
}
function configureProvider() {
  const provider = state.providers.find((item) => item.id === $("provider-select").value);
  state.selectedProvider = provider?.id || "";
  $("account-form").hidden = provider?.kind !== "imap";
  $("unsupported-pane").hidden = provider?.kind !== "unsupported";
  $("mailbox-oauth-pane").hidden = isAdmin() || provider?.kind !== "oauth";
  $("mailbox-oauth-login").disabled = !provider?.configured || Boolean(state.oauthStart);
  $("mailbox-allow-reply").disabled = $("mailbox-oauth-login").disabled;
  $("mailbox-allow-reply").checked = false;
  $("mailbox-oauth-error").textContent = "";
  $("provider-note").textContent = t(provider?.note || "");
  if (!isAdmin() && provider?.kind === "oauth" && !provider.configured) {
    $("provider-note").textContent = t("管理員尚未設定此服務的 OAuth 憑證。");
  }
  const guide = safeURL(provider?.guide_url);
  $("provider-guide").hidden = !guide;
  if (guide) $("provider-guide").href = guide;
  $("account-host").value = provider?.host || "";
  $("account-host").readOnly = Boolean(provider?.host);
  $("use-custom-imap").hidden = !provider?.host || !state.providers.some((item) => item.id === "custom");
  $("account-port").value = String(provider?.port || 993);
  $("smtp-settings").hidden = provider?.id !== "custom";
  $("account-smtp-host").value = "";
  $("account-smtp-port").value = "465";
  $("advanced-settings").open = provider?.id === "custom";
  clearValidation($("account-form"));
  $("account-error").textContent = "";
  $("unsupported-pane").textContent = t(provider?.note || "") || t("此服務目前無法直接連接。");
}
function openAccount() {
  if (!state.providers.length) { notify(t("郵箱服務清單尚未載入，請重新整理頁面。")); return; }
  $("account-form").reset();
  clearSecret("account-password");
  state.accountDirty = false; state.accountBusy = false; state.pendingDiscard = null;
  state.detectedImapAddress = "";
  $("discard-account").hidden = true;
  $("provider-select").value = (state.providers.find((item) => item.id === "custom") || state.providers[0]).id;
  configureProvider();
  openDialog($("account-dialog"), $("provider-select"));
}
function changeProvider(nextId) {
  const select = $("provider-select");
  const current = state.providers.find((item) => item.id === state.selectedProvider);
  const next = state.providers.find((item) => item.id === nextId);
  if (!next || state.accountBusy) return;
  cancelOAuthStart();
  if (next.kind === "oauth") {
    select.value = state.selectedProvider;
    guardDiscard(() => chooseOAuthMailbox(next.id));
    return;
  }
  if (current?.kind === "imap" && next?.kind === "imap") {
    state.detectedImapAddress = null;
    clearSecret("account-password");
    state.pendingDiscard = null;
    $("discard-account").hidden = true;
    state.accountDirty = ["account-email", "account-label", "account-username", "account-folder"].some((id) => $(id).value !== $(id).defaultValue);
    select.value = nextId;
    configureProvider();
    return;
  }
  select.value = state.selectedProvider;
  guardDiscard(() => { state.detectedImapAddress = null; clearSecret("account-password"); select.value = nextId; configureProvider(); });
}
function chooseOAuthMailbox(providerId) {
  state.accountDirty = false;
  $("account-form").reset();
  clearSecret("account-password");
  if (isAdmin()) {
    closeDialog($("account-dialog"));
    openProviderSettings(providerId);
  } else {
    $("provider-select").value = providerId;
    configureProvider();
    ($("mailbox-oauth-login").disabled ? $("provider-select") : $("mailbox-oauth-login")).focus();
  }
}
function applyImapAddressPreset(email) {
  // A manual provider or host selection takes precedence for the rest of this draft.
  if (state.detectedImapAddress === null ||
      (!state.detectedImapAddress && (state.selectedProvider !== "custom" || $("account-host").value.trim() || $("account-smtp-host").value.trim()))) return;
  const address = email.toLowerCase();
  const domain = /^[^@\s]+@([^@\s]+)$/.exec(address)?.[1];
  const preset = domain && state.providers.find((item) => item.email_domain === domain);
  if (!preset && !state.detectedImapAddress) return;
  if (state.detectedImapAddress && state.detectedImapAddress !== address) clearSecret("account-password");
  state.detectedImapAddress = preset ? address : "";
  const nextId = preset?.id || "custom";
  if (nextId !== state.selectedProvider) {
    $("provider-select").value = nextId;
    configureProvider();
  }
}
function routeMailboxAddress() {
  if (state.accountBusy || state.pendingDiscard) return false;
  const email = $("account-email").value.trim();
  applyImapAddressPreset(email);
  if (state.selectedProvider !== "custom") return false;
  const providerId = /^[^@\s]+@(?:gmail|googlemail)\.com$/i.test(email) ? "gmail" :
    /^[^@\s]+@(?:outlook|hotmail|live|msn)\.com$/i.test(email) ? "microsoft" : null;
  if (!providerId || !state.providers.some((provider) => provider.id === providerId)) return false;
  // A newly entered address needs no discard prompt. Protect any other IMAP draft fields.
  state.accountDirty = $("account-smtp-port").value !== "465" || ["account-label", "account-password", "account-host", "account-smtp-host", "account-username", "account-folder"]
    .some((id) => $(id).value !== $(id).defaultValue);
  guardDiscard(() => chooseOAuthMailbox(providerId));
  return true;
}
function guardDiscard(action) {
  if (state.accountBusy) return;
  if (!state.accountDirty) { action(); return; }
  state.pendingDiscard = action;
  $("discard-account").hidden = false;
  $("keep-account").focus();
}
function closeAccount() {
  cancelOAuthStart();
  guardDiscard(() => { closeDialog($("account-dialog")); $("account-form").reset(); state.accountDirty = false; });
}
function askConfirmation(title, description, label, action) {
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-action").textContent = label;
  $("confirm-error").textContent = "";
  state.confirmTask = action;
  openDialog($("confirm-dialog"), $("confirm-cancel"));
}
function allowsReply(scope) {
  return (scope || "").split(/\s+/).includes("mail.reply");
}
function accessLabel(scope) {
  return allowsReply(scope) ? t("讀取與回覆") : t("唯讀");
}
const accessLists = {
  tokens: {
    path: "/api/tokens", nameField: "name", createdLabel: "建立於 ",
    loading: "正在載入金鑰…", empty: "尚未建立存取金鑰。",
    revoke: "撤銷", title: "撤銷存取金鑰", confirm: "撤銷金鑰", success: "已撤銷存取金鑰。",
    consequence: "撤銷「{0}」後，使用這組金鑰的 AI 將無法讀取郵件。你可以另外建立一組金鑰。",
  },
  grants: {
    path: "/api/oauth/grants", nameField: "client_name", createdLabel: "授權於 ",
    loading: "正在載入應用程式授權…", empty: "尚未授權任何應用程式。先在 AI 工具新增 MCP 位址並啟動登入。",
    revoke: "撤銷授權", title: "撤銷應用程式授權", confirm: "撤銷授權", success: "已撤銷應用程式授權。",
    consequence: "「{0}」將無法再使用這次授權讀取任何郵箱快取，其更新憑證也會失效。其他獨立授權不受影響。",
  },
};

async function loadAccessList(kind) {
  const view = accessLists[kind];
  const epoch = ++state.accessEpoch[kind];
  const list = $(kind + "-list");
  if (!list.children.length) list.replaceChildren(node("p", "token-empty", t(view.loading)));
  try {
    const data = await api(view.path);
    if (epoch !== state.accessEpoch[kind] || !state.authenticated) return;
    const entries = data[kind];
    list.replaceChildren();
    if (!entries.length) list.append(node("p", "token-empty", t(view.empty)));
    for (const entry of entries) {
      const name = entry[view.nameField];
      const item = node("div", "token-item");
      const info = node("div");
      info.append(node("strong", "", name), node("p", "", t(view.createdLabel) + date(entry.created_at, true)),
        node("p", "", entry.last_used_at ? t("上次使用 ") + date(entry.last_used_at, true) : t("尚未使用")),
        node("p", "", accessLabel(entry.scope)));
      const revoke = node("button", "button danger-text small", t(view.revoke));
      revoke.type = "button";
      revoke.addEventListener("click", () => askConfirmation(t(view.title), t(view.consequence, name), t(view.confirm), async () => {
        await api(`${view.path}/${encodeURIComponent(entry.id)}`, { method: "DELETE" });
        await loadAccessList(kind);
        await loadSetup();
        notify(t(view.success));
      }));
      item.append(info, revoke);
      list.append(item);
    }
  } catch (error) {
    if (epoch !== state.accessEpoch[kind] || !state.authenticated) return;
    const retry = node("button", "button outline small", t("重新載入"));
    retry.type = "button";
    retry.addEventListener("click", () => void loadAccessList(kind));
    list.replaceChildren(node("p", "error-line", errorText(error)), retry);
  }
}
async function copyText(value, input, status) {
  try {
    await navigator.clipboard.writeText(value);
    if (status) status.textContent = t("已複製，請存放在安全的位置。");
    else notify(t("已複製。"));
  } catch {
    input?.focus(); input?.select();
    const message = t("瀏覽器無法自動複製，請選取內容後手動複製。");
    if (status) status.textContent = message; else notify(message);
  }
}
function closeSecret() {
  clearSecret("new-token");
  state.oneTimeSecret = false;
  closeDialog($("secret-dialog"));
}

async function loadSetup() {
  const epoch = ++state.setupEpoch;
  try {
    const setup = await api("/api/setup");
    if (epoch !== state.setupEpoch || !state.authenticated) return;
    if (!Array.isArray(setup.steps)) throw new Error(t("無法取得設定步驟，請重新整理。"));
    $("setup-panel").hidden = setup.ready === true;
    const list = $("setup-steps"); list.replaceChildren();
    const complete = setup.steps.filter((step) => step.complete).length;
    $("setup-progress").textContent = t("{0} / {1} 已完成", complete, setup.steps.length);
    setup.steps.forEach((step, index) => {
      const row = node("div", "setup-step" + (step.complete ? " complete" : ""));
      const marker = node("span", "step-marker", step.complete ? "✓" : String(index + 1));
      marker.setAttribute("aria-label", step.complete ? t("已完成") : t("待完成"));
      const detail = step.id === "mailbox" && Number.isInteger(setup.counts?.accounts)
        ? t("已連接 {0} 個郵箱。", setup.counts.accounts) : t(step.detail);
      const text = node("div");
      text.append(node("strong", "", t(step.title)), node("p", "", detail));
      row.append(marker, text); list.append(row);
    });
  } catch (error) {
    if (epoch !== state.setupEpoch || !state.authenticated) return;
    if (!state.accounts.length) {
      $("setup-panel").hidden = false;
      $("setup-progress").textContent = t("尚未取得狀態");
      const retry = node("button", "button outline small", t("重新取得狀態")); retry.type = "button";
      retry.addEventListener("click", () => void loadSetup());
      $("setup-steps").replaceChildren(node("p", "error-line", errorText(error)), retry);
    }
  }
}

const settingsCategories = ["sync", "accounts", "display", "mcp", "service"];

function updateSettingsSaveRow() {
  $("settings-save-row").hidden = !state.settingsDirty && !["sync", "accounts"].includes(state.settingsCategory);
}
function selectSettingsCategory(category) {
  if (!isAdmin() && !["display", "mcp"].includes(category)) return;
  if (state.settingsCategory === "accounts" && category !== "accounts") cancelOAuthStart();
  state.settingsCategory = category;
  for (const name of settingsCategories) {
    $("group-" + name).hidden = name !== category;
    $("category-" + name).setAttribute("aria-pressed", String(name === category));
  }
  updateSettingsSaveRow();
}
function selectSettingsProvider(service) {
  if ($(service + "-settings").hidden) cancelOAuthStart();
  for (const name of ["google", "microsoft"]) {
    $(name + "-settings").hidden = name !== service;
    $("select-" + name).setAttribute("aria-pressed", String(name === service));
  }
}
function navigateToSettings(category, focusId) {
  selectSettingsCategory(category);
  if (state.page === "settings" && location.hash === "#settings") {
    $(focusId).focus();
  } else {
    state.settingsFocus = focusId;
    location.hash = "settings";
  }
}
function openProviderSettings(providerId) {
  const service = providerId === "gmail" ? "google" : "microsoft";
  $(service + "-allow-reply").checked = false;
  selectSettingsProvider(service);
  navigateToSettings("accounts", service + "-settings-title");
}
function revealSettingsField(field) {
  if (field.form !== $("settings-form")) return;
  if (field.closest("#group-sync")) selectSettingsCategory("sync");
  else {
    selectSettingsCategory("accounts");
    selectSettingsProvider(field.closest("#google-settings") ? "google" : "microsoft");
  }
}
function refreshSettingsLoginButtons() {
  for (const service of ["google", "microsoft"]) {
    const button = $(service + "-login");
    const settings = state.settings?.[service];
    const configured = Boolean(settings?.client_id && settings.secret_configured);
    button.disabled = !state.authenticated || !configured || state.settingsDirty || state.settingsBusy || state.settingsLoading || Boolean(state.oauthStart);
    $(service + "-allow-reply").disabled = button.disabled;
    let hint = "";
    if (state.settingsDirty) hint = t("請先儲存或捨棄設定變更。");
    else if (!configured && !state.settingsLoading) hint = t("填入 Client ID 和 Client Secret 並儲存後，即可登入。");
    $(service + "-login-hint").textContent = hint;
  }
}
function renderSettings(settings) {
  state.settings = settings;
  $("sync-interval").value = String(settings.sync_interval_seconds);
  $("sync-days").value = String(settings.sync_days);
  $("sync-limit").value = String(settings.sync_limit);
  $("public-origin").value = settings.public_url;
  for (const service of ["google", "microsoft"]) {
    const provider = settings[service];
    $(service + "-client-id").value = provider.client_id || "";
    clearSecret(service + "-client-secret");
    $(service + "-client-secret").disabled = false;
    document.querySelector(`[data-reveal="${service}-client-secret"]`).disabled = false;
    $("clear-" + service + "-secret").checked = false;
    $("clear-" + service + "-secret").disabled = !provider.secret_configured;
    $(service + "-redirect").value = provider.redirect_uri;
    $(service + "-configured").textContent = provider.secret_configured ? t("已儲存 Secret") : t("尚未設定 Secret");
    $(service + "-configured").className = "tag" + (provider.secret_configured ? "" : " neutral");
  }
  clearValidation($("settings-form"));
  state.settingsDirty = false;
  $("settings-dirty").textContent = t("尚未變更設定。");
  $("settings-fields").disabled = false;
  $("settings-status").hidden = true;
  $("retry-settings").hidden = true;
  $("settings-error").textContent = "";
  updateSettingsSaveRow();
  refreshSettingsLoginButtons();
}
function markSettingsDirty(event) {
  if (event?.target?.hasAttribute("data-oauth-access")) return;
  state.settingsDirty = true;
  cancelOAuthStart();
  updateSettingsSaveRow();
  refreshSettingsLoginButtons();
  $("settings-dirty").textContent = t("有尚未儲存的變更。");
}
function discardSettings() {
  if (state.settings) renderSettings(state.settings);
  else {
    clearSecret("google-client-secret"); clearSecret("microsoft-client-secret");
    state.settingsDirty = false;
  }
}
async function loadSettings() {
  if (!isAdmin() || state.settingsDirty || state.settingsBusy) return;
  const epoch = ++state.settingsEpoch;
  state.settingsLoading = true;
  refreshSettingsLoginButtons();
  $("settings-status").textContent = t("正在載入設定…");
  $("settings-status").className = "banner";
  $("settings-status").hidden = false;
  $("retry-settings").hidden = true;
  if (!state.settings) $("settings-fields").disabled = true;
  try {
    const settings = await api("/api/settings");
    if (epoch !== state.settingsEpoch || !state.authenticated || state.settingsDirty) return;
    renderSettings(settings);
  } catch (error) {
    if (epoch !== state.settingsEpoch || !state.authenticated) return;
    $("settings-status").textContent = errorText(error);
    $("settings-status").className = "banner error";
    $("retry-settings").hidden = false;
  } finally {
    if (epoch === state.settingsEpoch) {
      state.settingsLoading = false;
      refreshSettingsLoginButtons();
    }
  }
}
function collectSettings() {
  const body = {
    sync_interval_seconds: Number($("sync-interval").value),
    sync_days: Number($("sync-days").value), sync_limit: Number($("sync-limit").value),
  };
  for (const provider of ["google", "microsoft"]) {
    body[provider + "_client_id"] = $(provider + "-client-id").value.trim();
    const secret = $(provider + "-client-secret").value;
    const clear = $("clear-" + provider + "-secret").checked;
    // Blank fields never replace a stored secret; explicit clear is the only deletion path.
    if (clear) body["clear_" + provider + "_secret"] = true;
    else if (secret) body[provider + "_client_secret"] = secret;
  }
  return body;
}
async function saveSettings(event) {
  event.preventDefault();
  if (!isAdmin() || state.settingsBusy || !validateForm($("settings-form"))) return;
  for (const provider of ["google", "microsoft"]) {
    const previous = state.settings?.[provider];
    const newId = $(provider + "-client-id").value.trim();
    if (previous?.secret_configured && newId && newId !== previous.client_id && !$(provider + "-client-secret").value.trim() && !$("clear-" + provider + "-secret").checked) {
      revealSettingsField($(provider + "-client-secret"));
      setFieldError($(provider + "-client-secret"), t("變更 Client ID 時，請一併填入新的 Secret，或勾選清除原值。"));
      $(provider + "-client-secret").focus(); return;
    }
  }
  const body = collectSettings();
  const button = $("save-settings");
  // A reload started before this edit may still return the previous settings.
  state.settingsEpoch++;
  state.settingsLoading = false;
  $("settings-status").hidden = true;
  $("retry-settings").hidden = true;
  state.settingsBusy = true; setBusy(button, true); $("settings-fields").disabled = true;
  refreshSettingsLoginButtons();
  $("settings-error").textContent = "";
  try {
    const settings = await api("/api/settings", { method: "POST", body });
    if (!state.authenticated) return;
    renderSettings(settings);
    state.session = { ...state.session, public_url: settings.public_url, sync_interval_seconds: settings.sync_interval_seconds, sync_days: settings.sync_days, sync_limit: settings.sync_limit };
    updateCoverage(); updateConnectionConfig();
    notify(t("設定已儲存，會套用到下一次同步。"));
    await loadSetup();
    try {
      const { providers } = await api("/api/providers");
      if (state.authenticated) { state.providers = providers; renderProviders(); }
    } catch {
      if (state.authenticated) { $("settings-status").textContent = t("設定已儲存，但服務清單尚未更新。連接郵箱前請重新整理頁面。"); $("settings-status").hidden = false; }
    }
  } catch (error) {
    if (!state.authenticated) return;
    $("settings-error").textContent = errorText(error);
    if (error.uncertain) $("settings-error").textContent += t(" 先捨棄本機表單並重新載入，即可確認伺服器目前的設定。");
  } finally {
    state.settingsBusy = false; setBusy(button, false);
    $("settings-fields").disabled = !state.authenticated;
    refreshSettingsLoginButtons();
  }
}
function renderDiagnostics(result) {
  const list = $("diagnostics-results");
  list.replaceChildren();
  for (const check of result.checks) {
    const row = node("div", "diagnostic-item");
    const labels = { pass: ["", t("通過")], warning: ["pending", t("提醒")], fail: ["error", t("未通過")] };
    const label = labels[check.status] || ["neutral", t("未知")];
    const isService = ["database", "origin", "google", "microsoft"].includes(check.id);
    let detail = t(check.detail);
    if (check.id === "origin") detail = t("{0}；此檢查未驗證外部 DNS 或 TLS 憑證。", state.session.public_url);
    else if (!isService && check.detail.startsWith("上次同步：")) {
      detail = t("上次同步：{0}", date(check.detail.slice("上次同步：".length), true));
    }
    const info = node("div"); info.append(node("strong", "", isService ? t(check.name) : check.name), node("p", "", detail));
    row.append(node("span", "tag " + label[0], label[1]), info); list.append(row);
  }
  list.append(node("p", "footnote", t("檢查時間：") + date(result.checked_at, true) + " · Europe/Belgrade"));
}
async function runDiagnostics() {
  const button = $("run-diagnostics"); if (button.disabled) return;
  setBusy(button, true);
  const list = $("diagnostics-results");
  list.replaceChildren(node("p", "small muted", t("正在檢查服務設定與資料庫…")));
  try {
    const result = await api("/api/diagnostics", { method: "POST" });
    if (!state.authenticated) return;
    state.diagnostics = result;
    renderDiagnostics(result);
  } catch (error) { if (state.authenticated) list.replaceChildren(node("p", "error-line", errorText(error))); }
  finally { setBusy(button, false); }
}
function safeOAuthRedirect(value) {
  try {
    const url = new URL(value);
    if (url.username || url.password || url.hash) return null;
    if (url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(url.hostname))) return url.href;
    return null;
  } catch { return null; }
}
async function loadConsent() {
  if (!state.oauthRequest || !state.authenticated || state.consentBusy) return;
  const epoch = ++state.consentEpoch;
  $("pending-consent").hidden = false;
  $("consent-error").textContent = "";
  $("consent-details").replaceChildren(node("dt", "", t("狀態")), node("dd", "", t("正在讀取授權要求…")));
  $("consent-expiry").textContent = "";
  $("consent-permissions").hidden = true;
  $("consent-approve").disabled = true; $("consent-deny").disabled = true;
  openDialog($("consent-dialog"), $("consent-later"));
  try {
    const data = await api(`/api/oauth/consent?${new URLSearchParams({ request_id: state.oauthRequest })}`);
    if (epoch !== state.consentEpoch || !state.authenticated) return;
    if (data.request_id !== state.oauthRequest) throw new Error(t("授權要求不一致，請由 AI 工具重新發起連線。"));
    state.consentData = data;
    const list = $("consent-details"); list.replaceChildren();
    for (const [label, value] of [[t("應用程式"), data.client_name], [t("完成後返回"), data.redirect_uri], [t("郵件服務"), data.resource], [t("授權範圍"), data.scope]]) list.append(node("dt", "", label), node("dd", "", value));
    $("consent-permissions").textContent = allowsReply(data.scope)
      ? t("此應用程式可讀取所有目前及日後連接的郵箱，並以這些郵箱的名義傳送郵件回覆。")
      : t("此應用程式可讀取所有目前及日後連接的郵箱快取。");
    $("consent-permissions").hidden = false;
    $("consent-approve").textContent = allowsReply(data.scope) ? t("允許讀取與回覆") : t("允許唯讀存取");
    $("consent-expiry").textContent = t("有效至 ") + date(data.expires_at, true) + " · Europe/Belgrade";
    const expired = !Number.isFinite(new Date(data.expires_at).getTime()) || new Date(data.expires_at).getTime() <= Date.now();
    $("consent-approve").disabled = expired; $("consent-deny").disabled = expired;
    if (expired) $("consent-error").textContent = t("授權要求已逾時，請回到 AI 工具重新發起登入。");
  } catch (error) {
    if (epoch !== state.consentEpoch || !state.authenticated) return;
    state.consentData = null;
    $("consent-error").textContent = errorText(error) + t(" 若已重新登入或要求逾時，請由 AI 工具重新發起連線。");
  }
}
async function resolveConsent(approve) {
  if (state.consentBusy || !state.consentData) return;
  if (new Date(state.consentData.expires_at).getTime() <= Date.now()) {
    $("consent-error").textContent = t("授權要求已逾時，請回到 AI 工具重新發起登入。");
    $("consent-approve").disabled = true; $("consent-deny").disabled = true; return;
  }
  state.consentBusy = true;
  $("consent-approve").disabled = true; $("consent-deny").disabled = true; $("consent-later").disabled = true;
  const button = approve ? $("consent-approve") : $("consent-deny"); setBusy(button, true);
  $("consent-error").textContent = "";
  try {
    const { redirect_url } = await api("/api/oauth/consent", { method: "POST", body: { request_id: state.oauthRequest, approve } });
    if (!state.authenticated) return;
    const target = safeOAuthRedirect(redirect_url);
    if (!target) throw new Error(t("服務回傳的重新導向網址不受支援，請從 AI 工具重新發起連線。"));
    state.oauthRequest = ""; state.consentData = null;
    const params = new URLSearchParams(location.search); params.delete("oauth_request");
    history.replaceState(null, "", location.pathname + (params.size ? "?" + params.toString() : "") + location.hash);
    $("pending-consent").hidden = true;
    closeDialog($("consent-dialog"));
    location.assign(target);
  } catch (error) {
    if (state.authenticated) $("consent-error").textContent = errorText(error);
  } finally {
    state.consentBusy = false; setBusy(button, false);
    $("consent-later").disabled = false;
    $("consent-approve").disabled = !state.consentData;
    $("consent-deny").disabled = !state.consentData;
  }
}

function renderAuth() {
  const registering = state.registering;
  $("login-username").required = registering;
  $("login-password").autocomplete = registering ? "new-password" : "current-password";
  $("auth-heading").textContent = registering ? t("註冊新帳號") : t("開啟工作台");
  $("auth-switch").textContent = registering ? t("已有帳號？登入") : t("註冊新帳號");
  $("auth-submit").textContent = registering ? t("建立帳號") : t("登入工作台");
  $("username-hint").textContent = registering
    ? t("使用者名稱須為 3–64 個字元，以英文字母或數字開頭，可包含點、底線及連字號。")
    : t("使用部署管理密碼登入時，可留空使用者名稱。");
  $("password-hint").hidden = !registering;
  document.title = registering ? t("註冊新帳號") + " · MailHarbor" : t("登入 · MailHarbor");
}
$("auth-switch").addEventListener("click", () => {
  state.registering = !state.registering;
  clearSecret("login-password");
  clearValidation($("login-form"));
  $("login-error").textContent = "";
  renderAuth();
  $("login-username").focus();
});
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.isComposing || !validateForm(event.currentTarget)) return;
  const button = event.currentTarget.querySelector('[type="submit"]');
  if (button.disabled) return;
  setBusy(button, true); $("auth-switch").disabled = true; $("login-error").textContent = "";
  const registering = state.registering;
  const username = $("login-username").value.trim();
  const body = { password: $("login-password").value };
  if (username || registering) body.username = username;
  try {
    const result = await api(registering ? "/api/register" : "/api/login", { method: "POST", body });
    sessionChanges?.postMessage("changed");
    state.csrf = result.csrf_token; await enterApp();
  } catch (error) {
    if (error.status === 409 && registering) {
      setFieldError($("login-username"), errorText(error)); $("login-username").focus();
    } else {
      $("login-error").textContent = registering && error.uncertain
        ? t("無法確認帳號是否建立。請先嘗試用這組使用者名稱和密碼登入。") : errorText(error);
      $("login-password").focus();
    }
  } finally { setBusy(button, false); $("auth-switch").disabled = false; }
});
async function performLogout() {
  cancelOAuthStart();
  const button = $("logout"); button.disabled = true;
  try { await api("/api/logout", { method: "POST" }); expireSession(""); sessionChanges?.postMessage("changed"); }
  catch (error) { notify(errorText(error)); }
  finally { button.disabled = false; }
}
$("logout").addEventListener("click", () => {
  if (state.settingsBusy) { notify(t("正在儲存設定，請稍候。")); return; }
  if (state.settingsDirty) askConfirmation(t("捨棄未儲存的設定並登出？"), t("尚未儲存的同步設定與授權資料會清除。已儲存在伺服器的設定不受影響。"), t("捨棄變更並登出"), async () => { discardSettings(); await performLogout(); });
  else void performLogout();
});
for (const button of document.querySelectorAll("[data-reveal]")) button.addEventListener("click", () => {
  const id = button.dataset.reveal;
  setSecretVisibility(id, $(id).type === "password");
});
for (const button of document.querySelectorAll("[data-open-account]")) button.addEventListener("click", openAccount);
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); if (!state.composing) searchNow(); });
$("mail-search").addEventListener("compositionstart", () => { state.composing = true; clearTimeout(state.searchTimer); });
$("mail-search").addEventListener("compositionend", () => { state.composing = false; scheduleSearch(); });
$("mail-search").addEventListener("input", scheduleSearch);
$("clear-search").addEventListener("click", clearSearch);
$("account-filter").addEventListener("change", () => { state.accountId = $("account-filter").value; state.offset = 0; void loadMessages(); });
$("refresh-messages").addEventListener("click", () => { void loadAccounts(); void loadMessages(); });
$("prev-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); void loadMessages(); $("mail-list").scrollTop = 0; });
$("next-page").addEventListener("click", () => { if (state.offset + state.limit < state.total) { state.offset += state.limit; void loadMessages(); $("mail-list").scrollTop = 0; } });
$("close-reader").addEventListener("click", closeReader);
$("reader-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeReader(); });
$("open-reply").addEventListener("click", () => {
  if (!state.reply) return;
  $("open-reply").hidden = true;
  $("open-reply").setAttribute("aria-expanded", "true");
  $("reply-form").hidden = false;
  $("reply-body").focus();
});
$("cancel-reply").addEventListener("click", () => discardReply(() => {
  $("reply-form").hidden = true;
  $("reply-body").value = "";
  $("reply-body").readOnly = false;
  $("reply-error").textContent = "";
  setFieldError($("reply-body"), "");
  state.reply.requestId = null;
  $("open-reply").hidden = false;
  $("open-reply").setAttribute("aria-expanded", "false");
  $("open-reply").focus();
}));
$("reply-form").addEventListener("submit", sendReply);
$("close-account").addEventListener("click", closeAccount);
$("cancel-account").addEventListener("click", closeAccount);
$("account-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeAccount(); });
$("account-form").addEventListener("input", () => { state.accountDirty = true; });
$("keep-account").addEventListener("click", () => { state.pendingDiscard = null; $("discard-account").hidden = true; $("account-email").focus(); });
$("discard-account-confirm").addEventListener("click", () => {
  const action = state.pendingDiscard; state.pendingDiscard = null; state.accountDirty = false;
  $("account-form").reset(); $("discard-account").hidden = true; action?.();
});
$("provider-select").addEventListener("change", () => changeProvider($("provider-select").value));
$("account-email").addEventListener("change", routeMailboxAddress);
$("use-custom-imap").addEventListener("click", () => {
  changeProvider("custom");
  $("account-host").focus();
});
$("account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.pendingDiscard) { $("keep-account").focus(); return; }
  if (state.accountBusy || routeMailboxAddress() || !validateForm(event.currentTarget)) return;
  const form = event.currentTarget; const button = form.querySelector('[type="submit"]');
  state.accountBusy = true; setBusy(button, true); $("provider-select").disabled = true;
  $("account-fields").disabled = true; $("close-account").disabled = true;
  $("account-error").textContent = "";
  const body = {
    provider: state.selectedProvider, email: $("account-email").value.trim(), label: $("account-label").value.trim(),
    username: $("account-username").value.trim(), password: $("account-password").value,
    host: $("account-host").value.trim(), port: Number($("account-port").value), folder: $("account-folder").value.trim(),
  };
  if (state.selectedProvider === "custom" && $("account-smtp-host").value.trim()) {
    body.smtp_host = $("account-smtp-host").value.trim();
    body.smtp_port = Number($("account-smtp-port").value);
  }
  try {
    await api("/api/accounts/imap", { method: "POST", body, timeout: 90000 });
    if (!state.authenticated) return;
  } catch (error) {
    $("account-error").textContent = errorText(error);
    if (error.uncertain) void loadAccounts();
    return;
  } finally {
    state.accountBusy = false; setBusy(button, false); $("provider-select").disabled = false;
    $("account-fields").disabled = false; $("close-account").disabled = false;
  }
  state.accountDirty = false; form.reset(); closeDialog($("account-dialog"));
  notify(t("已連接郵箱，正在準備同步。"));
  await loadAccounts(); await loadSetup(); if (state.page === "inbox") await loadMessages();
});
async function startProviderLogin(providerId, button, errorTarget) {
  const fromDialog = button === $("mailbox-oauth-login");
  const stillHere = () => fromDialog
    ? $("account-dialog").open && state.selectedProvider === providerId
    : state.page === "settings" && location.hash === "#settings";
  if (button.disabled || state.oauthStart || state.settingsDirty || state.settingsBusy || !stillHere()) return;
  const controller = new AbortController();
  const permission = $(fromDialog ? "mailbox-allow-reply" : (providerId === "gmail" ? "google" : "microsoft") + "-allow-reply");
  const allowReply = permission.checked === true;
  state.oauthStart = {controller, button, permission};
  permission.disabled = true;
  setBusy(button, true);
  refreshSettingsLoginButtons();
  errorTarget.textContent = "";
  try {
    const { url } = await api(`/api/oauth/${encodeURIComponent(providerId)}/start`, {
      method: "POST", body: {allow_reply: allowReply}, signal: controller.signal,
    });
    if (controller.signal.aborted || !state.authenticated || !stillHere()) return;
    if (!safeURL(url)) throw new Error(t("授權網址無效，請檢查伺服器設定。"));
    location.assign(url);
  } catch (error) {
    if (!controller.signal.aborted) errorTarget.textContent = errorText(error);
  } finally {
    if (state.oauthStart?.controller === controller) {
      state.oauthStart = null;
      setBusy(button, false);
      permission.disabled = false;
      refreshSettingsLoginButtons();
    }
  }
}
$("mailbox-oauth-login").addEventListener("click", () => {
  void startProviderLogin(state.selectedProvider, $("mailbox-oauth-login"), $("mailbox-oauth-error"));
});
$("confirm-cancel").addEventListener("click", () => { if (!$("confirm-action").disabled) closeDialog($("confirm-dialog")); });
$("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); if (!$("confirm-action").disabled) closeDialog($("confirm-dialog")); });
$("confirm-action").addEventListener("click", async () => {
  const button = $("confirm-action"); if (button.disabled || !state.confirmTask) return;
  setBusy(button, true); $("confirm-cancel").disabled = true; $("confirm-error").textContent = "";
  try { await state.confirmTask(); closeDialog($("confirm-dialog")); }
  catch (error) { $("confirm-error").textContent = errorText(error); if (error.uncertain) { await loadAccounts(); await loadAccessList("tokens"); await loadAccessList("grants"); } }
  finally { setBusy(button, false); $("confirm-cancel").disabled = false; }
});
$("token-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!validateForm(event.currentTarget)) return;
  const button = event.currentTarget.querySelector('[type="submit"]'); if (button.disabled) return;
  setBusy(button, true); $("token-error").textContent = "";
  try {
    const result = await api("/api/tokens", { method: "POST", body: { name: $("token-name").value.trim(), allow_reply: $("token-allow-reply").checked } });
    if (!state.authenticated) return;
    $("token-name").value = ""; $("token-allow-reply").checked = false;
    clearSecret("new-token");
    $("new-token").value = result.token;
    state.oneTimeSecret = true; $("copy-token-status").textContent = "";
    openDialog($("secret-dialog"), $("copy-token")); await loadAccessList("tokens"); await loadSetup();
  } catch (error) { $("token-error").textContent = errorText(error); if (error.uncertain) await loadAccessList("tokens"); }
  finally { setBusy(button, false); }
});
$("copy-token").addEventListener("click", () => void copyText($("new-token").value, $("new-token"), $("copy-token-status")));
$("close-secret").addEventListener("click", closeSecret);
$("saved-token").addEventListener("click", closeSecret);
$("secret-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeSecret(); });
$("copy-endpoint").addEventListener("click", () => void copyText($("endpoint").value, $("endpoint")));
$("copy-config").addEventListener("click", () => void copyText($("codex-config").textContent));
$("ai-client").addEventListener("change", updateConnectionConfig);
$("ai-auth").addEventListener("change", updateConnectionConfig);
for (const category of settingsCategories) {
  $("category-" + category).addEventListener("click", () => selectSettingsCategory(category));
}
$("open-mcp-guide").addEventListener("click", (event) => {
  event.preventDefault();
  navigateToSettings("mcp", "guide-title");
});
for (const service of ["google", "microsoft"]) {
  $("select-" + service).addEventListener("click", () => selectSettingsProvider(service));
  $(service + "-login").addEventListener("click", () =>
    startProviderLogin(service === "google" ? "gmail" : "microsoft", $(service + "-login"), $(service + "-login-error")));
}
$("settings-form").addEventListener("input", markSettingsDirty);
$("settings-form").addEventListener("change", markSettingsDirty);
$("settings-form").addEventListener("submit", saveSettings);
$("retry-settings").addEventListener("click", () => void loadSettings());
$("discard-settings").addEventListener("click", () => {
  if (state.settingsBusy) return;
  if (state.settingsDirty) askConfirmation(t("捨棄未儲存的設定？"), t("這會清除本機表單中的變更，並重新讀取伺服器已儲存的設定。"), t("捨棄並重新載入"), async () => { discardSettings(); await loadSettings(); });
  else void loadSettings();
});
$("run-diagnostics").addEventListener("click", () => void runDiagnostics());
$("refresh-grants").addEventListener("click", () => void loadAccessList("grants"));
$("review-consent").addEventListener("click", () => void loadConsent());
$("consent-approve").addEventListener("click", () => void resolveConsent(true));
$("consent-deny").addEventListener("click", () => void resolveConsent(false));
$("consent-later").addEventListener("click", () => { if (!state.consentBusy) closeDialog($("consent-dialog")); });
$("consent-dialog").addEventListener("cancel", (event) => { event.preventDefault(); if (!state.consentBusy) closeDialog($("consent-dialog")); });
for (const provider of ["google", "microsoft"]) {
  $("copy-" + provider + "-redirect").addEventListener("click", () => void copyText($(provider + "-redirect").value, $(provider + "-redirect")));
  $("clear-" + provider + "-secret").addEventListener("change", () => {
    const clear = $("clear-" + provider + "-secret").checked;
    const input = $(provider + "-client-secret");
    if (clear) clearSecret(provider + "-client-secret");
    input.disabled = clear;
    document.querySelector(`[data-reveal="${provider}-client-secret"]`).disabled = clear;
    markSettingsDirty();
  });
}
// Controls stay disabled until their handlers are bound (also safe when JS fails).
for (const button of document.querySelectorAll("[data-js-control]")) button.disabled = false;
// IME confirmation must never accidentally submit credential/token forms.
for (const form of document.querySelectorAll("form")) {
  form.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.isComposing || event.keyCode === 229)) event.preventDefault();
  });
}
window.addEventListener("hashchange", choosePage);
window.addEventListener("beforeunload", (event) => {
  if (state.accountDirty || state.oneTimeSecret || state.settingsDirty || $("reply-body").value) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("online", () => { if (state.authenticated) { void loadAccounts(); if (state.page === "inbox") void loadMessages(); } });
window.addEventListener("offline", () => { $("network-banner").textContent = t("目前離線。顯示的是上次取得的資料，操作不會排入離線佇列。"); $("network-banner").hidden = false; });
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.authenticated) { void loadAccounts(); if (state.page === "connect") { void loadAccessList("tokens"); void loadAccessList("grants"); } } });
setInterval(async () => {
  if (!state.authenticated || document.hidden || state.pollBusy || $("confirm-dialog").open) return;
  state.pollBusy = true;
  try { await loadAccounts(); } finally { state.pollBusy = false; }
}, 10000);

function refreshInterfaceLanguage(previous) {
  translateFeedback(previous);
  for (const id of Object.keys(secretLabels)) setSecretVisibility(id, $(id).type !== "password");
  if (!state.authenticated) {
    renderAuth();
    return;
  }
  // Only the login and settings pages expose a language selector. Keep unsaved form values intact.
  document.title = t("設定") + " · MailHarbor";
  updateConnectionConfig();
  renderProviders();
  renderAccounts();
  updateCoverage();
  if (state.settings) {
    for (const service of ["google", "microsoft"]) {
      $(service + "-configured").textContent = state.settings[service].secret_configured ? t("已儲存 Secret") : t("尚未設定 Secret");
    }
    $("settings-dirty").textContent = state.settingsDirty ? t("有尚未儲存的變更。") : t("尚未變更設定。");
    refreshSettingsLoginButtons();
  }
  if (state.diagnostics) renderDiagnostics(state.diagnostics);
  void loadAccounts();
  void loadSetup();
}
async function boot() {
  bindLanguageControls(refreshInterfaceLanguage);
  const params = new URLSearchParams(location.search);
  state.oauthRequest = params.get("oauth_request") || "";
  if (params.has("connected") || params.has("connection_error")) {
    $("callback-banner").textContent = params.has("connected") ? t("郵箱授權完成。可在「我的郵箱」查看同步狀態。") : t("郵箱授權未完成。請檢查服務商設定後重新連接。");
    $("callback-banner").className = "banner" + (params.has("connection_error") ? " error" : "");
    $("callback-banner").hidden = false;
    params.delete("connected"); params.delete("connection_error");
    history.replaceState(null, "", location.pathname + (params.size ? "?" + params.toString() : "") + location.hash);
  }
  try { await enterApp(); }
  catch (error) { expireSession(errorText(error)); }
}
void boot();
