"""Synthetic provider fixture for CI only; never connects to a real mailbox."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def create_fixture(data_dir, origin):
    from cryptography.fernet import Fernet
    from mailhub import providers
    from mailhub.config import Settings
    from mailhub.store import Store
    from mailhub.app import create_app

    password = "ci-only-fixture-password"
    salt = b"ci-fixture-salt-1"
    admin_hash = salt.hex() + ":" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64).hex()
    settings = Settings(key=Fernet.generate_key().decode(), admin_hash=admin_hash,
                        public_url=origin, data_dir=str(data_dir), sync_interval_seconds=86400)
    store = Store(data_dir, settings.key)
    credentials = {"email": "fixture@example.test", "username": "fixture@example.test", "password": "synthetic",
                   "host": "imap.example.test", "port": 993, "folder": "INBOX"}
    identity = {"email": "fixture@example.test", "provider_uid": "fixture@example.test"}
    account = store.save_account("custom", identity, credentials, "CI fixture mailbox")

    def fetch(*args, **kwargs):
        return [{"remote_id": "ci-fixture-mail", "subject": "CI fixture <script>window.__mailXss=1</script>",
                 "sender": "Fixture <sender@example.test>", "recipients": "fixture@example.test",
                 "received_at": datetime.now(timezone.utc).isoformat(), "snippet": "Synthetic test message",
                 "body": '<img src=x onerror="window.__mailXss=1">\nCI fixture body. This is synthetic test data.',
                 "unread": True, "attachments": []}], credentials

    providers.fetch_messages = fetch
    providers.probe_imap = lambda supplied: identity
    store.replace_snapshot(account["id"], *fetch())
    token = store.create_token("CI stdio bridge")
    return create_app(settings, store), {"password": password, "token": token["token"], "origin": origin}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    fixture_app, information = create_fixture(args.data_dir, f"http://127.0.0.1:{args.port}")
    fd = os.open(args.data_dir / "fixture.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(information, output)
    import uvicorn
    uvicorn.run(fixture_app, host="127.0.0.1", port=args.port, access_log=False, log_level="error")
