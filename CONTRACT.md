# MailHarbor implementation contract

Personal, single-owner, VPS-hosted mail aggregation service. Interface language: Traditional Chinese. AI tools are read-only. This is a new empty project.

## Stack and ownership

- Root owns `mailhub/app.py`, `mailhub/store.py`, `mailhub/config.py`, MCP integration and core tests.
- Provider agent owns `mailhub/providers.py` and `tests/test_providers.py`.
- UI agent owns `mailhub/static/**`, `DESIGN.md`, `UX-CONTRACT.md`, `premium-ui.json`, and UI-specific checks.
- Operations agent owns deployment files, README, integration configuration examples, and plugin packaging (no skill files).
- Python package name `mailhub`, ASGI entry `mailhub.app:app`. Python 3.12+. FastAPI, uvicorn, httpx, cryptography, mcp 1.x, python-dotenv. No frontend build required.

## Provider contract

Settings attributes: `google_client_id`, `google_client_secret`, `microsoft_client_id`, `microsoft_client_secret`, `public_url` (no trailing slash).

Blocking functions, run with asyncio.to_thread:

- `provider_catalog(settings) -> list[dict]`. Each: `{id, name, kind: 'oauth'|'imap'|'unsupported', host?, port?, note, configured: bool}`. OAuth ids `gmail`, `microsoft`; IMAP preset ids as documented. `custom` accepts explicit host.
- `oauth_authorize_url(provider, settings, state, challenge) -> str`. Callback `${public_url}/api/oauth/${provider}/callback`.
- `oauth_exchange(provider, settings, code, verifier) -> dict` credentials.
- `identify_oauth(provider, credentials) -> dict` `{email, provider_uid}`. May update credentials if needed.
- `probe_imap(credentials) -> dict` `{email, provider_uid}`. Credentials `{email, username, password, host, port: 993, folder: 'INBOX'}`; TLS only, public hosts only.
- `fetch_messages(provider, credentials, settings, limit=200, days=30) -> tuple[list[dict],dict]`. Returns recent INBOX snapshot and updated credentials. Mail `{remote_id, subject, sender, recipients, received_at (UTC ISO8601), snippet, body, unread: bool, attachments: list[{filename, content_type, size?}], source_url?}`. Strings bounded, no raw HTML, external images or attachment downloads. Errors sanitized as `ProviderError`.

## HTTP / browser contract

All API responses JSON; errors `{detail: string}`. Same-origin requests only. Session cookie HttpOnly, SameSite strict/lax as appropriate, secure in production. Browser needs `X-CSRF-Token` header on all authenticated POST/DELETE. CSRF provided by login and session. Login POST itself requires exact Origin (tests may provide). Never put secrets in URL or localStorage.

- `GET /api/session` -> `{authenticated: bool, csrf_token?: string, public_url: string, sync_interval_seconds: number, sync_days: number, sync_limit: number}`. On 401 other API calls return to login.
- `POST /api/login` body `{password}` -> `{authenticated:true,csrf_token}`.
- `POST /api/logout` -> `{ok:true}`.
- `GET /api/providers` -> `{providers:[...]}`.
- `GET /api/accounts` -> `{accounts:[{id, provider, email, label, status: 'ready'|'syncing'|'error', last_sync: ISO|null, error: string|null, message_count:number, folder:string}]}`.
- `POST /api/accounts/imap` -> body `{provider, email, label?, username?, password, host?, port?:993, folder?:'INBOX'}`, result `{account:{...}}`. Verify connection before saving. Refresh account list on success. Password cleared after success.
- `POST /api/oauth/{provider}/start` -> `{url}` for Google/Microsoft redirect. Provider callbacks redirect to `/?connected=1` or `/?connection_error=1` (no sensitive URL values). OAuth not configured -> 503.
- `POST /api/accounts/{id}/sync` -> `{accepted:true}` status 202. Poll account state after request. If already syncing, same result.
- `DELETE /api/accounts/{id}` -> `{ok:true}`. Removes local cached mails/credentials only; never deletes mailbox messages. Must show app-owned confirmation naming account.
- `GET /api/messages?q=&account_id=&offset=0&limit=30` -> `{messages:[{id, account_id, account_email, provider, subject, sender, recipients, received_at, snippet, unread, attachments}], total, offset, limit, coverage:{days, per_account_limit, folder:'INBOX', cached:true}}`. q is plain substring, no advanced operator semantics. Account optional. Never put mail search queries in URL history because private. Results are cached snapshots; clearly show synchronization range and timestamps. Server ignores empty account_id.
- `GET /api/messages/{id}` -> `{message:{...above,body,source_url?}}`. Use textContent only, preserve whitespace. All message contents are untrusted.
- `GET /api/tokens` -> `{tokens:[{id,name,created_at,last_used_at}]}`. Tokens grant only read-only MCP access, never admin.
- `POST /api/tokens` body `{name}` -> `{token:string,id,name}`. Secret displayed once; copy button. No browser persistence.
- `DELETE /api/tokens/{id}` -> `{ok:true}`. Confirmation required, revokes access. UI show endpoint `${public_url}/mcp` and configuration snippet.
- `GET /health` -> `{status:'ok'}` (no confidential fields).
- `/mcp` Streamable HTTP, bearer authentication only, no browser session accepted. Tools list_accounts, search_emails, read_email, get_sync_status. Tool responses disclose cache coverage/freshness. No message writes, send/delete/mark-read tools.

## Scope / states

First version synchronizes latest 200 messages per account within 30 days, INBOX by default. Configurable on server. Account can select one IMAP folder; OAuth INBOX only. Synchronized local cache searchable, not claimed to be whole-mailbox search. Auto sync default 300 seconds. No AI API key required: Codex does reasoning. No daily digest schedule created until user chooses it.

Mail data, OAuth tokens and IMAP passwords are encrypted in SQLite with external Fernet key. Metadata needed for mailbox list may remain plaintext. SQL identifiers bound safely; search over decrypted bounded mailbox cache. Single owner only. Login password hashed with scrypt; MCP token hashes only. No telemetry or third-party frontend/CDN requests. Setup generates secrets locally and never prints them in logs.
