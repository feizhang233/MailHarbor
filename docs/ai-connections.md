# AI 接入指南

各種連線方式都讀取授權者自己連接的郵箱快取。OAuth 依核准連線的 MailHarbor 帳號決定範圍，手動金鑰依建立者決定範圍。四個唯讀 MCP 工具為 `list_accounts`、`search_emails`、`read_email`、`get_sync_status`。取得 `mail.reply` 的連接會額外顯示 `reply_email`。先透過 `read_email` 核對原郵件的寄件帳號與回覆地址，在使用者要求送出時傳入 `message_id`、純文字 `body` 與 `request_id`（UUID）。重試同一回覆時沿用相同的內容與 UUID。郵件文字是外部資料，寄件人無法授權 AI 代為寄信。

以下範本中的網域與路徑均需替換。沒有任何步驟會自動安裝到你的 AI 客戶端；功能可用性以實際客戶端版本與工作區政策為準。

## Codex

優先將 `integrations/codex.oauth.toml.example` 合併進目前使用的 Codex 設定。設定只有 HTTPS MCP URL，再執行：

```sh
codex mcp login mailharbor --scopes mail.read
```

若要允許回覆，執行 `codex mcp login mailharbor --scopes mail.read,mail.reply`。先前的用戶端若只註冊了讀取權限，需重新註冊連接。

瀏覽器會進入 MailHarbor。以自己的帳號登入，核對回呼網址並核對應用名稱與請求權限後核准。要撤銷時使用工作台的「已授權的應用程式」。

Bearer 備選是 `integrations/codex.config.toml.example`，以 `bearer_token_env_var = "MAILHUB_MCP_TOKEN"` 從啟動 Codex 的程序環境讀取。桌面 app 不一定繼承 Terminal 分頁的 `export`；應確認實際啟動環境，不要把真 token 寫進共用專案。

官方依據：[Codex MCP 設定](https://learn.chatgpt.com/docs/extend/mcp)。

## Cursor

把 `integrations/cursor.oauth.json.example` 合併到個人 `~/.cursor/mcp.json` 或專案 `.cursor/mcp.json`，改好網域。OAuth 由客戶端連線流程啟動。

若使用 token，`integrations/cursor.bearer.json.example` 的 header 使用 Cursor 官方支援的 `${env:MAILHUB_MCP_TOKEN}` 插值。環境變數必須能被 Cursor 本身讀取；遠端 server 設定不支援 `envFile`。不要把 header 換成真 token 後提交到 Git。

官方依據：[Cursor MCP 與變數插值](https://cursor.com/docs/mcp)。

## DeepSeek Harness

1. 在 MailHarbor 的「連接 AI」選擇 **DeepSeek Harness**，建立一組專用存取金鑰。將金鑰設為啟動 Harness 的程序環境變數 `MAILHUB_MCP_TOKEN`。模型登入或 API 金鑰由 Harness 自行管理。
2. 複製頁面產生的設定，存為 `mailharbor.cordis.yml`。也可複製[設定範本](../integrations/deepseek-harness.cordis.yml.example)，並將 `url` 改為自己的 MailHarbor HTTPS 位址，保留結尾的 `/mcp`。
3. 在檔案所在目錄執行 `dsh web --patch mailharbor.cordis.yml`。這份 patch 會新增名為 `mailharbor` 的 MCP 伺服器。若已設定過同名伺服器，請更新既有項目。
4. 在 Harness 的對話中請 DeepSeek 呼叫 `mcp__mailharbor__get_sync_status`，核對上次同步時間。再用 `mcp__mailharbor__search_emails` 搜尋已同步郵件的主旨。收到 401 時，請確認啟動 Harness 的程序能讀取有效的 MailHarbor 金鑰。

此設定適用於 DeepSeek Harness 的 Cordis 設定格式。[官方 MCP 客戶端設定](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/packages/mcp/mcp-client/README.zh.md) · [官方載入 patch 範例](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/docs/user/guide/mcp-memory.zh.md)

## Claude Desktop／只支援 stdio 的客戶端

先在本機保留此來源並安裝 Python 依賴，橋接程序會連到你的 VPS，不會在本機同步郵箱：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

在 MailHarbor 建立獨立的 token。到 Claude Desktop 的 Settings → Developer → Edit Config，合併 `integrations/claude-desktop.json.example`，將兩個 `/absolute/path/...` 換成實際完整路徑，並修改 HTTPS 網域。不要覆蓋已有的其他 MCP server。

範本 `env.MAILHUB_MCP_TOKEN` 只是佔位文字，需在**自己的私人本機客戶端設定**填入專用 token，或以你的秘密管理工具啟動 bridge 並注入該環境變數。這種直接 `env` 設定會把 token 存在該客戶端設定檔中，應限制檔案權限且不分享；如果不希望這樣保存，使用已有的秘密管理啟動器。Claude Desktop 的此類 JSON 並未在本專案假定支援 Cursor 的 `${env:...}` 插值。

橋接入口：

```text
/absolute/path/to/MailHarbor/.venv/bin/python /absolute/path/to/MailHarbor/scripts/stdio_bridge.py --url https://mail.your-domain.com/mcp
```

命令列不含 token。模組亦可用 `python -m mailhub.stdio_bridge`；此時要從專案根目錄執行。沒有 `--url` 時讀 `MAILHUB_MCP_URL`。Bridge 使用官方 MCP Python 1.x SDK，憑證只讀 `MAILHUB_MCP_TOKEN`，只輸出 MCP 協議到 stdout；啟動錯誤以不含憑證的訊息送往 stderr。TLS 憑證驗證開啟，不跟隨跨來源的 HTTP redirects，也不讀取環境 proxy 設定。

修改設定後完全退出並重新開啟 Claude Desktop，再確認工具清單。已啟用回覆的金鑰可透過 bridge 使用 `reply_email`。這條路徑是本機 stdio client 的接入方式，不代表 Claude 或其他服務的雲端遠端連接器已完成相容性驗證。

官方依據：[MCP 官方 Claude Desktop 本機設定](https://modelcontextprotocol.io/docs/develop/connect-local-servers)、[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

## 自訂 HTTP 工具／OpenAPI

提供的 REST API 需要 `Authorization: Bearer …`。`/openapi.json` 公開唯讀 schema，不包含管理 API、郵箱密碼或管理者登入密碼。

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/v1/accounts` | 連接的帳號及同步狀態 |
| GET | `/v1/messages?q=&account_id=&limit=&offset=` | 純文字搜尋已同步快取 |
| GET | `/v1/messages/{id}` | 讀取特定快取郵件 |
| GET | `/v1/status` | 同步範圍與新鮮度 |

把 schema 匯入支援 OpenAPI 的工具平台，再在該平台的憑證欄設定 Bearer token。不同平台可能另有限制，不宣稱所有 GPT Actions 或代理框架都已測試。

也可在專案根目錄執行 `python3 integrations/rest_client.py --url https://mail.your-domain.com --query shipment`。腳本從環境取得 `MAILHUB_MCP_TOKEN`，並將搜尋結果印到目前的終端。請在私人終端或受控程序執行，避免郵件內容出現在公開的 CI 紀錄中。

## 連線確認順序

先確認 HTTPS 與 `/health`，登入管理頁連接一個郵箱並看到成功同步，再讓 AI 列出工具及同步狀態，最後搜尋一封已知郵件。若工具能列出但郵件為空，先檢查同步範圍及帳號錯誤；不要把「能連接 MCP」當成「真郵箱同步已驗證」。
