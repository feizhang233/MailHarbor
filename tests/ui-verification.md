# UI verification evidence

The UI is a buildless vanilla JavaScript application. No bundled browser framework or node dependencies are required.

Automated project commands:

- `node --check mailhub/static/app.js`: JavaScript syntax.
- `python3 scripts/check_ui.py`: IDs, label/reference integrity, form validation ownership, no external assets or persistent browser secret storage, no HTML injection/native product dialogs, CSRF/search cancellation/IME markers.
- `python3 scripts/generate_ui_tokens.py --check`: exact DESIGN.md to generated CSS mapping.

These are static checks; they do not simulate a browser or prove full accessibility. Runtime/browser findings, if obtained, should be added below with actual evidence. Required manual checks are in UX-CONTRACT.md.

Implemented failure paths: login inline error, 401 clears mail/secrets and returns to login, 403 has explicit access error, generic network banner, read retry, mutation timeout identifies uncertain completion, stale search and reader cancellation, OAuth unavailable/error, mailbox error with sync retry, token create/revoke failure with retained form, clipboard failure manual copy fallback.

Implemented CRUD paths: mailbox verify/add, mailbox list/read, read-only sync, disconnect confirmation; token create with one-time masked secret, token list, revoke confirmation. Source mail mutation is intentionally absent.

Executed 2026-10-29:

- JavaScript syntax: passed.
- UI static contract: passed (158 unique IDs, 28 labeled fields; v0.2).
- DESIGN.md token mapping: passed.
- `node tests/ui_logic.cjs`: passed (real app logic with in-memory DOM/API doubles: boot, safe text rendering, truncation notice, settings secret preservation/explicit clearing, settings POST/refresh, client configuration selection without secrets, local diagnostic rendering, OAuth grant revocation confirmation, no approval during consent load, explicit denial, safe redirect rejection, CSRF, 403 distinction, 401 mail/secret cleanup, and late-token response after session expiry). This is not browser verification.
- Premium strict audit: passed, zero findings; evidence in premium-audit.json.
- Official `designmd lint DESIGN.md`: unavailable because npm registry DNS/network access returned ENOTFOUND. The JSON frontmatter is parsed and its runtime token mapping checked by the project script; this is not represented as a completed official lint.

Local browser verification completed later on 2026-10-29 after installing dependencies and obtaining permission to start a loopback listener. `tests/live_smoke.py` passed using Chromium against the real application with synthetic mailbox data. It exercises wrong-password feedback, login, safe mail rendering, settings persistence/secret clearing, diagnostics, token creation and confirmed revocation, logout, and a 390-pixel viewport without horizontal overflow. A new regression holds the settings POST pending, clicks logout, confirms that no discard dialog opens and the app stays visible, then releases the request and verifies successful persistence. The browser collects JavaScript errors and requires none.

The logic harness now also covers pending-save logout with both dirty and clean forms, idle dirty-form cancellation preserving the draft, and normal idle logout. These regressions failed against the original handler and pass after its busy-state guard.

No visual screenshots, screen-reader session, real mailbox authentication, external client installation, or live provider integration were performed. See `verification.md` for full runtime results and deployment limits.

Additional manual verification should exercise the long settings page on mobile/short viewports, native AI client/authentication selects, password reveal and clear controls, dirty-setting navigation/reload confirmation, consent callback display and keyboard focus, and app/token revocation failures. The logic harness covers data and event decisions but cannot prove layout, native popup behavior, screen-reader output, or focus containment.
