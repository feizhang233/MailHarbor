# MailHarbor v0.2｜私人多郵箱中轉站

在自己的 VPS 上登入一次，分別連接多個郵箱，再讓 Codex 透過同一個唯讀 MCP 介面查找、閱讀郵件。每個帳號獨立儲存憑證，同一供應商可加入多個帳號。

v0.2 增加可直接操作的設定頁、首次設定清單、本機狀態診斷、OAuth 登入授權與應用撤銷、Claude Desktop stdio 橋接，以及唯讀 REST/OpenAPI。郵箱供應商設定加密保存於資料庫，修改後套用到下一次同步。

**這是待部署與真實郵箱驗證的來源碼；未替你建立 Google／Microsoft OAuth 應用、登入郵箱、部署 VPS 或安裝外掛。** 2026-10-29 已在本機 Python 3.12 執行 94 項測試，全數通過、零跳過，並通過使用合成郵件的真實 HTTP、stdio MCP 與 Chromium 操作驗證；修復與範圍見 [測試紀錄](tests/verification.md)。專案另附 GitHub Actions：Python 3.12／3.13 完整測試禁止跳過；GitHub Actions、Docker 容器執行和真實郵箱驗證尚未完成。

## 管理頁現在可以做什麼

- 「設定」調整同步間隔、天數及每帳號封數，填 Google／Microsoft Client ID 和 Secret；密碼欄留空保留原值，已儲存的 Secret 不會讀回，清除需明確選取。
- 「我的郵箱」逐一連接 Gmail、Microsoft 或 IMAP 帳號，查看錯誤及最後同步時間。
- 「連接 AI」選 Codex、Cursor、Claude Desktop 或其他工具，複製對應設定，建立／撤銷 token，審核並撤銷 OAuth 應用授權。
- 首次設定清單說明尚未完成的步驟；診斷檢查服務與資料庫設定，不會把設定存在誤報成郵箱或外部 HTTPS 已驗證。

## 郵箱接入

| 郵箱 | 接入方式 | 條件 |
|---|---|---|
| Gmail／Google Workspace | Google OAuth＋Gmail API | 先設定 Google OAuth 應用；Workspace 管理政策可能限制 |
| Outlook／Hotmail／Microsoft 365 | Microsoft OAuth＋Graph | 先設定 Microsoft 應用；工作帳號可能需管理員同意 |
| iCloud | IMAP TLS | Apple 帳號雙重認證＋App-specific password |
| QQ／QQ 企業郵箱 | IMAP TLS | 啟用 IMAP，使用郵箱授權碼或服務商要求的專用密碼 |
| 163／126／Yeah | IMAP TLS | 啟用 IMAP，使用客戶端授權碼 |
| Yahoo／AOL／Fastmail／Zoho／GMX | IMAP TLS | 依供應商設定應用程式密碼；部分方案或地區限制 IMAP |
| 其他企業、自建或自訂域郵箱 | 自訂 IMAP TLS | 可公開連線且憑證有效的 IMAP 伺服器 |
| Proton Mail | 本版未直接接入 | 一般第三方客戶端需 Proton Mail Bridge；本版不開放內網／本機 IMAP |
| Tuta | 本版未接入 | 不提供通用 IMAP；無法當作一般 IMAP 郵箱接入 |

預設是 **每 5 分鐘**讀取每個帳號 **最近 30 天、最新 200 封收件匣郵件**。IMAP 可指定一個資料夾；Google／Microsoft 目前讀收件匣。搜尋對象是已同步快取，並非整個郵箱歷史。結果會帶上次成功同步的時間與快取範圍；目前設定是下一次同步的目標，變更設定不會立即改寫既有快取範圍。同步失敗時保留舊快取，不能將舊結果當成最新郵件。

AI 工具只讀取資料，不發信、不刪信、不改已讀狀態。IMAP 採唯讀開啟與 `BODY.PEEK`。供應商授予的 IMAP 憑證本身可能可寫，唯讀限制由本服務實作。附件只儲存與顯示中繼資料，不主動呼叫附件下載 API；Gmail 完整 MIME 回應可能附帶小型內嵌附件資料，程式會忽略，不保存附件內容。郵件 HTML 轉成純文字，不載入外部圖片。大型郵件只取有上限的文字，未取得完整正文時會標示部分內容。

## 在 VPS 啟動

準備一台已安裝 Docker Engine 與 Compose 外掛的 Linux VPS，以及指向它的網域。若 VPS 已有反向代理，保留原服務並走下方的「已有反向代理」流程。

將來源檔案上傳至 VPS，例如 `~/mailharbor`，在該目錄執行：

```sh
python3 scripts/setup.py --url https://mail.your-domain.com
docker compose --profile https up -d --build
docker compose ps
```

`setup.py` 會要求你輸入並確認管理密碼，建立權限 `0600` 的 `.env`，包含密碼雜湊與隨機加密金鑰，不會把密碼或金鑰印到終端。已有 `.env` 時拒絕覆寫。

將 `mail.your-domain.com` 換成實際網域。Caddy profile 需要 80／443 可用，DNS 正確且防火牆允許這兩個連接埠，才可取得 HTTPS 憑證。服務本身只綁在 VPS 的 `127.0.0.1:8080`；不要把 8080 公開到網際網路。

開啟 `https://mail.your-domain.com`，以剛設定的管理密碼登入。IMAP 郵箱可直接新增；Google／Microsoft 要先完成以下 OAuth 設定。每個郵箱會個別驗證登入，失敗不會假裝已連接。

### 已有反向代理

```sh
docker compose up -d --build app
```

將現有代理的該網域轉送到 `http://127.0.0.1:8080`，保留原始 `Host`，允許 `/mcp` 的串流 HTTP。使用 Docker 網路的代理可轉送到同一網路內的 `app:8080`。`MAILHUB_PUBLIC_URL` 必須與瀏覽器實際使用的 HTTPS origin 完全相同。

### 本機開發

需要 Python 3.12+。以新的目錄或未設定的副本操作，避免覆寫正式憑證：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 scripts/setup.py --url http://127.0.0.1:8080
.venv/bin/python -m uvicorn mailhub.app:app --host 127.0.0.1 --port 8080 --no-access-log
```

開發時請始終用 `http://127.0.0.1:8080`，不要混用 `localhost`，否則同源檢查會拒絕操作。正式服務使用 HTTPS。

## Google 登入設定

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 建立自己的專案，啟用 Gmail API。
2. 設定 OAuth consent screen，建立類型為 **Web application** 的 OAuth client。
3. 加入精確的授權重新導向 URI：`https://mail.your-domain.com/api/oauth/gmail/callback`。
4. 在 MailHarbor「設定 → Google / Gmail」填入 client ID 與 client secret，按「儲存設定」。設定加密保存在 SQLite，無需重新啟動。
5. 在管理頁按 Google 登入。每個帳號各授權一次；重複新增不會讓其他帳號的 token 被覆蓋。

服務要求唯讀 Gmail scope、`openid email` 身分資訊與離線授權。Google 將 `gmail.readonly` 分類為 Restricted。External＋Testing 狀態的 refresh token 通常 7 天到期，不能把測試狀態當長期免維護方案。個人自用可能符合驗證豁免，但公開提供給其他人、伺服器儲存 restricted data 等情況，可能需要驗證與安全評估；依 Google 實際專案狀態處理。

官方說明：[Web server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)、[token 有效期與失效](https://developers.google.com/identity/protocols/oauth2)、[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)。

## Microsoft 登入設定

1. 在 [Microsoft Entra admin center](https://entra.microsoft.com/) 建立 App registration。
2. 要同時使用個人與工作／學校郵箱，選支援組織目錄與個人 Microsoft 帳號的 account type。
3. 設定 **Web** redirect URI：`https://mail.your-domain.com/api/oauth/microsoft/callback`，不要建立成 SPA。
4. 加入本服務使用的 delegated permissions：`Mail.Read`、`User.Read`、`openid` 與 `offline_access`；不需要 application-wide mail permission。
5. 建立 client secret，在 MailHarbor「設定 → Microsoft / Outlook」填 Application client ID 與 secret **value**，儲存後到「我的郵箱」登入。

工作帳號是否能授權由組織政策決定；遇到管理員同意要求需由租戶管理員處理。Client secret 到期也需要續期並更新管理頁設定。既有 `.env` 供應商欄位保留為初始備援；管理頁的已儲存值優先。網域、管理密碼雜湊與加密金鑰仍由部署設定管理。

官方說明：[Authorization code flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow)、[Graph permissions](https://learn.microsoft.com/graph/permissions-reference)。

## iCloud、QQ 與其他 IMAP

在管理頁選擇對應供應商，填電子郵件地址與授權碼／專用密碼。不要將這些憑證貼進聊天。本版使用公開主機的連接埠 993，使用 TLS 並驗證伺服器憑證；不支援明文、內網位址或跳過憑證驗證。預設資料夾為 INBOX；自訂資料夾支援 Unicode 名稱，連線時會轉為 IMAP modified UTF-7，請填寫伺服器上實際存在的完整資料夾名稱。

iCloud 使用 Apple 帳號產生的 [App-specific password](https://support.apple.com/en-us/102654)，需開啟雙重認證；主密碼變更會使專用密碼失效。官方 IMAP 主機為 `imap.mail.me.com`，設定見 [Apple 說明](https://support.apple.com/en-ie/102525)。

QQ 需要在郵箱設定啟用 IMAP 並產生授權碼，見 [QQ 官方說明](https://help.mail.qq.com/detail/106/985)。其他供應商的可用方案、地區伺服器、企業登入政策可能不同；預設值不代表帳號已獲准使用 IMAP。Zoho 等區域主機不同時使用「自訂 IMAP」。

## 讓 Codex 讀取

優先使用 OAuth：把 [OAuth 設定範本](integrations/codex.oauth.toml.example) 合併到 Codex 設定，改好 HTTPS 網域後執行 `codex mcp login mailharbor`。登入 MailHarbor 管理頁，確認要求存取的應用與回呼網址，再明確按「允許唯讀存取」。授權涵蓋所有已連接郵箱的快取；之後可從管理頁撤銷。詳細流程與限制見 [OAuth 說明](docs/oauth.md)。

以下是保留的 Bearer token 連線方式，適合 CLI、腳本與本機 stdio 橋接：

登入管理頁，在 AI 連接頁建立一個 MCP token，將一次顯示的值存入自己的密碼管理器。這個 token 只能使用唯讀 MCP，無法登入管理頁、增加郵箱或撤銷其他 token。

將 [Codex 設定範本](integrations/codex.config.toml.example) 的網域改成實際網域，再把該 table 合併到目前 Codex 使用的 `config.toml`：

```toml
[mcp_servers.mailharbor]
url = "https://mail.your-domain.com/mcp"
bearer_token_env_var = "MAILHUB_MCP_TOKEN"
```

讓**啟動 Codex 的程序環境**可讀取 `MAILHUB_MCP_TOKEN`。終端啟動的 CLI 可在該 shell 設定環境變數；桌面 app 通常不會繼承另一個 Terminal 分頁內的 `export`，要透過實際客戶端支援的憑證／啟動設定提供。不要把真實 token 放在共用設定、外掛 ZIP、命令歷史或截圖中。

建立新會話後先確認四個工具可見：`list_accounts`、`search_emails`、`read_email`、`get_sync_status`。可以讓 Codex：

- 「列出我的郵箱及最後同步時間。」
- 「搜尋所有已同步郵箱中提到 shipment 的郵件，按時間排序。」
- 「閱讀這封信，列出待辦事項，並附上原郵件來源與帳號。」

工具說明要求把郵件內容視為資料，不能將寄件人寫入的指令當成操作授權。此服務不需要 OpenAI API key；分析由使用它的 Codex／AI 完成，不會為每次同步呼叫模型。

[`plugin/`](plugin/README.md) 提供使用 OAuth discovery／DCR 的外掛 manifest 範本。它與 Google／Microsoft 郵箱授權是兩組不同的 OAuth：前者讓 AI 取得中轉站唯讀權限，後者讓中轉站讀你的郵箱。此版本未在你的 Codex／ChatGPT 客戶端完成外掛匯入或公開目錄審核，不能把 manifest 產生視為已安裝。

## 其他 AI 與自訂工具

| 客戶端 | 建議連線 | 設定範本 |
|---|---|---|
| Codex | 遠端 MCP＋OAuth，或 Bearer token | `integrations/codex.oauth.toml.example`／`codex.config.toml.example` |
| Cursor | 遠端 MCP＋OAuth，或環境變數 Bearer header | `integrations/cursor.oauth.json.example`／`cursor.bearer.json.example` |
| Claude Desktop | 本機 stdio bridge 連 VPS MCP | `integrations/claude-desktop.json.example` |
| 自訂 agent／其他 API 工具 | Bearer 驗證唯讀 REST | `/openapi.json`、`integrations/rest_client.py` |

完整設定、橋接依賴、憑證注入與已知相容性邊界見 [AI 接入指南](docs/ai-connections.md)。Bridge 限定四個已知唯讀工具，保留 MCP 結果及錯誤標記，不提供任意遠端工具轉發。其 token 只從程序環境讀取，不接受命令列密碼；正式端點必須 HTTPS。

## 維運與資料

SQLite 與郵箱快取存放在 Compose named volume `mailhub_data`。OAuth／IMAP 憑證及郵件內容以外置 `MAILHUB_KEY` 加密；帳號列表所需的部分中繼資料仍以明文保存。備份時同時保護資料庫與 `.env`，**失去加密金鑰就無法解密既有資料**。不要在更新時重新生成金鑰。

部署使用單一 app worker，避免背景同步重複執行。更新來源後使用 `docker compose up -d --build app`。排錯可看 `docker compose logs --tail=100 app`；服務不啟用 HTTP access log，以減少搜尋字詞或 OAuth query 出現在紀錄中。反向代理也不要自行啟用包含完整 query 的 access log。

在管理頁「移除郵箱」會刪除該帳號的本機憑證與快取，**不刪除服務商郵箱內容**；供應商端授權是否撤銷，請到 Google／Microsoft／Apple 等帳號安全頁管理。撤銷 MCP token 會中止該 token 之後的存取。

資料庫備份應在 app 停止或使用 SQLite 一致性備份時完成，不要只複製執行中的主 `.sqlite` 檔而忽略 WAL。此版本未自動配置備份排程、每日 AI 摘要、郵件附件分析、全歷史同步、多使用者租戶或發信功能。

## 驗證與封裝

在可安裝依賴的開發環境執行：

```sh
.venv/bin/python scripts/ci_tests.py
node tests/ui_logic.cjs
node --check mailhub/static/app.js
python3 scripts/check_ui.py
python3 scripts/generate_ui_tokens.py --check
python3 scripts/package.py
```

`scripts/ci_tests.py` 發現任何 skip、expected failure、測試失敗或零測試都會回傳失敗。GitHub Actions 在 Python 3.12／3.13 各跑一次，另開 Chromium 工作執行 `tests/live_smoke.py`；該測試只使用明確標記的合成郵件，不連真郵箱、不需 GitHub secrets。

本機執行完整瀏覽器／stdio 測試還需要：

```sh
.venv/bin/python -m pip install 'playwright>=1.49,<2'
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/live_smoke.py
```

封裝器只加入來源白名單，包括 `.github/workflows` 和 `.cjs` 測試，排除 `.env`、資料庫、虛擬環境、快取與符號連結。輸出 `dist/MailHarbor.zip`。測試通過不等同已完成真實供應商授權或 VPS 網路驗證；這兩步需在正式環境分別確認。
