from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: str, key: str):
        self.cipher = Fernet(key.encode())
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "mailharbor.sqlite3"
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_uid TEXT NOT NULL,
                    email TEXT NOT NULL, label TEXT NOT NULL, credentials BLOB NOT NULL,
                    folder TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
                    last_sync TEXT, error TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(provider, provider_uid, folder)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    remote_id TEXT NOT NULL, received_at TEXT NOT NULL, payload BLOB NOT NULL,
                    UNIQUE(account_id, remote_id)
                );
                CREATE INDEX IF NOT EXISTS messages_account ON messages(account_id);
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    name TEXT PRIMARY KEY, payload BLOB NOT NULL
                );
            """)
            if "generation" not in {r[1] for r in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
            for column in ("snapshot_days", "snapshot_limit"):
                if column not in {r[1] for r in db.execute("PRAGMA table_info(accounts)")}:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {column} INTEGER")
            db.execute("UPDATE accounts SET status='ready' WHERE status='syncing'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT payload FROM app_settings WHERE name='runtime'").fetchone()
            return self.decrypt(row[0]) if row else {}

    def save_settings(self, values):
        with self.db() as db:
            db.execute("INSERT INTO app_settings VALUES('runtime',?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                       (self.encrypt(values),))

    def check_database(self):
        with self.db() as db:
            return db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def accounts(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("""SELECT a.id,a.provider,a.email,a.label,a.folder,
                a.status,a.last_sync,a.error,a.snapshot_days,a.snapshot_limit,COUNT(m.id) AS message_count
                FROM accounts a LEFT JOIN messages m ON m.account_id=a.id
                GROUP BY a.id ORDER BY a.email,a.id""")]

    def account(self, account_id):
        with self.db() as db:
            r = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not r:
                return None
            result = dict(r)
            result["credentials"] = self.decrypt(result["credentials"])
            return result

    def save_account(self, provider, identity, credentials, label=""):
        folder = credentials.get("folder", "INBOX")
        with self.db() as db:
            previous = db.execute("SELECT id FROM accounts WHERE provider=? AND provider_uid=? AND folder=?",
                                  (provider, identity["provider_uid"], folder)).fetchone()
            if not previous and db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] >= 50:
                raise ValueError("此個人服務最多連接 50 個郵箱；現有帳號仍可重新授權。")
            account_id = previous[0] if previous else secrets.token_hex(12)
            db.execute("""INSERT INTO accounts(id,provider,provider_uid,email,label,credentials,folder)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,provider_uid,folder) DO UPDATE SET
                email=excluded.email,label=excluded.label,credentials=excluded.credentials,
                generation=accounts.generation+1,status='ready',error=NULL""",
                       (account_id, provider, identity["provider_uid"], identity["email"],
                        label or identity["email"], self.encrypt(credentials), folder))
        return next(a for a in self.accounts() if a["id"] == account_id)

    def delete_account(self, account_id):
        with self.db() as db:
            return bool(db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount)

    def set_status(self, account_id, status, error=None, generation=None):
        with self.db() as db:
            db.execute("UPDATE accounts SET status=?,error=? WHERE id=? AND (? IS NULL OR generation=?)",
                       (status, error, account_id, generation, generation))

    def update_credentials(self, account_id, credentials, generation):
        with self.db() as db:
            db.execute("UPDATE accounts SET credentials=? WHERE id=? AND generation=?",
                       (self.encrypt(credentials), account_id, generation))

    def replace_snapshot(self, account_id, messages, credentials, generation=None, sync_days=None, sync_limit=None):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND (? IS NULL OR generation=?)",
                              (account_id, generation, generation)).fetchone():
                return  # Account was removed or reauthorized while the old request ran.
            db.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
            for message in messages:
                remote_id = str(message["remote_id"])
                message_id = hashlib.sha256(f"{account_id}:{remote_id}".encode()).hexdigest()[:32]
                db.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?)",
                           (message_id, account_id, remote_id, message.get("received_at", ""), self.encrypt(message)))
            db.execute("UPDATE accounts SET credentials=?,status='ready',error=NULL,last_sync=?,snapshot_days=?,snapshot_limit=? WHERE id=?",
                       (self.encrypt(credentials), now(), sync_days, sync_limit, account_id))

    def message(self, message_id):
        with self.db() as db:
            r = db.execute("""SELECT m.*,a.email AS account_email,a.provider,a.last_sync,a.snapshot_days,a.snapshot_limit FROM messages m
                JOIN accounts a ON a.id=m.account_id WHERE m.id=?""", (message_id,)).fetchone()
            return self._message(r) if r else None

    def _message(self, r):
        result = self.decrypt(r["payload"])
        result.update(id=r["id"], account_id=r["account_id"], account_email=r["account_email"], provider=r["provider"])
        result["sync"] = {"last_sync": r["last_sync"], "days": r["snapshot_days"], "per_account_limit": r["snapshot_limit"]}
        return result

    def search(self, query="", account_id=None, limit=30, offset=0):
        query = query.casefold().strip()
        with self.db() as db:
            if not query:
                args = (account_id or None, account_id or None)
                total = db.execute("SELECT COUNT(*) FROM messages WHERE (? IS NULL OR account_id=?)", args).fetchone()[0]
                rows = db.execute("""SELECT m.*,a.email AS account_email,a.provider,a.last_sync,a.snapshot_days,a.snapshot_limit FROM messages m
                    JOIN accounts a ON a.id=m.account_id WHERE (? IS NULL OR m.account_id=?)
                    ORDER BY m.received_at DESC,m.id DESC LIMIT ? OFFSET ?""", (*args, limit, offset))
                found = []
                for row in rows:
                    message = self._message(row)
                    message.pop("body", None)
                    found.append(message)
                return {"messages": found, "total": total, "offset": offset, "limit": limit}
            rows = db.execute("""SELECT m.*,a.email AS account_email,a.provider,a.last_sync,a.snapshot_days,a.snapshot_limit FROM messages m
                JOIN accounts a ON a.id=m.account_id WHERE (? IS NULL OR m.account_id=?)
                ORDER BY m.received_at DESC,m.id DESC""", (account_id or None, account_id or None))
            found = []
            total = 0
            for row in rows:
                message = self._message(row)
                haystack = " ".join(str(message.get(k, "")) for k in ("subject", "sender", "recipients", "body"))
                if query and query not in haystack.casefold():
                    continue
                if offset <= total < offset + limit:
                    message.pop("body", None)
                    found.append(message)
                total += 1
            return {"messages": found, "total": total, "offset": offset, "limit": limit}

    def tokens(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,name,created_at,last_used_at FROM tokens ORDER BY created_at DESC")]

    def create_token(self, name):
        secret = "mh_" + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(12)
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] >= 20:
                raise ValueError("最多保留 20 組存取金鑰。")
            db.execute("INSERT INTO tokens VALUES(?,?,?,?,NULL)",
                       (token_id, name, hashlib.sha256(secret.encode()).hexdigest(), now()))
        return {"id": token_id, "name": name, "token": secret}

    def token_valid(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            return bool(db.execute("UPDATE tokens SET last_used_at=? WHERE digest=?", (now(), digest)).rowcount)

    def delete_token(self, token_id):
        with self.db() as db:
            return bool(db.execute("DELETE FROM tokens WHERE id=?", (token_id,)).rowcount)
