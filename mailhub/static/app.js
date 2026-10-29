"use strict";

// All mailbox content is untrusted: build nodes with textContent, never HTML.
const $ = (id) => document.getElementById(id);
const state = {
  authenticated: false, csrf: "", session: {}, accounts: [], providers: [],
  page: "inbox", query: "", accountId: "", offset: 0, limit: 30, total: 0,
  searchEpoch: 0, accountEpoch: 0, tokenEpoch: 0, readerEpoch: 0,
  searchController: null, readerController: null, searchTimer: null, composing: false,
  accountDirty: false, accountBusy: false, pendingDiscard: null, selectedProvider: "",
  oneTimeSecret: false, confirmTask: null, triggerMap: new WeakMap(), liveTimer: null,
  pollBusy: false, lastSyncSignature: "", lastAccountRenderSignature: "", requests: new Set(),
  settings: null, settingsDirty: false, settingsBusy: false, settingsEpoch: 0, setupEpoch: 0,
  grantsEpoch: 0, setup: null, oauthRequest: "", consentData: null, consentBusy: false, consentEpoch: 0,
};
const dateFormat = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Europe/Belgrade", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const longDateFormat = new Intl.DateTimeFormat("zh-TW", {
  timeZone: "Europe/Belgrade", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const numberFormat = new Intl.NumberFormat("zh-TW");

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}
function date(value, long = false) {
  if (!value) return "尚未同步";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "時間不明" : (long ? longDateFormat : dateFormat).format(parsed);
}
function safeURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}
function providerName(id) {
  return state.providers.find((provider) => provider.id === id)?.name || id;
}
function snapshotRange(days, limit) {
  const range = Number.isInteger(days) && days > 0 ? `近 ${numberFormat.format(days)} 天` : "天數未記錄";
  const count = Number.isInteger(limit) && limit > 0 ? `最多 ${numberFormat.format(limit)} 封` : "封數未記錄";
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
  return error.message || "無法完成要求，請稍後重試。";
}
function setFieldError(input, message) {
  input.setAttribute("aria-invalid", message ? "true" : "false");
  const target = $(input.id + "-error");
  if (target) target.textContent = message;
}
function clearValidation(form) {
  for (const input of form.querySelectorAll("input")) setFieldError(input, "");
}
function validateForm(form) {
  clearValidation(form);
  let first = null;
  for (const input of form.querySelectorAll("input")) {
    let message = "";
    if (input.required && !input.value.trim()) message = "請填寫此欄位。";
    else if (input.type === "email" && input.value && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(input.value)) message = "請填寫完整的郵箱地址。";
    else if (input.type === "number" && (!Number.isInteger(Number(input.value)) || Number(input.value) < Number(input.min) || Number(input.value) > Number(input.max))) message = `請填寫 ${input.min}–${input.max} 之間的整數。`;
    else if (input.maxLength > 0 && input.value.length > input.maxLength) message = "內容過長，請縮短後重試。";
    setFieldError(input, message);
    if (message && !first) first = input;
  }
  if (first) {
    const details = first.closest("details");
    if (details) details.open = true;
    first.focus();
  }
  return !first;
}
function resetSecrets() {
  for (const id of ["login-password", "account-password", "new-token", "google-client-secret", "microsoft-client-secret"]) {
    $(id).value = "";
    $(id).type = "password";
    const button = document.querySelector(`[data-reveal="${id}"]`);
    button.textContent = "顯示";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", "顯示" + (id === "new-token" ? "存取金鑰" : "密碼"));
  }
  state.oneTimeSecret = false;
}
function openDialog(dialog, focusTarget) {
  state.triggerMap.set(dialog, document.activeElement);
  if (!dialog.open) dialog.showModal();
  (focusTarget || dialog.querySelector("button, input, select"))?.focus();
}
function closeDialog(dialog) {
  if (dialog.open) dialog.close();
  const trigger = state.triggerMap.get(dialog);
  if (trigger?.isConnected && !trigger.disabled) trigger.focus();
  else if (state.authenticated) $("main").focus();
}
function expireSession(message = "登入已逾時，請重新登入。") {
  state.authenticated = false;
  state.csrf = "";
  state.searchEpoch += 1;
  state.accountEpoch += 1;
  state.tokenEpoch += 1;
  state.readerEpoch += 1;
  clearTimeout(state.searchTimer);
  for (const controller of state.requests) controller.abort();
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  state.accountDirty = false;
  state.settingsDirty = false;
  state.settingsBusy = false;
  state.settings = null;
  state.setup = null;
  state.consentData = null;
  state.settingsEpoch += 1;
  state.setupEpoch += 1;
  state.grantsEpoch += 1;
  state.consentEpoch += 1;
  state.accounts = [];
  state.lastAccountRenderSignature = "";
  state.query = "";
  state.offset = 0;
  state.accountId = "";
  $("mail-search").value = "";
  $("clear-search").hidden = true;
  for (const id of ["mail-list", "accounts-list", "tokens-list", "reader-content", "grants-list", "setup-steps", "diagnostics-results", "consent-details"]) $(id).replaceChildren();
  $("settings-form").reset();
  $("settings-fields").disabled = true;
  resetSecrets();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("boot").hidden = true;
  $("login-error").textContent = message;
  document.title = "登入 · MailHarbor";
  $("login-password").focus();
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
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  try {
    const response = await fetch(path, {
      method, credentials: "same-origin", cache: "no-store", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: controller.signal,
    });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (csrfAtDispatch && !["/api/login", "/api/session"].includes(path) && state.csrf !== csrfAtDispatch) {
      const stale = new Error("登入狀態已變更，忽略舊要求的回應。"); stale.name = "AbortError"; throw stale;
    }
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/login") expireSession();
      const defaultMessage = response.status === 403 ? "沒有權限完成這項操作，請重新登入後重試。" : "無法完成要求，請稍後重試。";
      const detail = typeof data.detail === "string" ? data.detail : defaultMessage;
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    $("network-banner").hidden = true;
    return data;
  } catch (error) {
    if (controller.signal.aborted && !external?.aborted && controller.signal.reason === "timeout") {
      const timed = new Error(method === "GET" ? "伺服器回應逾時，請重新整理。" : "等待逾時，操作可能已完成。請先重新整理列表確認，再嘗試一次。");
      timed.uncertain = method !== "GET";
      throw timed;
    }
    if (error instanceof TypeError && !controller.signal.aborted) {
      $("network-banner").textContent = "暫時無法連接伺服器。現有資料可能不是最新狀態，請檢查網路後重新整理。";
      $("network-banner").hidden = false;
      const network = new Error(method === "GET" ? "無法連接伺服器，請檢查網路後重試。" : "連線中斷，操作結果尚未確認。請先重新整理列表確認。");
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
  $("coverage").textContent = `同步目標：近 ${numberFormat.format(days)} 天 · 每個郵箱最多 ${numberFormat.format(limit)} 封`;
  const seconds = state.session.sync_interval_seconds || 300;
  const interval = seconds % 60 === 0 ? `${seconds / 60} 分鐘` : `${seconds} 秒`;
  const failed = state.accounts.filter((account) => account.status === "error").length;
  const syncing = state.accounts.filter((account) => account.status === "syncing").length;
  const latest = state.accounts.map((account) => account.last_sync).filter(Boolean).sort().at(-1);
  const pieces = [`自動同步約每 ${interval}`, "各帳號設定的資料夾"];
  if (latest) pieces.push(`最近完成 ${date(latest)}`);
  if (syncing) pieces.push(`${syncing} 個同步中`);
  if (failed) pieces.push(`${failed} 個郵箱需要處理`);
  $("sync-summary").textContent = pieces.join(" · ") + "。目前快取依各郵箱上次同步；目標變更於下次成功同步後套用。";
}
function choosePage() {
  const desired = location.hash.slice(1);
  const destination = ["inbox", "accounts", "connect", "settings"].includes(desired) ? desired : "inbox";
  if (state.settingsBusy && state.page === "settings" && destination !== "settings") {
    history.replaceState(null, "", location.pathname + location.search + "#settings");
    notify("正在儲存設定，請稍候。"); return;
  }
  if (state.settingsDirty && state.page === "settings" && destination !== "settings") {
    history.replaceState(null, "", location.pathname + location.search + "#settings");
    askConfirmation("離開未儲存的設定？", "你填寫的同步設定與授權資料尚未儲存。離開會捨棄這些變更。", "捨棄變更並離開", async () => {
      discardSettings(); location.hash = destination;
    });
    return;
  }
  state.page = destination;
  for (const page of ["inbox", "accounts", "connect", "settings"]) $("page-" + page).hidden = page !== state.page;
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === state.page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const titles = { inbox: "統一收件匣", accounts: "我的郵箱", connect: "連接 AI", settings: "設定" };
  document.title = state.authenticated ? `${titles[state.page]} · MailHarbor` : "登入 · MailHarbor";
  if (state.authenticated) {
    if (state.page === "inbox") void loadMessages();
    if (state.page === "accounts") void loadAccounts();
    if (state.page === "connect") { void loadTokens(); void loadGrants(); }
    if (state.page === "settings" && !state.settingsDirty) void loadSettings();
  }
}
function updateConnectionConfig() {
  const origin = (state.session.public_url || location.origin).replace(/\/$/, "");
  const url = origin + "/mcp";
  const client = $("ai-client").value || "codex";
  const authSelect = $("ai-auth");
  if (["claude", "rest"].includes(client)) authSelect.value = "token";
  if (client === "oauth") authSelect.value = "oauth";
  authSelect.disabled = ["claude", "rest", "oauth"].includes(client);
  const token = authSelect.value === "token";
  let config, title, help, note;
  $("endpoint").value = client === "rest" ? origin + "/v1" : url;
  $("endpoint-label").textContent = client === "rest" ? "REST API 位址" : "MCP 伺服器位址";
  $("transport-label").textContent = client === "rest" ? "HTTPS · OpenAPI" : client === "claude" ? "stdio → HTTPS" : "Streamable HTTP";
  $("openapi-link").hidden = client !== "rest";
  $("openapi-link").href = origin + "/openapi.json";
  if (client === "codex") {
    title = "Codex · config.toml";
    config = `[mcp_servers.mailharbor]\nurl = ${JSON.stringify(url)}` + (token ? '\nbearer_token_env_var = "MAILHUB_MCP_TOKEN"' : "");
    help = token ? "將設定加入 Codex 的 config.toml，並在啟動 Codex 的環境中設定 MAILHUB_MCP_TOKEN。" : "將設定加入 Codex 的 config.toml，執行 codex mcp login mailharbor，然後在本工作台確認讀取授權。";
    note = token ? "在右側建立存取金鑰。設定範例只引用環境變數，不會包含金鑰。" : "OAuth 由 Codex 管理登入憑證；不需要把管理密碼或郵箱密碼填入 Codex。";
  } else if (client === "cursor") {
    title = "Cursor · mcp.json";
    const entry = { url };
    if (token) entry.headers = { Authorization: "Bearer ${env:MAILHUB_MCP_TOKEN}" };
    config = JSON.stringify({ mcpServers: { mailharbor: entry } }, null, 2);
    help = token ? "加入 Cursor 的 MCP 設定，並確保 Cursor 啟動環境能讀到 MAILHUB_MCP_TOKEN。" : "加入 Cursor 的 MCP 設定後，從 Cursor 啟動連線／登入，在此工作台確認授權。";
    note = token ? "環境變數插值會由 Cursor 處理；此範例不含實際金鑰。" : "實際授權完成前，新增設定不代表已成功連接。";
  } else if (client === "claude") {
    title = "Claude Desktop · 本機 stdio 設定";
    config = JSON.stringify({ mcpServers: { mailharbor: {
      command: "/path/to/MailHarbor/.venv/bin/python",
      args: ["/path/to/MailHarbor/scripts/stdio_bridge.py", "--url", url],
      env: { MAILHUB_MCP_TOKEN: "<在私人客戶端設定填入專用 token>" },
    } } }, null, 2);
    help = "在使用 Claude Desktop 的電腦安裝本專案與依賴，將兩個 /path/to 路徑換成該電腦的絕對路徑，再加入 Claude Desktop 的 MCP 設定。";
    note = "這是本機橋接器，不是 VPS 上的檔案路徑。token 欄位只是佔位字；填入專用金鑰，或改由安全啟動器注入 MAILHUB_MCP_TOKEN。不要分享填入金鑰後的設定。";
  } else if (client === "rest") {
    title = "REST 呼叫範例";
    config = `curl --fail-with-body \\\n  --header "Authorization: Bearer $MAILHUB_MCP_TOKEN" \\\n  "${origin}/v1/status"`;
    help = "供自訂 AI 工具或 API 用戶端使用。讀取 /openapi.json 的唯讀路由定義，並在 Authorization 標頭傳入 Bearer 金鑰。";
    note = "可用路由：GET /v1/accounts、/v1/messages、/v1/messages/{id}、/v1/status。搜尋與閱讀仍限於已同步快取。";
  } else {
    title = "OAuth MCP 連線資料";
    config = JSON.stringify({ mcp_url: url, authorization_server: origin, protected_resource_metadata: origin + "/.well-known/oauth-protected-resource/mcp", scope: "mail.read" }, null, 2);
    help = "在支援遠端 MCP 與 OAuth 的 AI 工具中，新增上方 MCP 位址並啟動登入。工具需支援動態註冊、授權碼流程與 S256 PKCE。";
    note = "這份資料供對照各平台的連線欄位，不是所有 AI 工具都接受的通用設定檔。登入後仍需在此工作台明確允許讀取。";
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
  $("login-view").hidden = true;
  $("boot").hidden = true;
  $("app-view").hidden = false;
  $("login-password").value = "";
  updateConnectionConfig();
  updateCoverage();
  const results = await Promise.allSettled([api("/api/providers"), loadAccounts()]);
  if (!state.authenticated) return;
  if (results[0].status === "fulfilled") {
    state.providers = results[0].value.providers;
    renderProviders();
    renderAccounts();
  } else {
    $("callback-banner").textContent = "無法取得郵箱服務清單，請重新整理頁面後再連接郵箱。";
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
    filter.replaceChildren(new Option("所有郵箱", ""));
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
    if (!state.accounts.length) emptyState($("accounts-list"), "無法載入郵箱", errorText(error), () => void loadAccounts(), "重新載入", "!");
  }
}
function renderAccounts() {
  const container = $("accounts-list");
  if (!state.accounts.length) {
    emptyState(container, "先連接第一個郵箱", "選擇郵箱服務完成授權。之後可以繼續加入其他服務或同一服務的不同帳號。", openAccount, "連接郵箱", "◫");
    return;
  }
  container.replaceChildren();
  for (const account of state.accounts) {
    const card = node("article", "account-card panel");
    const header = node("div", "account-card-header");
    const initials = providerName(account.provider).slice(0, 1).toUpperCase();
    const status = account.status === "error" ? ["error", "需要處理"] : account.status === "syncing" ? ["pending", "同步中"] : ["", account.last_sync ? "已連接" : "等待同步"];
    header.append(node("span", "provider-icon", initials), node("span", "tag " + status[0], status[1]));
    card.append(header, node("h2", "", account.label || account.email), node("p", "email", account.label ? account.email : providerName(account.provider)));
    const metadata = node("dl", "account-meta");
    for (const [label, value] of [["郵箱服務", providerName(account.provider)], ["快取郵件", `${numberFormat.format(account.message_count || 0)} 封`], ["同步資料夾", account.folder || "INBOX"], ["快取同步範圍", snapshotRange(account.snapshot_days, account.snapshot_limit)], ["上次同步", date(account.last_sync, true)]]) {
      const row = node("div"); row.append(node("dt", "", label), node("dd", "", value)); metadata.append(row);
    }
    card.append(metadata);
    if (account.error) card.append(node("p", "banner error", account.error));
    const actions = node("div", "account-actions");
    const sync = node("button", "button outline small", account.status === "syncing" ? "同步中" : "立即同步");
    sync.type = "button"; sync.disabled = account.status === "syncing";
    sync.addEventListener("click", () => void syncAccount(account, sync));
    const disconnect = node("button", "button danger-text small", "斷開連接");
    disconnect.type = "button";
    disconnect.addEventListener("click", () => askConfirmation("斷開郵箱連接", `斷開「${account.email}」後，會移除本服務保存的登入資料和郵件快取。原郵箱中的郵件不會刪除。`, "斷開連接", async () => {
      await api(`/api/accounts/${encodeURIComponent(account.id)}`, { method: "DELETE" });
      await loadAccounts();
      await loadSetup();
      if (state.page === "inbox") await loadMessages();
      notify("已斷開郵箱連接。");
    }));
    actions.append(sync, disconnect); card.append(actions); container.append(card);
  }
}
async function syncAccount(account, button) {
  setBusy(button, true);
  try {
    await api(`/api/accounts/${encodeURIComponent(account.id)}/sync`, { method: "POST" });
    notify("已開始同步，可在郵箱狀態查看進度。");
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
  if (!list.children.length) emptyState(list, "正在載入郵件", "從你的中轉站讀取已同步的郵件。", null, "", "↻");
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
    $("page-range").textContent = `${numberFormat.format(start)}–${numberFormat.format(end)} ／ ${numberFormat.format(state.total)} 封`;
    $("prev-page").disabled = state.offset === 0;
    $("next-page").disabled = state.offset + state.limit >= state.total;
  } catch (error) {
    if (epoch !== state.searchEpoch || controller.signal.aborted || !state.authenticated) return;
    emptyState(list, "無法載入郵件", errorText(error), () => void loadMessages(), "重新載入", "!");
    $("page-range").textContent = "尚未取得最新結果";
  } finally { if (epoch === state.searchEpoch) list.setAttribute("aria-busy", "false"); }
}
function renderMessages(messages) {
  const list = $("mail-list");
  if (!messages.length) {
    if (state.query) emptyState(list, "沒有符合的郵件", "試試較短的關鍵字，或切換郵箱。搜尋僅涵蓋目前已同步的郵件。", clearSearch, "清除搜尋", "⌕");
    else if (!state.accounts.length) emptyState(list, "郵件會在這裡匯合", "連接你的第一個郵箱，開始跨帳號閱讀與搜尋。", openAccount, "連接郵箱");
    else emptyState(list, "目前沒有快取郵件", "郵箱可能尚未完成同步，或同步範圍內沒有郵件。到「我的郵箱」查看狀態。", () => { location.hash = "accounts"; }, "查看我的郵箱");
    return;
  }
  list.replaceChildren();
  for (const message of messages) {
    const row = node("button", "mail-row" + (message.unread ? " unread" : ""));
    row.type = "button";
    row.setAttribute("aria-label", `${message.unread ? "未讀，" : ""}${message.subject || "無主旨"}，${message.sender || "寄件者不明"}，${message.account_email || ""}`);
    const sender = node("span", "mail-sender", message.sender || "寄件者不明");
    const summary = node("span", "mail-summary");
    const title = node("span", "mail-subject", message.subject || "（無主旨）");
    const snippet = node("span", "mail-snippet", message.snippet || "開啟以閱讀內文");
    const origin = node("span", "mail-origin");
    origin.append(node("span", "tag neutral", providerName(message.provider)), node("span", "", message.account_email || ""));
    if (message.unread) origin.append(node("span", "", "未讀"));
    if (message.attachments?.length) origin.append(node("span", "", `${message.attachments.length} 個附件`));
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
  state.readerController?.abort();
  const controller = new AbortController();
  state.readerController = controller;
  const epoch = ++state.readerEpoch;
  const content = $("reader-content");
  const title = node("h2", "", "正在讀取郵件"); title.id = "reader-title";
  content.replaceChildren(title, node("p", "muted", "正在讀取郵件快取…"));
  openDialog($("reader-dialog"), $("close-reader"));
  try {
    const { message } = await api(`/api/messages/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (epoch !== state.readerEpoch || !$("reader-dialog").open || !state.authenticated) return;
    title.textContent = message.subject || "（無主旨）";
    const metadata = node("dl", "reader-meta");
    const recipients = Array.isArray(message.recipients) ? message.recipients.join(", ") : message.recipients;
    for (const [label, value] of [["寄件者", message.sender], ["收件者", recipients], ["時間", date(message.received_at, true) + " · Europe/Belgrade"], ["來源", message.account_email], ["快取同步", message.sync?.last_sync ? date(message.sync.last_sync, true) + " · Europe/Belgrade" : "未記錄"], ["同步範圍", snapshotRange(message.sync?.days, message.sync?.per_account_limit)]]) {
      metadata.append(node("dt", "", label), node("dd", "", value || "—"));
    }
    const body = node("div", "reader-body", message.body || "此郵件沒有可顯示的文字內文。");
    content.replaceChildren(title, metadata);
    if (message.body_truncated) content.append(node("p", "banner warning", "此郵件僅同步部分內容。完整內容請至原郵箱查看。"));
    content.append(body);
    if (message.attachments?.length) {
      const section = node("section", "attachment-list");
      section.append(node("h3", "", `附件（${message.attachments.length}）`), node("p", "small muted", "僅列附件資料，不提供附件內容。"));
      for (const attachment of message.attachments) section.append(node("div", "attachment", attachment.filename || "未命名附件"));
      content.append(section);
    }
    const original = safeURL(message.source_url);
    if (original) {
      const link = node("a", "button outline", "在原郵箱開啟 ↗");
      link.href = original; link.target = "_blank"; link.rel = "noopener noreferrer";
      content.append(link);
    }
  } catch (error) {
    if (epoch !== state.readerEpoch || controller.signal.aborted || !state.authenticated) return;
    title.textContent = "無法讀取郵件";
    const retry = node("button", "button outline", "重新載入"); retry.type = "button"; retry.addEventListener("click", () => void openMessage(id));
    content.replaceChildren(title, node("p", "error-line", errorText(error)), retry);
  }
}
function closeReader() {
  state.readerController?.abort(); state.readerEpoch += 1;
  closeDialog($("reader-dialog"));
  $("reader-content").replaceChildren();
}
function renderProviders() {
  const select = $("provider-select"); select.replaceChildren();
  for (const provider of state.providers) select.add(new Option(provider.name, provider.id));
  if (state.providers.length) { state.selectedProvider = state.providers[0].id; configureProvider(); }
}
function configureProvider() {
  const provider = state.providers.find((item) => item.id === $("provider-select").value);
  state.selectedProvider = provider?.id || "";
  $("account-form").hidden = provider?.kind !== "imap";
  $("oauth-pane").hidden = provider?.kind !== "oauth";
  $("unsupported-pane").hidden = provider?.kind !== "unsupported";
  $("provider-note").textContent = provider?.note || "";
  const guide = safeURL(provider?.guide_url);
  $("provider-guide").hidden = !guide;
  if (guide) $("provider-guide").href = guide;
  $("account-host").value = provider?.host || "";
  $("account-host").readOnly = Boolean(provider?.host);
  $("account-port").value = String(provider?.port || 993);
  $("advanced-settings").open = provider?.id === "custom";
  $("account-error").textContent = "";
  $("oauth-error").textContent = "";
  $("start-oauth").disabled = !provider?.configured;
  $("open-oauth-settings").hidden = Boolean(provider?.configured);
  $("start-oauth").textContent = `登入 ${provider?.name || "郵箱"} 並授權`;
  $("oauth-state").textContent = provider?.configured ? "前往服務商的登入頁面，可選擇或新增另一個帳號。授權完成後會回到這裡。" : "伺服器尚未設定此服務的 OAuth 應用。請先依部署說明填入 Client ID、Client Secret 和回呼網址。";
  $("oauth-state").className = "banner" + (provider?.configured ? "" : " warning");
  $("unsupported-pane").textContent = provider?.note || "此服務目前無法直接連接。";
}
function openAccount() {
  if (!state.providers.length) { notify("郵箱服務清單尚未載入，請重新整理頁面。"); return; }
  $("account-form").reset(); clearValidation($("account-form"));
  state.accountDirty = false; state.accountBusy = false; state.pendingDiscard = null;
  $("discard-account").hidden = true; $("account-password").type = "password";
  const reveal = document.querySelector('[data-reveal="account-password"]'); reveal.textContent = "顯示"; reveal.setAttribute("aria-pressed", "false");
  configureProvider();
  openDialog($("account-dialog"), $("provider-select"));
}
function guardDiscard(action) {
  if (state.accountBusy) return;
  if (!state.accountDirty) { action(); return; }
  state.pendingDiscard = action;
  $("discard-account").hidden = false;
  $("keep-account").focus();
}
function closeAccount() {
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
async function loadTokens() {
  const epoch = ++state.tokenEpoch;
  const list = $("tokens-list");
  if (!list.children.length) list.replaceChildren(node("p", "token-empty", "正在載入金鑰…"));
  try {
    const { tokens } = await api("/api/tokens");
    if (epoch !== state.tokenEpoch || !state.authenticated) return;
    list.replaceChildren();
    if (!tokens.length) { list.append(node("p", "token-empty", "尚未建立金鑰。先為你的 Codex 建立一組。")); return; }
    for (const token of tokens) {
      const item = node("div", "token-item");
      const info = node("div");
      info.append(node("strong", "", token.name), node("p", "", "建立於 " + date(token.created_at, true)), node("p", "", token.last_used_at ? "上次使用 " + date(token.last_used_at, true) : "尚未使用"));
      const revoke = node("button", "button danger-text small", "撤銷"); revoke.type = "button";
      revoke.addEventListener("click", () => askConfirmation("撤銷存取金鑰", `撤銷「${token.name}」後，使用這組金鑰的 AI 將無法讀取郵件。你可以另外建立一組金鑰。`, "撤銷金鑰", async () => {
        await api(`/api/tokens/${encodeURIComponent(token.id)}`, { method: "DELETE" });
        await loadTokens(); await loadSetup(); notify("已撤銷存取金鑰。");
      }));
      item.append(info, revoke); list.append(item);
    }
  } catch (error) {
    if (epoch !== state.tokenEpoch || !state.authenticated) return;
    const retry = node("button", "button outline small", "重新載入"); retry.type = "button"; retry.addEventListener("click", () => void loadTokens());
    list.replaceChildren(node("p", "error-line", errorText(error)), retry);
  }
}
async function copyText(value, input, status) {
  try {
    await navigator.clipboard.writeText(value);
    if (status) status.textContent = "已複製，請存放在安全的位置。";
    else notify("已複製。");
  } catch {
    input?.focus(); input?.select();
    const message = "瀏覽器無法自動複製，請選取內容後手動複製。";
    if (status) status.textContent = message; else notify(message);
  }
}
function closeSecret() {
  $("new-token").value = ""; $("new-token").type = "password";
  state.oneTimeSecret = false;
  closeDialog($("secret-dialog"));
}

async function loadSetup() {
  const epoch = ++state.setupEpoch;
  try {
    const setup = await api("/api/setup");
    if (epoch !== state.setupEpoch || !state.authenticated) return;
    if (!Array.isArray(setup.steps)) throw new Error("無法取得設定步驟，請重新整理。");
    state.setup = setup;
    $("setup-panel").hidden = setup.ready === true;
    const list = $("setup-steps"); list.replaceChildren();
    const complete = setup.steps.filter((step) => step.complete).length;
    $("setup-progress").textContent = `${complete} / ${setup.steps.length} 已完成`;
    setup.steps.forEach((step, index) => {
      const row = node("div", "setup-step" + (step.complete ? " complete" : ""));
      const marker = node("span", "step-marker", step.complete ? "✓" : String(index + 1));
      marker.setAttribute("aria-label", step.complete ? "已完成" : "待完成");
      const text = node("div"); text.append(node("strong", "", step.title), node("p", "", step.detail));
      row.append(marker, text); list.append(row);
    });
  } catch (error) {
    if (epoch !== state.setupEpoch || !state.authenticated) return;
    if (!state.accounts.length) {
      $("setup-panel").hidden = false;
      $("setup-progress").textContent = "尚未取得狀態";
      const retry = node("button", "button outline small", "重新取得狀態"); retry.type = "button";
      retry.addEventListener("click", () => void loadSetup());
      $("setup-steps").replaceChildren(node("p", "error-line", errorText(error)), retry);
    }
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
    $(service + "-client-secret").value = "";
    $(service + "-client-secret").type = "password";
    $(service + "-client-secret").disabled = false;
    const reveal = document.querySelector(`[data-reveal="${service}-client-secret"]`);
    reveal.textContent = "顯示"; reveal.setAttribute("aria-pressed", "false"); reveal.disabled = false;
    $("clear-" + service + "-secret").checked = false;
    $("clear-" + service + "-secret").disabled = !provider.secret_configured;
    $(service + "-redirect").value = provider.redirect_uri;
    $(service + "-configured").textContent = provider.secret_configured ? "已儲存 Secret" : "尚未設定 Secret";
    $(service + "-configured").className = "tag" + (provider.secret_configured ? "" : " neutral");
  }
  clearValidation($("settings-form"));
  state.settingsDirty = false;
  $("settings-dirty").textContent = "尚未變更設定。";
  $("settings-fields").disabled = false;
  $("settings-status").hidden = true;
  $("retry-settings").hidden = true;
  $("settings-error").textContent = "";
}
function markSettingsDirty() {
  state.settingsDirty = true;
  $("settings-dirty").textContent = "有尚未儲存的變更。";
}
function discardSettings() {
  if (state.settings) renderSettings(state.settings);
  else {
    $("google-client-secret").value = ""; $("microsoft-client-secret").value = "";
    state.settingsDirty = false;
  }
}
async function loadSettings() {
  if (state.settingsDirty || state.settingsBusy) return;
  const epoch = ++state.settingsEpoch;
  $("settings-status").textContent = "正在載入設定…";
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
  if (state.settingsBusy || !validateForm($("settings-form"))) return;
  for (const provider of ["google", "microsoft"]) {
    const previous = state.settings?.[provider];
    const newId = $(provider + "-client-id").value.trim();
    if (previous?.secret_configured && newId && newId !== previous.client_id && !$(provider + "-client-secret").value.trim() && !$("clear-" + provider + "-secret").checked) {
      setFieldError($(provider + "-client-secret"), "變更 Client ID 時，請一併填入新的 Secret，或勾選清除原值。");
      $(provider + "-client-secret").focus(); return;
    }
  }
  const body = collectSettings();
  const button = $("save-settings");
  state.settingsBusy = true; setBusy(button, true); $("settings-fields").disabled = true;
  $("settings-error").textContent = "";
  try {
    const settings = await api("/api/settings", { method: "POST", body });
    if (!state.authenticated) return;
    renderSettings(settings);
    state.session = { ...state.session, public_url: settings.public_url, sync_interval_seconds: settings.sync_interval_seconds, sync_days: settings.sync_days, sync_limit: settings.sync_limit };
    updateCoverage(); updateConnectionConfig();
    notify("設定已儲存，會套用到下一次同步。");
    await loadSetup();
    try {
      const { providers } = await api("/api/providers");
      if (state.authenticated) { state.providers = providers; renderProviders(); }
    } catch {
      if (state.authenticated) { $("settings-status").textContent = "設定已儲存，但服務清單尚未更新。連接郵箱前請重新整理頁面。"; $("settings-status").hidden = false; }
    }
  } catch (error) {
    if (!state.authenticated) return;
    $("settings-error").textContent = errorText(error);
    if (error.uncertain) $("settings-error").textContent += " 先捨棄本機表單並重新載入，即可確認伺服器目前的設定。";
  } finally {
    state.settingsBusy = false; setBusy(button, false);
    $("settings-fields").disabled = !state.authenticated;
  }
}
async function runDiagnostics() {
  const button = $("run-diagnostics"); if (button.disabled) return;
  setBusy(button, true);
  const list = $("diagnostics-results");
  list.replaceChildren(node("p", "small muted", "正在檢查服務設定與資料庫…"));
  try {
    const result = await api("/api/diagnostics", { method: "POST" });
    if (!state.authenticated) return;
    list.replaceChildren();
    for (const check of result.checks) {
      const row = node("div", "diagnostic-item");
      const labels = { pass: ["", "通過"], warning: ["pending", "提醒"], fail: ["error", "未通過"] };
      const label = labels[check.status] || ["neutral", "未知"];
      const info = node("div"); info.append(node("strong", "", check.name), node("p", "", check.detail));
      row.append(node("span", "tag " + label[0], label[1]), info); list.append(row);
    }
    list.append(node("p", "footnote", "檢查時間：" + date(result.checked_at, true) + " · Europe/Belgrade"));
  } catch (error) { if (state.authenticated) list.replaceChildren(node("p", "error-line", errorText(error))); }
  finally { setBusy(button, false); }
}
async function loadGrants() {
  const epoch = ++state.grantsEpoch;
  const list = $("grants-list");
  if (!list.children.length) list.replaceChildren(node("p", "token-empty", "正在載入應用程式授權…"));
  try {
    const { grants } = await api("/api/oauth/grants");
    if (epoch !== state.grantsEpoch || !state.authenticated) return;
    list.replaceChildren();
    if (!grants.length) { list.append(node("p", "token-empty", "尚未授權任何應用程式。先在 AI 工具新增 MCP 位址並啟動登入。")); return; }
    for (const grant of grants) {
      const row = node("div", "token-item");
      const info = node("div");
      info.append(node("strong", "", grant.client_name), node("p", "", "授權於 " + date(grant.created_at, true)), node("p", "", grant.last_used_at ? "上次使用 " + date(grant.last_used_at, true) : "尚未使用"));
      const revoke = node("button", "button danger-text small", "撤銷授權"); revoke.type = "button";
      revoke.addEventListener("click", () => askConfirmation("撤銷應用程式授權", `「${grant.client_name}」將無法再使用這次授權讀取任何郵箱快取，其更新憑證也會失效。其他獨立授權不受影響。`, "撤銷授權", async () => {
        await api(`/api/oauth/grants/${encodeURIComponent(grant.id)}`, { method: "DELETE" });
        await loadGrants(); await loadSetup(); notify("已撤銷應用程式授權。");
      }));
      row.append(info, revoke); list.append(row);
    }
  } catch (error) {
    if (epoch !== state.grantsEpoch || !state.authenticated) return;
    const retry = node("button", "button outline small", "重新載入"); retry.type = "button";
    retry.addEventListener("click", () => void loadGrants());
    list.replaceChildren(node("p", "error-line", errorText(error)), retry);
  }
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
  $("consent-details").replaceChildren(node("dt", "", "狀態"), node("dd", "", "正在讀取授權要求…"));
  $("consent-expiry").textContent = "";
  $("consent-approve").disabled = true; $("consent-deny").disabled = true;
  openDialog($("consent-dialog"), $("consent-later"));
  try {
    const data = await api(`/api/oauth/consent?${new URLSearchParams({ request_id: state.oauthRequest })}`);
    if (epoch !== state.consentEpoch || !state.authenticated) return;
    if (data.request_id !== state.oauthRequest) throw new Error("授權要求不一致，請由 AI 工具重新發起連線。");
    state.consentData = data;
    const list = $("consent-details"); list.replaceChildren();
    for (const [label, value] of [["應用程式", data.client_name], ["完成後返回", data.redirect_uri], ["郵件服務", data.resource], ["授權範圍", "mail.read · 所有郵箱快取"]]) list.append(node("dt", "", label), node("dd", "", value));
    $("consent-expiry").textContent = "有效至 " + date(data.expires_at, true) + " · Europe/Belgrade";
    const expired = !Number.isFinite(new Date(data.expires_at).getTime()) || new Date(data.expires_at).getTime() <= Date.now();
    $("consent-approve").disabled = expired; $("consent-deny").disabled = expired;
    if (expired) $("consent-error").textContent = "授權要求已逾時，請回到 AI 工具重新發起登入。";
  } catch (error) {
    if (epoch !== state.consentEpoch || !state.authenticated) return;
    state.consentData = null;
    $("consent-error").textContent = errorText(error) + " 若已重新登入或要求逾時，請由 AI 工具重新發起連線。";
  }
}
async function resolveConsent(approve) {
  if (state.consentBusy || !state.consentData) return;
  if (new Date(state.consentData.expires_at).getTime() <= Date.now()) {
    $("consent-error").textContent = "授權要求已逾時，請回到 AI 工具重新發起登入。";
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
    if (!target) throw new Error("服務回傳的重新導向網址不受支援，請從 AI 工具重新發起連線。");
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

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.isComposing || !validateForm(event.currentTarget)) return;
  const button = event.currentTarget.querySelector('[type="submit"]');
  if (button.disabled) return;
  setBusy(button, true); $("login-error").textContent = "";
  try {
    const result = await api("/api/login", { method: "POST", body: { password: $("login-password").value } });
    state.csrf = result.csrf_token; await enterApp();
  } catch (error) { $("login-error").textContent = errorText(error); $("login-password").focus(); }
  finally { setBusy(button, false); }
});
async function performLogout() {
  const button = $("logout"); button.disabled = true;
  try { await api("/api/logout", { method: "POST" }); expireSession(""); }
  catch (error) { notify(errorText(error)); }
  finally { button.disabled = false; }
}
$("logout").addEventListener("click", () => {
  if (state.settingsBusy) { notify("正在儲存設定，請稍候。"); return; }
  if (state.settingsDirty) askConfirmation("捨棄未儲存的設定並登出？", "尚未儲存的同步設定與授權資料會清除。已儲存在伺服器的設定不受影響。", "捨棄變更並登出", async () => { discardSettings(); await performLogout(); });
  else void performLogout();
});
for (const button of document.querySelectorAll("[data-reveal]")) button.addEventListener("click", () => {
  const input = $(button.dataset.reveal);
  const reveal = input.type === "password"; input.type = reveal ? "text" : "password";
  button.textContent = reveal ? "隱藏" : "顯示";
  button.setAttribute("aria-label", `${reveal ? "隱藏" : "顯示"}${input.id === "new-token" ? "存取金鑰" : "密碼"}`);
  button.setAttribute("aria-pressed", String(reveal));
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
$("close-account").addEventListener("click", closeAccount);
$("cancel-account").addEventListener("click", closeAccount);
$("account-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeAccount(); });
$("account-form").addEventListener("input", () => { state.accountDirty = true; });
$("keep-account").addEventListener("click", () => { state.pendingDiscard = null; $("discard-account").hidden = true; $("account-email").focus(); });
$("discard-account-confirm").addEventListener("click", () => {
  const action = state.pendingDiscard; state.pendingDiscard = null; state.accountDirty = false;
  $("account-form").reset(); $("discard-account").hidden = true; action?.();
});
$("provider-select").addEventListener("change", () => {
  const next = $("provider-select").value;
  if (state.accountDirty) {
    $("provider-select").value = state.selectedProvider;
    guardDiscard(() => { $("provider-select").value = next; configureProvider(); });
  } else configureProvider();
});
$("account-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (state.accountBusy || !validateForm(event.currentTarget)) return;
  const form = event.currentTarget; const button = form.querySelector('[type="submit"]');
  state.accountBusy = true; setBusy(button, true); $("provider-select").disabled = true;
  $("account-error").textContent = "";
  const body = {
    provider: state.selectedProvider, email: $("account-email").value.trim(), label: $("account-label").value.trim(),
    username: $("account-username").value.trim(), password: $("account-password").value,
    host: $("account-host").value.trim(), port: Number($("account-port").value), folder: $("account-folder").value.trim(),
  };
  try {
    await api("/api/accounts/imap", { method: "POST", body, timeout: 90000 });
    if (!state.authenticated) return;
    state.accountDirty = false; form.reset(); closeDialog($("account-dialog"));
    await loadAccounts(); await loadSetup(); if (state.page === "inbox") await loadMessages();
    notify("已連接郵箱，正在準備同步。");
  } catch (error) { $("account-error").textContent = errorText(error); if (error.uncertain) await loadAccounts(); }
  finally { state.accountBusy = false; setBusy(button, false); $("provider-select").disabled = false; }
});
$("start-oauth").addEventListener("click", async () => {
  const button = $("start-oauth"); if (button.disabled) return;
  setBusy(button, true); $("oauth-error").textContent = "";
  try {
    const { url } = await api(`/api/oauth/${encodeURIComponent(state.selectedProvider)}/start`, { method: "POST" });
    if (!state.authenticated) return;
    if (!safeURL(url)) throw new Error("授權網址無效，請檢查伺服器設定。");
    location.assign(url);
  } catch (error) { $("oauth-error").textContent = errorText(error); setBusy(button, false); }
});
$("confirm-cancel").addEventListener("click", () => { if (!$("confirm-action").disabled) closeDialog($("confirm-dialog")); });
$("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); if (!$("confirm-action").disabled) closeDialog($("confirm-dialog")); });
$("confirm-action").addEventListener("click", async () => {
  const button = $("confirm-action"); if (button.disabled || !state.confirmTask) return;
  setBusy(button, true); $("confirm-cancel").disabled = true; $("confirm-error").textContent = "";
  try { await state.confirmTask(); closeDialog($("confirm-dialog")); }
  catch (error) { $("confirm-error").textContent = errorText(error); if (error.uncertain) { await loadAccounts(); await loadTokens(); await loadGrants(); } }
  finally { setBusy(button, false); $("confirm-cancel").disabled = false; }
});
$("token-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!validateForm(event.currentTarget)) return;
  const button = event.currentTarget.querySelector('[type="submit"]'); if (button.disabled) return;
  setBusy(button, true); $("token-error").textContent = "";
  try {
    const result = await api("/api/tokens", { method: "POST", body: { name: $("token-name").value.trim() } });
    if (!state.authenticated) return;
    $("token-name").value = ""; $("new-token").value = result.token; $("new-token").type = "password";
    const reveal = document.querySelector('[data-reveal="new-token"]'); reveal.textContent = "顯示"; reveal.setAttribute("aria-pressed", "false"); reveal.setAttribute("aria-label", "顯示存取金鑰");
    state.oneTimeSecret = true; $("copy-token-status").textContent = "";
    openDialog($("secret-dialog"), $("copy-token")); await loadTokens(); await loadSetup();
  } catch (error) { $("token-error").textContent = errorText(error); if (error.uncertain) await loadTokens(); }
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
$("settings-form").addEventListener("input", markSettingsDirty);
$("settings-form").addEventListener("change", markSettingsDirty);
$("settings-form").addEventListener("submit", saveSettings);
$("retry-settings").addEventListener("click", () => void loadSettings());
$("discard-settings").addEventListener("click", () => {
  if (state.settingsBusy) return;
  if (state.settingsDirty) askConfirmation("捨棄未儲存的設定？", "這會清除本機表單中的變更，並重新讀取伺服器已儲存的設定。", "捨棄並重新載入", async () => { discardSettings(); await loadSettings(); });
  else void loadSettings();
});
$("run-diagnostics").addEventListener("click", () => void runDiagnostics());
$("refresh-grants").addEventListener("click", () => void loadGrants());
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
    if (clear) input.value = "";
    input.disabled = clear;
    document.querySelector(`[data-reveal="${provider}-client-secret"]`).disabled = clear;
    markSettingsDirty();
  });
}
$("open-oauth-settings").addEventListener("click", () => { closeDialog($("account-dialog")); location.hash = "settings"; });
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
  if (state.accountDirty || state.oneTimeSecret || state.settingsDirty) { event.preventDefault(); event.returnValue = ""; }
});
window.addEventListener("online", () => { if (state.authenticated) { void loadAccounts(); if (state.page === "inbox") void loadMessages(); } });
window.addEventListener("offline", () => { $("network-banner").textContent = "目前離線。顯示的是上次取得的資料，操作不會排入離線佇列。"; $("network-banner").hidden = false; });
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.authenticated) { void loadAccounts(); if (state.page === "connect") { void loadTokens(); void loadGrants(); } } });
setInterval(async () => {
  if (!state.authenticated || document.hidden || state.pollBusy || $("confirm-dialog").open) return;
  state.pollBusy = true;
  try { await loadAccounts(); } finally { state.pollBusy = false; }
}, 10000);

async function boot() {
  const params = new URLSearchParams(location.search);
  state.oauthRequest = params.get("oauth_request") || "";
  if (params.has("connected") || params.has("connection_error")) {
    $("callback-banner").textContent = params.has("connected") ? "郵箱授權完成。可在「我的郵箱」查看同步狀態。" : "郵箱授權未完成。請檢查服務商設定後重新連接。";
    $("callback-banner").className = "banner" + (params.has("connection_error") ? " error" : "");
    $("callback-banner").hidden = false;
    params.delete("connected"); params.delete("connection_error");
    history.replaceState(null, "", location.pathname + (params.size ? "?" + params.toString() : "") + location.hash);
  }
  try { await enterApp(); }
  catch (error) { expireSession(errorText(error)); }
}
void boot();
