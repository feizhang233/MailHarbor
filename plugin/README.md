# MailHarbor v0.2 外掛範本

本資料夾採 Agent Plugins 格式：`plugin.json` 定義外掛，`mcp.json` 宣告遠端 Streamable HTTP 端點，使用 OAuth discovery、公開用戶端 DCR、S256 PKCE 與 `mail.read` scope。`resource` 精確綁定該部署的 `/mcp`。

從專案根目錄產生你的版本：

```sh
python3 scripts/package.py --plugin-url https://mail.your-domain.com --output dist/mailharbor-plugin
```

生成器同時產出 Codex OAuth 及 Bearer 設定範本，不含任何真實憑證。先部署並驗證 HTTPS 服務，再使用 OAuth 設定連接 Codex，執行 `codex mcp login mailharbor`，在 MailHarbor 頁面明確核准該應用。

這是**尚待實際客戶端匯入驗證的外掛封裝**，並未建立公開外掛、通過目錄審核或安裝到你的帳號。`mail.example.com` 是佔位網址，不能直接連線。Google／Microsoft 的郵箱 OAuth 與此 AI-client OAuth 是獨立授權流程。

Bearer token 仍可供 Codex 設定與本機 bridge 使用，但不放在此 OAuth manifest 裡。此 OAuth server 目前不支援 CIMD、`private_key_jwt` 或 confidential-client secret；客戶端應使用 DCR 與 public-client PKCE。具體安全流程與未驗證項目見專案 `docs/oauth.md`。

官方格式與連線文件：

- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Plugin authentication](https://developers.openai.com/plugins/build/auth)
