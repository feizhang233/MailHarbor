# AI 客戶端的 OAuth 連線

MailHarbor 的 OAuth 服務讓 AI 客戶端請求讀取郵件快取，以及選用的回覆權限。每次授權都會在 MailHarbor 管理頁顯示請求的權限，由目前登入的使用者核准。

在客戶端加入正式 HTTPS MCP 端點，例如 `https://mail.example.com/mcp`，然後選擇 OAuth／動態客戶端註冊。登入後，核對應用程式自報名稱、回呼網域及要求的權限，再按允許或拒絕。省略授權請求的 scope 時只會核發 `mail.read`。要讓客戶端發出回覆，請明確要求 `mail.read mail.reply`。已取得的唯讀授權需重新取得同意。舊版只註冊 `mail.read` 的客戶端需重新註冊連線。

每筆授權涵蓋該使用者目前與日後連接的所有郵箱。應用可用 `mail.read` 搜尋及閱讀郵件。另授予 `mail.reply` 後，應用可透過支援寄信的來源郵箱回覆指定的快取郵件。使用者可在工作台查看各應用的權限或撤銷授權。

郵箱服務也需要授權發信。先前只允許同步的 Gmail 或 Microsoft 郵箱，請在工作台重新連接同一帳號，勾選「允許此郵箱寄送回覆」，再依供應商提示授予 Gmail 的 `gmail.send` 或 Microsoft Graph 的 `Mail.Send`。IMAP 郵箱透過 SMTP 提交回覆，自訂 IMAP 需設定 SMTP 主機與連接埠。

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
| 授權範圍 | 預設 `mail.read`，可一併要求 `mail.reply` |
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
- 可在刷新時指定 `scope=mail.read` 取得唯讀 access token。輪替後的 refresh token 沿用當初核准的範圍。要永久移除某個應用的回覆權限，先撤銷舊授權，再核准新的唯讀請求。
- 管理頁「已授權應用程式」可以撤銷 grant，之後的存取立即失效。Gmail／Microsoft 的郵箱授權可在供應商的帳號設定中另外管理。

公開的 `/oauth/register` 供 AI 客戶端註冊應用程式，人員帳號則由登入頁透過 `/api/register` 建立。OAuth 客戶端註冊與授權有頻率和總量限制。尚未完成授權、超過 4 小時的閒置 client 可被清理。若首次連線停留過久，請在 AI 客戶端重新建立連線。已授權 client 會保留。

## 主應用整合契約

```python
oauth = OAuthServer(lambda: current_settings, store, owner_session=session_for)
app.include_router(oauth.router)  # 必須在根 catch-all MCP mount 之前

# /mcp 的 OAuth Bearer 驗證
access = oauth.access_token_access(token)
if access is None:
    raise HTTPException(status_code=401)
user_id, scope = access
```

`owner_session` 是主應用的瀏覽器會話驗證函數。登入的管理員或成員應傳回含可信 `user_id` 與 `csrf` 的會話字典，未登入或過期則傳回 `None`。MCP 工具以 Bearer token 解析出的 `user_id` 查詢郵箱與郵件，回覆工具另須檢查 `mail.reply`。`/v1` 由 `store.token_access(token)` 解析手動金鑰的使用者與權限。舊金鑰與預設建立的新金鑰都只有讀取權限。

未授權的 MCP 請求返回 401，並附上資源探索標頭。

```http
WWW-Authenticate: Bearer realm="MailHarbor", resource_metadata="https://mail.example.com/.well-known/oauth-protected-resource/mcp"
```

主應用的同源、CSRF、Host、body-size 限制仍需保留。OAuth token 端點使用 code 或 refresh token 的密碼學綁定確認權限，換發的 token 沿用原授權的使用者身分。

## 驗證狀態

測試涵蓋 discovery/DCR、錯誤 metadata、回呼與 audience、PKCE、使用者會話與 CSRF、同意／拒絕、授權隔離、請求逾期、code 重放、refresh 輪替／重放、token 雜湊、撤銷、失敗請求限流及舊註冊清理。純格式／PKCE 測試可離線執行。ASGI 和加密資料庫測試需要安裝 FastAPI、httpx、cryptography。真實 AI 客戶端的登入流程需在部署環境另行驗證。

官方參考：[OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)、[OpenAI OAuth plugin configuration](https://developers.openai.com/plugins/build/plugins)、[MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[RFC 7636 PKCE](https://www.rfc-editor.org/rfc/rfc7636)、[RFC 9207 issuer identification](https://www.rfc-editor.org/rfc/rfc9207)、[RFC 7009 revocation](https://www.rfc-editor.org/rfc/rfc7009)。
