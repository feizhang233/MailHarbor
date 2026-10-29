# MailHarbor UI behavior contract

## Business sources

| Concern | Source | UI consequence |
|---|---|---|
| Permission model | CONTRACT.md: single owner and HTTP contract | Session login for management; separate read-only MCP tokens. |
| Data lifecycle and deletion | CONTRACT.md: DELETE account endpoint | Confirm account name; explain local cache and credentials are removed, original mailbox retained. |
| Mail privacy | CONTRACT.md: scope and states | Treat all mail as untrusted text; no remote images, search history, persistent browser cache, or token storage. |
| Synchronization coverage | CONTRACT.md: scope and states | Show server-provided days/limit, selected folder, and account timestamps. |
| Billing/legal | Not applicable | No subscription, payment, or invented legal copy. |
| Runtime settings and setup | V2-CONTRACT.md: Settings API | Secret values are write-only; empty preserves, explicit checkbox clears. Checklist only reflects server evidence. |
| AI OAuth consent | V2-CONTRACT.md: OAuth server and Frontend AI integration | Preserve request through login, show self-reported client identity and callback, never approve on load, revoke grants separately. |

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native select in index.html | DESIGN.md, platform popup accepted | provider/account | keyboard/browser check |
| Form | app.js: validateForm/setFieldError/setBusy | This contract | login/account/token | scripts/check_ui.py and browser |
| Scrollbar | app.css global baseline | DESIGN.md generated tokens | dialog/mail-list geometry | static check and browser |
| Toast | app.js: notify / #live-status | This contract | success/info | live region browser check |
| CRUD | app.js: api + list refresh handlers | CONTRACT.md | account/token | API checks and browser flow |
| Dialog | app.js: openDialog/closeDialog | This contract | account/reader/confirm/secret | keyboard and focus check |

## Flow ledger

Login submits credentials once, shows inline errors, clears the password after authentication, and restores the active safe page. A 401 cancels outstanding reads, closes dialogs, clears visible mail and secret fields, and returns to login. A 403 explains access failure without pretending the resource does not exist. The server remains the authority for access.

Creating a mailbox validates owned fields, prevents duplicate submission, waits for verification, closes the dialog, refreshes accounts, and announces completion. OAuth starts only from a user action, redirects to the provider, and shows a generic connected/error return state. Unconfigured OAuth is disabled with instructions. Unsupported providers show the real limitation. Switching provider while a credential form is dirty requests discard via an app-owned inline confirmation, never a native confirm.

Synchronization shows accepted/pending, then polls account state. It never claims message freshness until last_sync changes. Errors persist on the account with retry. Disconnect confirms the mailbox and consequence, then waits for the server before updating the list. No mailbox source message is modified by these UI actions.

Inbox search uses a plain substring, 300ms debounce, IME guard, AbortController, and monotonic request epochs. Clear is immediate. Page/account changes reset or clamp pagination. Search, mailbox filter, and page stay only in memory because of the personal nature of mailbox queries; navigation hashes contain only `inbox`, `accounts`, or `connect`. Reader fetches are cancelled when closed, and HTML is never injected.

Creating a token returns to the token list and opens a one-time secret dialog. Secret is masked, copy is explicit, and closing clears the DOM value. Token revocation confirms its name and consequence. Copy failure preserves the visible field and instructs manual copying. Request timeout on mutation states that completion is uncertain and refreshes lists before retry.

## Shared interaction states

All lists have initial loading, empty, error/retry, and success states. Inbox additionally has no-results and stable pagination. Buttons expose disabled and aria-busy. Every field has a real label, an owned error line, invalid state, and first-invalid focus. Fields preserve input on recoverable failure; secret fields clear on success/session loss. No automatic mutation retry.

Native modal dialogs provide inert background, focus containment and Escape behavior; helpers restore focus. Dirty account form close shows a discard/keep choice in the same dialog. Beforeunload is used only to protect a dirty account form or unrecorded new token on real page unload. Other in-app navigation retains list state. Serious confirmations focus Cancel first. Dialog controls remain reachable at narrow and short viewports.

Static buttons are disabled until external JavaScript has bound their action handlers. This progressive-enhancement boundary leaves no enabled actionless controls when JavaScript fails. No inline JavaScript or CSP exceptions are needed. The premium auditor recognizes seven named capabilities; the additional Dialog row is a project-owned extension, not included in its requiredCapabilities list.

Network failures show a persistent inline state and retry; stale readable mail remains labeled. Account polling is read-only and stops while logged out or page-hidden. Reconnection/visibility revalidates account status. No offline queue or optimistic destructive updates are used. The app uses `zh-TW` and explicitly displayed `Europe/Belgrade` times. Reduced-motion and forced-color preferences are respected.

## Version 0.2 settings and AI flows

Settings loads its safe public projection and displays no stored Client Secret. Blank secret fields are omitted on Save. A checked clear control disables and empties the corresponding draft secret and submits only the explicit clear flag. Changing an existing Client ID requires updating or clearing its secret, matching the API. Successful Save clears secret inputs, applies live synchronization display values, refreshes provider availability/setup, and stays on the owning page. Failure preserves the draft and explains uncertainty for timed-out mutations. Reload offers an app-owned discard confirmation. Navigation and logout protect unsaved settings; active saving blocks navigation until a result. Session loss clears both provider-secret drafts.

The inbox coverage strip labels current days/limit as the synchronization target, not a statement about existing cached messages. Account cards use snapshot_days/snapshot_limit and last_sync to show the actual saved snapshot. Each message reader uses message.sync for that account's last successful snapshot. Missing snapshot values are marked 未記錄 and never replaced by today's target. Settings changes take effect in each mailbox cache only after its next successful sync.

The setup checklist shows server-provided steps and labels; configuration presence does not imply provider/network success. Diagnostics reports only server-returned local checks and states that external HTTPS and mailbox connectivity remain unverified.

AI selection supports Codex and Cursor with OAuth or environment-backed bearer configuration, Claude Desktop via the actual local stdio bridge, read-only REST/OpenAPI, and generic remote MCP OAuth connection metadata. Copied configurations never include a live secret from this application. Claude's placeholder token is clearly identified and must be filled privately or supplied by a secure launcher.

`oauth_request` remains in the URL across login and provider callback cleanup. Its opaque identifier is the only OAuth continuation state placed there. Fetching consent never sends an approve/deny mutation. Consent shows the application's unverified self-reported name, full redirect URI, service resource, expiry, and read access to all present/future connected mailbox caches. The initial focus is the least consequential Later action. Deny and Allow each require explicit clicks and CSRF; only server-returned HTTPS or exact HTTP loopback callbacks are accepted, excluding userinfo/fragments. A resolved decision clears the request; expired/session-bound failures direct the user to restart from the AI client. Granted applications have a distinct confirmed revocation flow.

API responses from an earlier CSRF/session generation are discarded before they can mutate visible state. Token creation additionally checks current authentication before revealing a returned secret. This prevents a late response from reopening a secret dialog after logout/session expiry.

## Version 0.2 verification scope

Project checks: `node --check mailhub/static/app.js`, `python3 scripts/check_ui.py`, `python3 scripts/generate_ui_tokens.py --check`, and the premium skill static auditor. Browser verification must separately cover login, empty lists, account form errors, provider selection popup, reader, pagination, token one-time display, confirmation cancel, session expiry, keyboard, and narrow layout. Static checks are not a claim of completed browser verification.
