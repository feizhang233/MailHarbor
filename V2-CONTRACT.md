# MailHarbor v0.2 work contract

User requests a useful settings frontend, improved AI connections, bug fixes, and GitHub upload. Preserve read-only provider behavior. No mailbox credentials available. GitHub target account feizhang233; root handles upload and repository creation (private by default).

## Ownership

- Root: app.py/config.py/store.py + backend settings API/core tests + GitHub.
- frontend: static/**, DESIGN.md/UX-CONTRACT.md/premium-ui.json and UI checks/tests.
- oauth: mailhub/oauth_server.py, tests/test_oauth_server.py, docs/oauth.md. Own OAuth-specific schema using store.db() directly; do not modify app/store.
- providers: providers.py, test_providers.py.
- integrations: mailhub/stdio_bridge.py, scripts/stdio_bridge.py if needed, integrations/**, plugin/**, Docker/compose/setup/package, CI config and documentation README, docs/** except oauth.md. Tests for these.

## Settings API (root implements)

- GET /api/settings -> `{public_url, sync_interval_seconds, sync_days, sync_limit, google:{client_id,secret_configured,redirect_uri}, microsoft:{client_id,secret_configured,redirect_uri}, storage:{encrypted:true}, restart_required:false}`. No secrets returned. Existing env config remains initial fallback; saved values encrypted in SQLite and applied dynamically.
- POST /api/settings body `{sync_interval_seconds:int(30..86400),sync_days:int(1..365),sync_limit:int(1..1000),google_client_id?:str,google_client_secret?:str,microsoft_client_id?:str,microsoft_client_secret?:str,clear_google_secret?:bool,clear_microsoft_secret?:bool}`. Blank secret keeps current; explicit clear checkbox required. Secret changes require the owner session/CSRF. Response same as GET. Settings updates affect next sync; existing mail cache pruned on next successful sync, no immediate destructive wipe. Don't make domain editable in UI: changing reverse proxy origin needs deployment configuration.
- GET /api/setup -> `{steps:[{id,title,complete,detail}], ready:bool, counts:{accounts,tokens}, ai:{mcp_url,oauth_supported:true,rest_base_url,openapi_url}}` for setup checklist. Network/provider connection state is not inferred from config presence.
- POST /api/diagnostics -> `{checks:[{id,name,status:'pass'|'warning'|'fail',detail}],checked_at}`. Test service/config/DB locally only; do not claim external HTTPS/mailbox verified. Also use `/api/accounts` last_sync/status as real provider evidence.

All management APIs use existing session + X-CSRF-Token; API errors detail string. Settings route UI `#settings`; first-login empty accounts surface setup checklist. Keep private secrets out of URL, browser storage, and exported configs. No fake successful credentials.

## OAuth server module (oauth agent implements, send exact API back)

`OAuthServer(settings_or_getter, store)` supports static Settings or getter returning live Settings. `router` for FastAPI routes. `validate_access_token(token)->bool`, `metadata_url` for WWW-Authenticate. Root includes router and calls validation in MCP auth fallback; browser session alone must never grant MCP.

- `GET /.well-known/oauth-protected-resource` and `GET /.well-known/oauth-protected-resource/mcp`; resource is `{public_url}/mcp`; auth server issuer public_url.
- `GET /.well-known/oauth-authorization-server`.
- `POST /oauth/register` dynamic public client registration (exact safe HTTPS or loopback redirect URIs, limits, no client secrets, grant auth_code/refresh_token, S256 PKCE).
- `GET /oauth/authorize` validates registered client/redirect/resource/scope/PKCE and queues authorization request; redirect to `/?oauth_request=opaque_id#connect`. No tokens or email data in URL. Owner logs in through portal if needed.
- `GET /api/oauth/consent?request_id=...` -> `{request_id,client_name,redirect_uri,scope:'mail.read',resource,expires_at}` (owner session guarded by root middleware).
- `POST /api/oauth/consent` body `{request_id,approve:bool}` -> `{redirect_url}`; owner/CSRF guarded. Approval is explicit user action naming requesting app, granting read-only access to all connected mailboxes. Deny routes OAuth error back.
- `POST /oauth/token` standard form-encoded code exchange/refresh with PKCE, exact redirect/client/resource; short-lived hashed access tokens; refresh rotation and revocation, no credentials leaked. Use stdlib parse_qs(await request.body()) to avoid optional multipart dependency.
- `POST /oauth/revoke` revokes token family; valid caller bound to registered client; idempotent generic result.
- `GET /api/oauth/grants` -> `{grants:[{id,client_name,created_at,last_used_at}]}`.
- `DELETE /api/oauth/grants/{id}` -> `{ok:true}`.

OAuth database tables via store.db use distinct names; no plain access/refresh tokens in storage. Use encrypted queued authorization data where necessary. Consent requests expire 10min, codes expire 60s+single use, access1h, refresh30d; bound resource/audience. Apply sensible registration/request caps, prune old transient rows. Sanitized errors. Root's global BodyLimit covers routes; OAuth public routes outside /api do not accept session as authentication. No secret into logs. Tests should cover attacks/replay/revoke with actual dependencies in CI.

## Other AI integration (root + integrations)

Root exposes bearer-protected GET /v1/accounts, /v1/messages?q=&account_id=&limit=&offset=, /v1/messages/{id}, /v1/status using same cache/validation; read only. GET /openapi.json publishes only those read-only routes and a bearer scheme; descriptions explicitly untrusted mail/cache scope. No admin routes or secrets. Used for custom tools / API clients. `/mcp` is primary remote Streamable HTTP.

Integrations agent adds stdio-to-remote bridge using official mcp Python client/server APIs for Claude Desktop etc. Never print secrets/stdout logs; endpoint HTTPS unless loopback, token from env only, preserve readonly annotations/errors. Document real tool names/configs for Codex, Claude Desktop bridge, Cursor remote, generic HTTP/OpenAPI. Fetch official current docs before claiming client support; examples should use placeholder origin/env not secrets. Add GitHub Actions on Python3.12/3.13 to install requirements, run tests with no skips allowed (CI wrapper) and JS checks; if adding browser e2e use clearly marked own fixtures, prefer actually testing app via browser in CI.

## Frontend AI integration

Add client selector with precise config/help for Codex/MCP clients/stdio/OpenAPI; OAuth connection via endpoint and explicit consent flow. Show authorized applications list and revoke dialog. `oauth_request` query survives login then loads consent; don't erase until resolved. No approve-on-load. Consent modal may show automatically but only POST after user clicks approve/deny. Configs never include token itself.

## Validation

Local network/package downloads remain blocked. Offline tests pass/skips must be honest. Root will try GitHub Actions after publishing. No live mailbox end-to-end claim without credentials. Do not write runtime data or secrets to repo. Source packaging must include .github workflows and .cjs checks but exclude tokens/.env/DB/venv.
