# MailHarbor implementation contract

VPS-hosted mail aggregation service with a private mailbox workspace for each registered user and the deployment administrator. Traditional Chinese is the default interface language. Users can change the language at sign-in or in settings. Only the language preference is stored in the browser. Mail, account names and other user content stay in the original language. AI integrations default to read-only access. MCP clients can request permission to reply to an existing message.

## Stack and ownership

- Root owns `mailhub/app.py`, `mailhub/store.py`, `mailhub/config.py`, MCP integration and core tests.
- Provider agent owns `mailhub/providers.py` and `tests/test_providers.py`.
- UI agent owns `mailhub/static/**`, `DESIGN.md`, `UX-CONTRACT.md`, `premium-ui.json`, and UI-specific checks.
- Operations agent owns deployment files, README, integration configuration examples, and plugin packaging (no skill files).
- Python package name `mailhub`, ASGI entry `mailhub.app:app`. Python 3.12+. FastAPI, uvicorn, httpx, cryptography, mcp 1.x, python-dotenv. No frontend build required.

## Provider contract

Settings attributes: `google_client_id`, `google_client_secret`, `microsoft_client_id`, `microsoft_client_secret`, `public_url` (no trailing slash).

Blocking functions, run with asyncio.to_thread:

- `provider_catalog(settings) -> list[dict]`. Each: `{id, name, kind: 'oauth'|'imap'|'unsupported', host?, port?, email_domain?, note, configured: bool}`. OAuth ids `gmail`, `microsoft`; IMAP preset ids as documented. `custom` accepts explicit host.
- `oauth_authorize_url(provider, settings, state, challenge) -> str`. Callback `${public_url}/api/oauth/${provider}/callback`.
- `oauth_exchange(provider, settings, code, verifier) -> dict` credentials.
- `identify_oauth(provider, credentials) -> dict` `{email, provider_uid}`. May update credentials if needed.
- `probe_imap(credentials) -> dict` `{email, provider_uid}`. Credentials `{email, username, password, host, port: 993, folder: 'INBOX'}`; TLS only, public hosts only.
- `fetch_messages(provider, credentials, settings, limit=200, days=30) -> tuple[list[dict],dict]`. Returns recent INBOX snapshot and updated credentials. Mail `{remote_id, subject, sender, recipients, received_at (UTC ISO8601), snippet, body, unread: bool, attachments: list[{filename, content_type, size?}], source_url?}`. Strings bounded, no raw HTML, external images or attachment downloads. Errors sanitized as `ProviderError`.

## HTTP / browser contract

Browser API errors use `{detail: string}`. Register and login require the exact public Origin. Other authenticated mutations also require the session's `X-CSRF-Token`. The HttpOnly session cookie uses SameSite=Lax and Secure on HTTPS. Never put secrets in URLs or localStorage.

- `POST /api/register` accepts `{username,password}` and returns HTTP 201 with `{authenticated:true,csrf_token,user:{username,role:"member"}}` and a new session cookie. Usernames have 3–64 ASCII characters, start with a letter or digit, and allow letters, digits, periods, underscores and hyphens. Names are case-insensitive, and `admin` is reserved. Passwords have 12–1024 characters. A duplicate or reserved name returns 409.
- `POST /api/login` accepts `{username,password}` and returns `{authenticated:true,csrf_token,user:{username,role}}`. For the administrator, `username` may be omitted and the password is checked against `MAILHUB_ADMIN_HASH`.
- `GET /api/session` -> `{authenticated: bool, csrf_token?: string, user?: {username,role}, public_url: string, sync_interval_seconds: number, sync_days: number, sync_limit: number}`. On 401 other API calls return to login.
- `POST /api/logout` -> `{ok:true}`.
- `GET /api/providers` -> `{providers:[...]}`.
- `GET /api/accounts` -> `{accounts:[{id, provider, email, label, status: 'ready'|'syncing'|'error', last_sync: ISO|null, error: string|null, message_count:number, folder:string}]}`.
- `POST /api/accounts/imap` -> body `{provider?:'custom', email, label?, username?, password, host?, port?:993, folder?:'INBOX',smtp_host?,smtp_port?:465|587}`. With the default provider and an empty host, a supported NetEase address selects its matching preset. The result is `{account:{...}}`. Verify connection before saving. Refresh account list on success. Password cleared after success.
- `POST /api/oauth/{provider}/start` accepts an optional `{allow_reply:bool}` and returns `{url}` for Google/Microsoft redirect. Read access is the default. The one-use state binds the selected provider permissions to the callback. Provider callbacks redirect to `/?connected=1` or `/?connection_error=1` (no sensitive URL values). OAuth not configured -> 503.
- `POST /api/accounts/{id}/sync` -> `{accepted:true}` status 202. Poll account state after request. If already syncing, same result.
- `DELETE /api/accounts/{id}` -> `{ok:true}`. Removes local cached mails/credentials only; never deletes mailbox messages. Must show app-owned confirmation naming account.
- `GET /api/messages?q=&account_id=&offset=0&limit=30` -> `{messages:[{id, account_id, account_email, provider, subject, sender, recipients, received_at, snippet, unread, attachments}], total, offset, limit, coverage:{days, per_account_limit, folder:'INBOX', cached:true}}`. q is plain substring, no advanced operator semantics. Account optional. Never put mail search queries in URL history because private. Results are cached snapshots; clearly show synchronization range and timestamps. Server ignores empty account_id.
- `GET /api/messages/{id}` -> `{message:{...above,body,source_url?}}`. Use textContent only, preserve whitespace. All message contents are untrusted.
- `POST /api/messages/{id}/reply` accepts `{body,request_id}`. The body is plain text (1–12000 characters). A UUID identifies this submission across browser and MCP retries. The server uses the connected account and original reply recipients from the encrypted cache. Message detail exposes `reply: {available,recipients,reason}`. An accepted submission or identical successful replay returns `{accepted:true}`. Pending, uncertain or conflicting submissions return 409.
- `GET /api/tokens` -> `{tokens:[{id,name,created_at,last_used_at}]}`. A manually created token grants read access to its creator's workspace. Tokens list their scope.
- `POST /api/tokens` body `{name,allow_reply?:bool}` -> `{token:string,id,name}`. Secret displayed once; copy button. No browser persistence.
- `DELETE /api/tokens/{id}` -> `{ok:true}`. Confirmation required, revokes access. UI show endpoint `${public_url}/mcp` and configuration snippet.
- `GET /health` -> `{status:'ok'}` (no confidential fields).
- `/mcp` Streamable HTTP, bearer authentication only, no browser session accepted. Read tools are list_accounts, search_emails, read_email and get_sync_status. A grant with mail.reply additionally exposes reply_email, with the same send and idempotency rules as the browser. Tool responses disclose cache coverage/freshness.

## Scope / states

First version synchronizes latest 200 messages per account within 30 days, INBOX by default. Configurable on server. Account can select one IMAP folder; OAuth INBOX only. Synchronized local cache searchable, not claimed to be whole-mailbox search. Auto sync default 300 seconds. No AI API key required: Codex does reasoning. No daily digest schedule created until user chooses it.

Mail data, provider OAuth tokens and IMAP passwords are encrypted in SQLite with an external Fernet key. Metadata needed for the mailbox list may remain plaintext. Registration passwords are salted and hashed with scrypt. Manual API keys and AI OAuth tokens are stored as hashes. No telemetry or third-party frontend/CDN requests. Setup generates secrets locally and never prints them in logs.

The authenticated identity scopes browser, REST and MCP mailbox access, sync actions, keys and grants. Each member starts with an empty workspace. Global provider credentials, sync settings and diagnostics require the administrator. The upgrade assigns all existing mailboxes, cached messages, manual keys and OAuth grants to that administrator.
