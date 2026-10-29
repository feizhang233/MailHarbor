# AI 接入指南

所有方式讀取同一份近期郵件快取，只有四個 MCP 工具：`list_accounts`、`search_emails`、`read_email`、`get_sync_status`。工具不發信、不刪信、不改已讀狀態；郵件文字始終是外部資料，不能當作代理操作指令。

以下範本中的網域與路徑均需替換。沒有任何步驟會自動安裝到你的 AI 客戶端；功能可用性以實際客戶端版本與工作區政策為準。

## Codex

優先將 `integrations/codex.oauth.toml.example` 合併進目前使用的 Codex 設定。設定只有 HTTPS MCP URL，再執行：

```sh
codex mcp login mailharbor
```

瀏覽器會進入 MailHarbor，登入管理者並核准 AI 應用的唯讀範圍。要撤銷時使用管理頁的「已授權的應用程式」。

Bearer 備選是 `integrations/codex.config.toml.example`，以 `bearer_token_env_var = "MAILHUB_MCP_TOKEN"` 從啟動 Codex 的程序環境讀取。桌面 app 不一定繼承 Terminal 分頁的 `export`；應確認實際啟動環境，不要把真 token 寫進共用專案。

官方依據：[Codex MCP 設定](https://learn.chatgpt.com/docs/extend/mcp)。

## Cursor

把 `integrations/cursor.oauth.json.example` 合併到個人 `~/.cursor/mcp.json` 或專案 `.cursor/mcp.json`，改好網域。OAuth 由客戶端連線流程啟動。

若使用 token，`integrations/cursor.bearer.json.example` 的 header 使用 Cursor 官方支援的 `${env:MAILHUB_MCP_TOKEN}` 插值。環境變數必須能被 Cursor 本身讀取；遠端 server 設定不支援 `envFile`。不要把 header 換成真 token 後提交到 Git。

官方依據：[Cursor MCP 與變數插值](https://cursor.com/docs/mcp)。

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

修改設定後完全退出並重新開啟 Claude Desktop，再確認四個工具出現。這條路徑是本機 stdio client 的接入方式，不代表 Claude 或其他服務的雲端遠端連接器已完成相容性驗證。

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

也可執行 `integrations/rest_client.py --url https://mail.your-domain.com --query shipment`；它從環境取得 `MAILHUB_MCP_TOKEN`。腳本會把你請求的郵件結果印到終端，適合自己的受控終端或程序，不要用在會公開輸出的 CI 工作。

## 連線確認順序

先確認 HTTPS 與 `/health`，登入管理頁連接一個郵箱並看到成功同步，再讓 AI 列出工具及同步狀態，最後搜尋一封已知郵件。若工具能列出但郵件為空，先檢查同步範圍及帳號錯誤；不要把「能連接 MCP」當成「真郵箱同步已驗證」。
