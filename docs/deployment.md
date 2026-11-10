# Deploying MailHarbor

From the project directory, run `python3 scripts/setup.py --url https://mail.example.com` with your own public HTTPS origin. Setup prompts for the administrator password and creates a private `.env` file. For an existing installation, reuse its environment and encryption key.

## Start the service

With the bundled Caddy proxy, run `docker compose --profile https up -d --build` after configuring DNS and making ports 80 and 443 available. If your server already has an HTTPS reverse proxy, run `docker compose up -d --build app` and route your hostname to the app on `127.0.0.1:8080`. Preserve the original Host header.

Check service status and health using your own hostname.

```sh
docker compose ps
curl --fail https://mail.example.com/health
```

Sign in as `admin` with the password entered during setup and check the database diagnostics. The administrator manages the global sync settings and the shared Google and Microsoft OAuth application credentials. Each user authorizes their own mailboxes. See the [provider setup instructions](../README.md#google-登入設定).

The sign-in page supports public self-registration. Select **Register a new account**, enter a username and password, then select **Create account** to open an empty member workspace. The member's mailboxes, message cache, API keys and application grants are private to that account. See the [registration instructions](../README.md#註冊與登入).

## Updates and backups

Back up the active environment and take a consistent SQLite backup before updating. Alternatively, stop the app while copying the data volume. Recovery requires both the database and the matching encryption key. Store these backups outside source control with access limited to the operator.

Build and restart the updated source with the same Compose project name, environment and persistent volume. Check container health, the public HTTPS endpoint and authenticated access before switching traffic. The [README](../README.md) includes the settings for each deployment mode.

On the first start after upgrading a single-owner installation, existing mailbox IDs, cached messages, API keys and OAuth grants belong to `admin`. The existing administrator password and client keys continue to work. Verify that the administrator can see the original mailboxes and that a newly registered member starts with an empty mailbox list. To roll back to a single-owner release, restore the matching pre-upgrade database and environment together with the old application so that the old release cannot expose members' data.
