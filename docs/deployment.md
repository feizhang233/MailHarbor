# Production deployment — 2026-10-29

- Website: https://mailharbor.feizhang233.com
- Repository: https://github.com/feizhang233/MailHarbor (private)
- Verified application commit: `66113b7a2650e9cd634340bb2404e0d88c462043`. Subsequent documentation updates do not change the deployed application files.
- GitHub Actions: https://github.com/feizhang233/MailHarbor/actions/runs/40962534626 — Python 3.12, Python 3.13 and browser/MCP jobs passed.

## Runtime layout

VPS SSH alias: `vultr-tokyo-fei` (service owner `fei`).

- Project: `/home/fei/Work/MailHarbor`
- Active source: `current` → `releases/20261029-66113b7`
- Production environment: `shared/.env`, mode 0600; release `.env` is a symlink to it.
- Compose project: `mailharbor`; only the `app` service runs.
- Container: `mailharbor-app-1`, Python 3.13, application user `mailhub`, restart policy `unless-stopped`.
- Listener: `127.0.0.1:8080`; no public 8080 binding.
- Persistent database: named volume `mailharbor_mailhub_data`.
- Dedicated ingress: `mailharbor-tunnel.service`, enabled for startup and running as `fei`.
- Tunnel configuration: `/home/fei/.cloudflared/config-mailharbor.yml`; hostname forwards to `http://127.0.0.1:8080`, followed by a 404 fallback.
- Tunnel ID: `fa183e8b-c91c-47b2-97cc-6592b8ce34a7`.

Existing proxy/tunnel configurations and the existing service on port 443 were preserved. The Compose HTTPS/Caddy profile is not used on this VPS.

The generated administrator password and an environment backup were saved in the local workspace under ignored `data/deployment/`, with owner-only permissions. No password, encryption key or mailbox credential is stored in Git. Keep the encryption key when updating the service; do not run setup again over this deployment.

## Executed production checks

- Docker build/start and container health check passed.
- All 94 Python tests passed in an isolated read-only container using the production image, without attaching the production database or environment.
- Public DNS/HTTPS and `/health` passed, with certificate verification enabled.
- Unauthenticated management and MCP requests were rejected.
- Administrator login, Secure/HttpOnly/SameSite cookie, settings retrieval and database diagnostics passed.
- OAuth resource discovery advertises the exact production MCP URL.
- A temporary deployment token successfully accessed read-only REST, initialized MCP, listed the four read-only tools and called sync status.
- That token was revoked and removed; both REST and MCP rejected it afterward. Administrator logout also invalidated access.
- Source checksums were compared with the uploaded application files.

No mailbox has been connected yet. Google/Microsoft OAuth applications, real IMAP/provider refresh and unread-flag behavior, and the user's actual AI client installation still need their own verification.

## Operations

```sh
cd /home/fei/Work/MailHarbor/current
docker compose -p mailharbor ps
docker compose -p mailharbor logs --tail=100 app
systemctl status mailharbor-tunnel.service
curl --fail -H 'Host: mailharbor.feizhang233.com' http://127.0.0.1:8080/health
```

For an update, extract a reviewed Git commit into a new release directory, link its `.env` to `../../shared/.env`, and build/start only `app` with the same `-p mailharbor`. Check health and the public authenticated paths before changing `current`. Preserve the named volume and shared environment. Do not use `docker compose down -v`.

For backup, preserve `shared/.env` and take a consistent SQLite backup (or stop the app while copying its volume). The database and encryption key are both required for recovery.
