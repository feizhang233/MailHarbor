# MailHarbor v0.2｜私人多郵箱中轉站

在自己的 VPS 上架設 MailHarbor，讓使用者自行註冊並連接多個郵箱。每位使用者都有獨立的郵箱、郵件快取和 AI 授權，可在網頁回覆郵件，也可授權 Codex 透過 MCP 搜尋、閱讀及回覆自己的郵件。

v0.2 增加可直接操作的設定頁、首次設定清單、本機狀態診斷、OAuth 登入授權與應用撤銷、Claude Desktop stdio 橋接，以及唯讀 REST/OpenAPI。郵箱供應商設定加密保存於資料庫，修改後套用到下一次同步。

在自己的伺服器上安裝，請參閱[部署指南](docs/deployment.md)。[測試紀錄](tests/verification.md)涵蓋以合成郵件執行的 HTTP、stdio MCP 與瀏覽器測試。實際郵箱的授權與同步需在自己的環境驗證。

## 管理頁現在可以做什麼

- 管理員可在「設定」調整全站郵件同步參數、設定郵箱供應商應用程式憑證，以及執行服務診斷。每位使用者都可查看 MCP 使用指南，並選擇繁體中文、簡體中文、英文、法文、德文、日文及塞爾維亞文（拉丁字母）。瀏覽器會記住介面語言。
- 在「我的郵箱 → 連接郵箱」選 Gmail 或 Outlook。管理員完成對應的全站 OAuth 應用程式設定後，成員就能在自己的工作台逐一授權郵箱。IMAP 郵箱可直接在表單驗證。
- 「連接 AI」選 Codex、Cursor、DeepSeek Harness、Claude Desktop 或其他工具，複製對應設定，建立／撤銷 token，審核並撤銷 OAuth 應用授權。
- 首次設定清單說明尚未完成的步驟；診斷檢查服務與資料庫設定，不會把設定存在誤報成郵箱或外部 HTTPS 已驗證。

## 郵箱接入

| 郵箱 | 接入方式 | 條件 |
|---|---|---|
| Gmail／Google Workspace | Google OAuth＋Gmail API | 先設定 Google OAuth 應用；Workspace 管理政策可能限制 |
| Outlook／Hotmail／Microsoft 365 | Microsoft OAuth＋Graph | 先設定 Microsoft 應用；工作帳號可能需管理員同意 |
| iCloud | IMAP TLS | Apple 帳號雙重認證＋App-specific password |
| QQ／QQ 企業郵箱 | IMAP TLS | 啟用 IMAP，使用郵箱授權碼或服務商要求的專用密碼 |
| 網易 163、126、Yeah、VIP 163、VIP 126、188 | IMAP TLS | 輸入郵箱地址後自動選用對應伺服器。請先啟用 IMAP 並取得客戶端授權碼 |
| Yahoo／AOL／Fastmail／Zoho／GMX | IMAP TLS | 依供應商設定應用程式密碼；部分方案或地區限制 IMAP |
| 其他企業、自建或自訂域郵箱 | 自訂 IMAP TLS | 可公開連線且憑證有效的 IMAP 伺服器 |
| Proton Mail | 本版未直接接入 | 一般第三方客戶端需 Proton Mail Bridge；本版不開放內網／本機 IMAP |
| Tuta | 本版未接入 | 不提供通用 IMAP；無法當作一般 IMAP 郵箱接入 |

預設是 **每 5 分鐘**讀取每個帳號 **最近 30 天、最新 200 封收件匣郵件**。IMAP 可指定一個資料夾；Google／Microsoft 目前讀收件匣。搜尋對象是已同步快取，並非整個郵箱歷史。結果會帶上次成功同步的時間與快取範圍；目前設定是下一次同步的目標，變更設定不會立即改寫既有快取範圍。同步失敗時保留舊快取，不能將舊結果當成最新郵件。

同步以唯讀方式開啟 IMAP 資料夾，並用 `BODY.PEEK` 讀取內容。附件只儲存與顯示中繼資料，不主動呼叫附件下載 API；Gmail 完整 MIME 回應可能附帶小型內嵌附件資料，程式會忽略，不保存附件內容。郵件 HTML 轉成純文字，不載入外部圖片。大型郵件只取有上限的文字，未取得完整正文時會標示部分內容。

## 回覆郵件

開啟一封已同步的郵件，按「回覆」，核對畫面上的寄件帳號和回覆地址，再輸入純文字並送出。回覆使用原郵件的 Reply-To 或寄件人地址。舊快取缺少回覆資料時，先同步一次。

Gmail 和 Outlook 連接預設只讀。若要回覆，連接或重新連接同一帳號時勾選郵箱的寄信選項，再核准供應商授權。IMAP 預設服務使用對應的 SMTP 主機。自訂 IMAP 郵箱可在連接表單填入 SMTP 主機，選擇 465（TLS）或 587（STARTTLS），並使用該帳號的登入憑證。SMTP 寄件備份取決於郵箱服務商。

在「連接 AI」建立金鑰時，可勾選回覆權限。使用 OAuth 的 AI 客戶端可請求 `mail.read mail.reply` 並在工作台核准。取得權限後，MCP 會列出 `reply_email`。只讀連接仍提供原有四個查閱工具。回覆工具使用原郵件 ID、正文和請求 UUID，同一封回覆重試時沿用原 UUID。服務商接受回覆後才會顯示成功；若回應中斷且結果未明，請先核對郵箱的寄件記錄。

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

### 註冊與登入

1. 開啟 `https://mail.your-domain.com`，選「註冊新帳號」。任何能開啟登入頁的人都可自行註冊。
2. 輸入 3–64 個字元的使用者名稱，以英文字母或數字開頭，其餘可使用英文字母、數字、點、底線或連字號。名稱不區分大小寫，`admin` 為管理員保留。密碼至少 12 個字元。
3. 按「建立帳號」後會直接登入自己的空白工作台。到「我的郵箱 → 連接郵箱」新增第一個郵箱。下次以相同的使用者名稱和密碼登入。

部署管理員以 `admin` 和安裝時設定的管理密碼登入，也可沿用原先留空使用者名稱的登入方式。管理員可設定全站同步和 Google／Microsoft 憑證。管理員在工作台及 AI 介面讀取的郵件限於自己連接的郵箱。

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
4. 以 MailHarbor 管理員登入，在「設定 → 郵箱授權 → Google / Gmail」填入 client ID 與 client secret，按「儲存設定」。設定加密保存在 SQLite，無需重新啟動。
5. 按「登入並新增 Gmail 帳號」，在 Google 選擇帳號。新增下一個帳號時，在同一處再次登入。

服務要求 `gmail.readonly`、`openid email` 身分資訊與離線授權。啟用回覆時會額外要求 `gmail.send`。Google 將 `gmail.readonly` 分類為 Restricted。External＋Testing 狀態的 refresh token 通常 7 天到期，不能把測試狀態當長期免維護方案。個人自用可能符合驗證豁免，但公開提供給其他人、伺服器儲存 restricted data 等情況，可能需要驗證與安全評估；依 Google 實際專案狀態處理。

官方說明：[Web server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)、[token 有效期與失效](https://developers.google.com/identity/protocols/oauth2)、[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)。

## Microsoft 登入設定

1. 在 [Microsoft Entra admin center](https://entra.microsoft.com/) 建立 App registration。
2. 要同時使用個人與工作／學校郵箱，選支援組織目錄與個人 Microsoft 帳號的 account type。
3. 設定 **Web** redirect URI：`https://mail.your-domain.com/api/oauth/microsoft/callback`，不要建立成 SPA。
4. 加入本服務使用的 delegated permissions：`Mail.Read`、`User.Read`、`openid` 與 `offline_access`。需要回覆時，再加入委派權限 `Mail.Send`。
5. 建立 client secret，以 MailHarbor 管理員登入，在「設定 → 郵箱授權 → Microsoft / Outlook」填 Application client ID 與 secret **value**。儲存後按「登入並新增 Outlook 帳號」。每個帳號分別授權。

工作帳號是否能授權由組織政策決定；遇到管理員同意要求需由租戶管理員處理。Client secret 到期也需要續期並更新管理頁設定。既有 `.env` 供應商欄位保留為初始備援；管理頁的已儲存值優先。網域、管理密碼雜湊與加密金鑰仍由部署設定管理。

官方說明：[Authorization code flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow)、[Graph permissions](https://learn.microsoft.com/graph/permissions-reference)。

## iCloud、QQ 與其他 IMAP

在管理頁選擇對應供應商，填電子郵件地址與授權碼／專用密碼。不要將這些憑證貼進聊天。本版使用公開主機的連接埠 993，使用 TLS 並驗證伺服器憑證；不支援明文、內網位址或跳過憑證驗證。預設資料夾為 INBOX；自訂資料夾支援 Unicode 名稱，連線時會轉為 IMAP modified UTF-7，請填寫伺服器上實際存在的完整資料夾名稱。

新增網易郵箱時，在預設表單填入完整地址即可帶入主機。支援 `@163.com`、`@126.com`、`@yeah.net`、`@vip.163.com`、`@vip.126.com` 和 `@188.com`。預設以完整地址作為使用者名稱。請在網易網頁郵箱設定開啟 IMAP，並在表單填入客戶端授權碼。[免費郵箱設定](https://help.mail.126.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b) · [VIP 郵箱伺服器設定](https://help.vip.188.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac205fa9c38aad06acb)

iCloud 使用 Apple 帳號產生的 [App-specific password](https://support.apple.com/en-us/102654)，需開啟雙重認證；主密碼變更會使專用密碼失效。官方 IMAP 主機為 `imap.mail.me.com`，設定見 [Apple 說明](https://support.apple.com/en-ie/102525)。

QQ 需要在郵箱設定啟用 IMAP 並產生授權碼，見 [QQ 官方說明](https://help.mail.qq.com/detail/106/985)。其他供應商的可用方案、地區伺服器、企業登入政策可能不同；預設值不代表帳號已獲准使用 IMAP。Zoho 等區域主機不同時使用「自訂 IMAP」。

## 讓 Codex 存取郵件

把 [OAuth 設定範本](integrations/codex.oauth.toml.example) 合併到 Codex 設定，改好 HTTPS 網域後執行 `codex mcp login mailharbor --scopes mail.read`。若要回覆，改用 `codex mcp login mailharbor --scopes mail.read,mail.reply`。以自己的 MailHarbor 帳號登入，核對應用、回呼網址和請求權限後核准。授權涵蓋自己目前和日後連接的郵箱快取，可在工作台撤銷。[OAuth 說明](docs/oauth.md)

以下是保留的 Bearer token 連線方式，適合 CLI、腳本與本機 stdio 橋接：

登入自己的工作台，在 AI 連接頁建立一組存取金鑰，將一次顯示的值存入自己的密碼管理器。金鑰可用於 MCP 與唯讀 REST API。只有勾選回覆權限的金鑰才能透過 MCP 回覆。

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

[`plugin/`](plugin/README.md) 提供使用 OAuth discovery／DCR 的外掛 manifest 範本。它與 Google／Microsoft 郵箱授權是兩組不同的 OAuth：前者控制 AI 對工作台的讀取與回覆權限，後者讓中轉站連接你的郵箱。此版本未在你的 Codex／ChatGPT 客戶端完成外掛匯入或公開目錄審核，不能把 manifest 產生視為已安裝。

## 其他 AI 與自訂工具

| 客戶端 | 建議連線 | 設定範本 |
|---|---|---|
| Codex | 遠端 MCP＋OAuth，或 Bearer token | `integrations/codex.oauth.toml.example`／`codex.config.toml.example` |
| Cursor | 遠端 MCP＋OAuth，或環境變數 Bearer header | `integrations/cursor.oauth.json.example`／`cursor.bearer.json.example` |
| DeepSeek Harness | 遠端 MCP，從環境變數讀取 MailHarbor 存取金鑰 | [DeepSeek Harness 設定範本](integrations/deepseek-harness.cordis.yml.example) |
| Claude Desktop | 本機 stdio bridge 連 VPS MCP | `integrations/claude-desktop.json.example` |
| 自訂 agent／其他 API 工具 | Bearer 驗證唯讀 REST | `/openapi.json`、`integrations/rest_client.py` |

完整設定、橋接依賴、憑證注入與已知相容性邊界見 [AI 接入指南](docs/ai-connections.md)。Bridge 會轉發四個唯讀工具，以及已授權連接的 `reply_email`，並保留工具的操作標記、結果及錯誤。其 token 只從程序環境讀取，不接受命令列密碼；正式端點必須 HTTPS。

## 維運與資料

SQLite 與郵箱快取存放在 Compose named volume `mailhub_data`。OAuth／IMAP 憑證及郵件內容以外置 `MAILHUB_KEY` 加密；帳號列表所需的部分中繼資料仍以明文保存。備份時同時保護資料庫與 `.env`，**失去加密金鑰就無法解密既有資料**。不要在更新時重新生成金鑰。

部署使用單一 app worker，避免背景同步重複執行。更新來源後使用 `docker compose up -d --build app`。排錯可看 `docker compose logs --tail=100 app`；服務不啟用 HTTP access log，以減少搜尋字詞或 OAuth query 出現在紀錄中。反向代理也不要自行啟用包含完整 query 的 access log。

在管理頁「移除郵箱」會刪除該帳號的本機憑證與快取，**不刪除服務商郵箱內容**；供應商端授權是否撤銷，請到 Google／Microsoft／Apple 等帳號安全頁管理。撤銷 MCP token 會中止該 token 之後的存取。

升級原先的單一管理員版本時，首次啟動會將既有郵箱、郵件快取、存取金鑰與 AI 應用授權歸入 `admin` 的工作台。升級前應停止 app 備份資料卷，或使用 SQLite 一致性備份，並保留原有 `.env` 和加密金鑰。[部署指南](docs/deployment.md)

此版本未自動配置備份排程、每日 AI 摘要、郵件附件分析或全歷史同步。

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
