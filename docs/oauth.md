# AI 客戶端的 OAuth 連線

MailHarbor 的 OAuth 服務讓支援 MCP OAuth 的 AI 客戶端在管理頁請求唯讀授權。這與「連接 Gmail／Microsoft 郵箱」的供應商 OAuth 是兩套獨立流程：前者決定 AI 能否讀取快取，後者決定 MailHarbor 能否從郵箱同步。

在客戶端加入正式 HTTPS MCP 端點，例如 `https://mail.example.com/mcp`。選擇 OAuth／動態客戶端註冊。客戶端會自動發現授權端點，開啟 MailHarbor 管理頁；登入後，畫面顯示應用程式自報名稱、回呼網址、資源和 `mail.read` 權限。確認後按允許，或按拒絕返回客戶端。應用程式自報名稱未經 MailHarbor 驗證，請一併核對回呼網域。

此權限涵蓋目前與日後連接的所有郵箱快取，僅能讀取，不能管理帳號、修改設定、發信、刪信或標記已讀。尚未提供每個應用只可讀指定郵箱的權限分組。

## 支援的協定

| 項目 | 實作 |
|---|---|
| 授權伺服器 issuer | 正式 `MAILHUB_PUBLIC_URL`，不含結尾斜線 |
| Protected Resource Metadata | `/.well-known/oauth-protected-resource/mcp`，另有根路徑別名 |
| Authorization Server Metadata | `/.well-known/oauth-authorization-server` |
| 客戶端註冊 | `POST /oauth/register`，DCR 公開客戶端，`token_endpoint_auth_method=none` |
| 授權 | `GET /oauth/authorize`，authorization code＋PKCE S256 |
| 換取／刷新 token | `POST /oauth/token`，`application/x-www-form-urlencoded` |
| 撤銷 token | `POST /oauth/revoke`，相同格式，傳 `client_id` 與 `token` |
| 唯一 scope | `mail.read` |
| 唯一 OAuth resource | 正式來源網址加 `/mcp`，每次授權與換 token 必須精確匹配 |
| 回呼安全性 | 註冊的完整 HTTPS URI，或原生客戶端的 HTTP loopback URI；不接受萬用字元、自訂 scheme、fragment 或內嵌帳密 |
| Issuer 識別 | 授權成功／拒絕回呼包含 RFC 9207 `iss`，並在 metadata 宣告支援 |

不提供 OpenID Connect、ID token、CIMD、`private_key_jwt`、client secret、client credentials grant 或任意遠端 metadata URL 抓取。對要求 confidential-client 認證的主機，不能直接套用此公開客戶端配置。DCR 支援仍由實際 AI 客戶端與工作區政策決定。

### Token 與會話期限

- 待審核請求 10 分鐘到期；第一次在管理頁讀取時綁定當前登入會話。登出後，請由 AI 客戶端重新發起。
- Authorization code 60 秒到期，僅能使用一次；換 token 時檢查 client、完整 redirect URI、resource 和 PKCE verifier。
- Access token 最長 1 小時，只能存取 `/mcp` audience。手動 Bearer 金鑰是獨立路徑；不要將 OAuth token 用於 `/v1` REST API。
- Refresh token family 最長 30 天，每次刷新輪替，舊 refresh token 的有效重放會撤銷整個 family；舊 access token 也會在刷新時退役。
- 伺服器只儲存 token 的雜湊；授權請求與 code 的敏感 payload 使用既有外置金鑰加密。重啟服務不會遺失有效的 OAuth grant。
- 管理頁「已授權應用程式」可以撤銷 grant，之後的存取立即失效。這不會撤銷 Gmail／Microsoft 的郵箱同步授權。

公開註冊與授權有頻率和總量限制。尚未完成授權、超過 4 小時的閒置 client 可被清理；若首次連線停留過久，請在 AI 客戶端重新建立連線。已授權 client 會保留。程式目前以單一服務執行緒／程序模型管理私人使用，不是多租戶 OAuth 平台。

## 主應用整合契約

```python
oauth = OAuthServer(lambda: current_settings, store, owner_session=session_for)
app.include_router(oauth.router)  # 必須在根 catch-all MCP mount 之前

# 僅在 /mcp 的 Bearer 驗證路徑使用；不可接受瀏覽器 session 代替。
oauth.validate_access_token(token)
```

`owner_session` 必須驗證主應用的 owner session 並傳回含 `csrf` 的字典或 `None`。未提供驗證函數時，所有 consent／grant 管理路由會拒絕操作；不能把獨立 router 掛載後只依 cookie 名稱判斷已登入。

未授權的 MCP 請求應返回 401 與：

```http
WWW-Authenticate: Bearer resource_metadata="https://mail.example.com/.well-known/oauth-protected-resource/mcp", scope="mail.read"
```

主應用的同源、CSRF、Host、body-size 限制仍需保留。OAuth token 端點不使用 owner cookie，僅依 code／refresh token 的密碼學綁定換取權限。

## 驗證狀態

新增測試包含 discovery/DCR、錯誤 metadata、回呼與 audience、PKCE、owner session／CSRF、同意／拒絕、請求逾期、code 重放、refresh 輪替／重放、token 雜湊、撤銷、失敗請求限流及舊註冊清理。純格式／PKCE 測試可離線執行；ASGI 和加密資料庫測試需要安裝 FastAPI、httpx、cryptography。未取得真實 AI 客戶端完成登入的證據前，不將此協定實作描述為已完成 ChatGPT 實際連線驗證。

官方參考：[OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)、[OpenAI OAuth plugin configuration](https://developers.openai.com/plugins/build/plugins)、[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[RFC 7636 PKCE](https://www.rfc-editor.org/rfc/rfc7636)、[RFC 9207 issuer identification](https://www.rfc-editor.org/rfc/rfc9207)、[RFC 7009 revocation](https://www.rfc-editor.org/rfc/rfc7009)。
