# Deploying MailHarbor

From the project directory, run `python3 scripts/setup.py --url https://mail.example.com` with your own public HTTPS origin. Setup prompts for the administrator password and creates a private `.env` file. For an existing installation, reuse its environment and encryption key.

## Start the service

With the bundled Caddy proxy, run `docker compose --profile https up -d --build` after configuring DNS and making ports 80 and 443 available. If your server already has an HTTPS reverse proxy, run `docker compose up -d --build app` and route your hostname to the app on `127.0.0.1:8080`. Preserve the original Host header.

Check service status and health using your own hostname.

```sh
docker compose ps
curl --fail https://mail.example.com/health
```

Log in to the management page and check the database diagnostics. Connecting a mailbox requires separate provider authorization. See the [OAuth setup guide](oauth.md).

## Updates and backups

Back up the active environment and take a consistent SQLite backup before updating. Alternatively, stop the app while copying the data volume. Recovery requires both the database and the matching encryption key. Store these backups outside source control with access limited to the operator.

Build and restart the updated source with the same Compose project name, environment and persistent volume. Check container health, the public HTTPS endpoint and authenticated access before switching traffic. The [README](../README.md) includes the settings for each deployment mode.
