# Verification record — 2026-10-29

The release was tested with synthetic mail and mocked provider responses. Real-mailbox acceptance is a separate check.

## Source and fixes

1. **Microsoft snapshot integrity:** repeated pagination URLs previously returned partial results as successful synchronization. Empty or unusable pages also had no request-count bound, and duplicate message IDs could fill the requested limit before enough unique messages were collected. Pagination now raises `ProviderError` on loops or excess pages, deduplicates IDs, and stops normally after reaching the requested unique-message limit. A background-sync regression confirms errors retain the previous cache, successful-sync timestamp, and recorded coverage.
2. **Malformed CSRF headers:** non-ASCII `X-CSRF-Token` headers previously caused `hmac.compare_digest` to raise and return HTTP 500 in management and OAuth routes. Both boundaries now reject these inputs with 403. Regression checks confirm rejected requests do not create tokens or revoke grants; valid requests still work.
3. **Logout during settings save:** logout previously allowed a discard confirmation while a save was in progress, even though the server might commit that save. Logout now waits for the save result. Logic tests cover dirty and clean forms, and Chromium holds the actual settings request pending while checking that logout neither closes the app nor opens the discard confirmation.

## Executed results

| Check | Result |
|---|---|
| `.venv/bin/python scripts/ci_tests.py` | 94 passed; zero skips and expected failures |
| `node tests/ui_logic.cjs` | Passed, including pending-save logout and existing rendering/authentication races |
| `node --check mailhub/static/app.js` | Passed |
| `.venv/bin/python scripts/check_ui.py` | Passed: 158 IDs, 28 labeled fields |
| `.venv/bin/python scripts/generate_ui_tokens.py --check` | Passed |
| `.venv/bin/python -m compileall -q mailhub scripts tests` | Passed |
| `.venv/bin/python -m pip check` | No broken requirements |
| `PLAYWRIGHT_BROWSERS_PATH=/tmp/mailharbor-playwright .venv/bin/python tests/live_smoke.py` | Passed with real local HTTP, stdio MCP and Chromium |
| `docker compose --env-file .env.example config --no-interpolate --no-env-resolution --quiet` | Passed |
| Docker image build/start | Passed |
| Unit suite in the release image | 94 passed, zero skips |
| GitHub Actions Python 3.12 / 3.13 and browser/MCP | All three jobs passed |
| HTTPS / admin session / database / REST + MCP / token revocation | Passed in deployment verification |

The browser smoke covers incorrect-password feedback, login, synthetic-mail search/read and HTML injection protection, settings persistence and secret clearing, logout blocked during pending save, diagnostics, token creation/revocation, a 390-pixel mobile viewport without horizontal overflow, logout, and JavaScript error collection. The stdio bridge initializes through the actual MCP SDK, lists the four expected read-only tools, searches mail and reads a result over real local HTTP.

The test suite uses synthetic mail, passwords, tokens and provider responses. Provider network behavior is tested with mocks.

Tested main packages: FastAPI 0.162.0, Starlette 1.10.0, MCP 1.30.0, HTTPX 0.28.1, cryptography 46.0.7, Playwright 1.64.1. The installed Starlette emits a TestClient deprecation notice for HTTPX; this did not affect test results.

## Remaining provider/client acceptance

- Complete actual Google/Microsoft OAuth or IMAP authorization and test refresh, synchronization scope and disconnect behavior for each provider in use.
- Verify external Codex/client connectivity, explicit OAuth consent and grant revocation in the intended client, and preservation of provider read/unread flags.

Frontend evidence and its limits are recorded in `ui-verification.md`.
