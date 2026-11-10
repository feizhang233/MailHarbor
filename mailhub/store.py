from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .config import READ_REPLY_SCOPE, READ_SCOPE


_ACCOUNT_SUMMARY = """SELECT a.id,a.provider,a.email,a.label,a.folder,
    a.status,a.last_sync,a.error,a.snapshot_days,a.snapshot_limit,COUNT(m.id) AS message_count
    FROM accounts a LEFT JOIN messages m ON m.account_id=a.id"""

_ACCOUNT_SCHEMA = """(
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
    provider TEXT NOT NULL, provider_uid TEXT NOT NULL, email TEXT NOT NULL,
    label TEXT NOT NULL, credentials BLOB NOT NULL, folder TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready', last_sync TEXT, error TEXT,
    generation INTEGER NOT NULL DEFAULT 0, snapshot_days INTEGER, snapshot_limit INTEGER,
    UNIQUE(owner_id, provider, provider_uid, folder)
)"""

_MESSAGE_COLUMNS = "m.*,a.email AS account_email,a.provider,a.last_sync,a.snapshot_days,a.snapshot_limit"
_MESSAGE_SCOPE = """ FROM messages m JOIN accounts a ON a.id=m.account_id
    WHERE a.owner_id=? AND (? IS NULL OR m.account_id=?)"""


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: str, key: str):
        self.cipher = Fernet(key.encode())
        self.reply_digest_key = hmac.digest(key.encode(), b"MailHarbor reply idempotency", "sha256")
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "mailharbor.sqlite3"
        self.lock = threading.RLock()
        with self.db() as db:
            # Rebuild the legacy UNIQUE constraint without cascading deletions to messages.
            # Foreign keys remain enabled on all application connections.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT COLLATE NOCASE UNIQUE, password_hash TEXT,
                CHECK ((id='owner' AND username IS NULL AND password_hash IS NULL) OR
                       (id!='owner' AND username IS NOT NULL AND password_hash IS NOT NULL)))""")
            db.execute("INSERT INTO users(id) VALUES('owner') ON CONFLICT(id) DO NOTHING")
            columns = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
            legacy_accounts = bool(columns and "owner_id" not in columns)
            if legacy_accounts:
                db.execute("CREATE TABLE accounts_new " + _ACCOUNT_SCHEMA)
                existing = "id,provider,provider_uid,email,label,credentials,folder,status,last_sync,error"
                optional = ("generation", "snapshot_days", "snapshot_limit")
                defaults = ("0", "NULL", "NULL")
                select = ",".join(col if col in columns else default for col, default in zip(optional, defaults))
                db.execute(f"""INSERT INTO accounts_new(owner_id,{existing},generation,snapshot_days,snapshot_limit)
                    SELECT 'owner',{existing},{select} FROM accounts""")
                db.execute("DROP TABLE accounts")
                db.execute("ALTER TABLE accounts_new RENAME TO accounts")
            else:
                db.execute("CREATE TABLE IF NOT EXISTS accounts " + _ACCOUNT_SCHEMA)
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                remote_id TEXT NOT NULL, received_at TEXT NOT NULL, payload BLOB NOT NULL,
                UNIQUE(account_id, remote_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS messages_account ON messages(account_id)")
            # Snapshots replace every message row. Bind receipts to the mailbox so
            # a sync cannot remove the record of an accepted or uncertain send.
            db.execute("""CREATE TABLE IF NOT EXISTS reply_requests (
                owner_id TEXT NOT NULL REFERENCES users(id),
                request_id TEXT NOT NULL, message_id TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                body_digest BLOB NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(owner_id,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS tokens (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL, last_used_at TEXT, owner_id TEXT NOT NULL REFERENCES users(id),
                scope TEXT NOT NULL DEFAULT 'mail.read')""")
            token_columns = {r[1] for r in db.execute("PRAGMA table_info(tokens)")}
            legacy_tokens = "owner_id" not in token_columns
            if legacy_tokens:
                db.execute("ALTER TABLE tokens ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'owner' REFERENCES users(id)")
            if "scope" not in token_columns:
                db.execute("ALTER TABLE tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'mail.read'")
            db.execute("CREATE INDEX IF NOT EXISTS tokens_owner ON tokens(owner_id)")
            db.execute("CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            if (legacy_accounts or legacy_tokens) and db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Mailbox ownership migration failed its foreign key check")
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

    def create_user(self, username, password_hash):
        user_id = secrets.token_hex(16)
        with self.db() as db:
            inserted = db.execute("""INSERT INTO users(id,username,password_hash) VALUES(?,?,?)
                ON CONFLICT(username) DO NOTHING""", (user_id, username, password_hash))
            if not inserted.rowcount:
                raise ValueError("此使用者名稱已被使用。")
        return {"id": user_id, "username": username}

    def user_by_username(self, username):
        with self.db() as db:
            row = db.execute("SELECT id,username,password_hash FROM users WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    def all_account_ids(self):
        with self.db() as db:
            return [(r["owner_id"], r["id"]) for r in db.execute("SELECT owner_id,id FROM accounts")]

    def accounts(self, *, user_id="owner"):
        with self.db() as db:
            return [dict(r) for r in db.execute(_ACCOUNT_SUMMARY + " WHERE a.owner_id=? GROUP BY a.id ORDER BY a.email,a.id", (user_id,))]

    def account(self, account_id, *, user_id="owner"):
        with self.db() as db:
            r = db.execute("SELECT * FROM accounts WHERE id=? AND owner_id=?", (account_id, user_id)).fetchone()
            if not r:
                return None
            result = dict(r)
            result["credentials"] = self.decrypt(result["credentials"])
            return result

    def save_account(self, provider, identity, credentials, label="", *, user_id="owner"):
        folder = credentials.get("folder", "INBOX")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id,credentials FROM accounts WHERE owner_id=? AND provider=? AND provider_uid=? AND folder=?",
                                  (user_id, provider, identity["provider_uid"], folder)).fetchone()
            if provider in ("gmail", "microsoft") and not credentials.get("refresh_token"):
                stored = self.decrypt(previous["credentials"]) if previous else {}
                if (not stored.get("refresh_token") or not credentials.get("client_id")
                        or stored.get("client_id") != credentials["client_id"]):
                    raise ValueError("未取得背景同步授權，請重新授權此郵箱。")
                credentials = {**credentials, "refresh_token": stored["refresh_token"]}
            if not previous and db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (user_id,)).fetchone()[0] >= 50:
                raise ValueError("此個人服務最多連接 50 個郵箱；現有帳號仍可重新授權。")
            account_id = previous["id"] if previous else secrets.token_hex(12)
            db.execute("""INSERT INTO accounts(id,owner_id,provider,provider_uid,email,label,credentials,folder)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,provider,provider_uid,folder) DO UPDATE SET
                email=excluded.email,label=excluded.label,credentials=excluded.credentials,
                generation=accounts.generation+1,status='ready',error=NULL""",
                       (account_id, user_id, provider, identity["provider_uid"], identity["email"],
                        label or identity["email"], self.encrypt(credentials), folder))
            row = db.execute(_ACCOUNT_SUMMARY + " WHERE a.id=? GROUP BY a.id", (account_id,)).fetchone()
            return dict(row)

    def delete_account(self, account_id, *, user_id="owner"):
        with self.db() as db:
            return bool(db.execute("DELETE FROM accounts WHERE id=? AND owner_id=?", (account_id, user_id)).rowcount)

    def set_status(self, account_id, status, error=None, generation=None, *, user_id="owner"):
        with self.db() as db:
            db.execute("UPDATE accounts SET status=?,error=? WHERE id=? AND owner_id=? AND (? IS NULL OR generation=?)",
                       (status, error, account_id, user_id, generation, generation))

    def update_credentials(self, account_id, credentials, generation, *, user_id="owner"):
        with self.db() as db:
            db.execute("UPDATE accounts SET credentials=? WHERE id=? AND owner_id=? AND generation=?",
                       (self.encrypt(credentials), account_id, user_id, generation))

    def replace_snapshot(self, account_id, messages, credentials, generation=None, sync_days=None, sync_limit=None, *, user_id="owner"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND owner_id=? AND (? IS NULL OR generation=?)",
                              (account_id, user_id, generation, generation)).fetchone():
                return  # Account was removed or reauthorized while the old request ran.
            db.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
            for message in messages:
                remote_id = str(message["remote_id"])
                message_id = hashlib.sha256(f"{account_id}:{remote_id}".encode()).hexdigest()[:32]
                db.execute("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?)",
                           (message_id, account_id, remote_id, message.get("received_at", ""), self.encrypt(message)))
            db.execute("UPDATE accounts SET credentials=?,status='ready',error=NULL,last_sync=?,snapshot_days=?,snapshot_limit=? WHERE id=?",
                       (self.encrypt(credentials), now(), sync_days, sync_limit, account_id))

    def message(self, message_id, *, user_id="owner"):
        with self.db() as db:
            r = db.execute(f"""SELECT {_MESSAGE_COLUMNS} FROM messages m
                JOIN accounts a ON a.id=m.account_id WHERE m.id=? AND a.owner_id=?""", (message_id, user_id)).fetchone()
            return self._message(r) if r else None

    def begin_reply(self, message_id, request_id, body, *, user_id="owner"):
        """Return the reservation state and, only for a new send, the mailbox ID."""
        content = json.dumps([user_id, request_id, body], ensure_ascii=False).encode()
        fingerprint = hmac.digest(self.reply_digest_key, content, "sha256")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""SELECT message_id,body_digest,status FROM reply_requests
                WHERE owner_id=? AND request_id=?""", (user_id, request_id)).fetchone()
            if previous:
                if previous["message_id"] != message_id or not hmac.compare_digest(previous["body_digest"], fingerprint):
                    return "conflict", None
                if previous["status"] != "failed":
                    return previous["status"], None
            account = db.execute("""SELECT m.account_id FROM messages m JOIN accounts a ON a.id=m.account_id
                WHERE m.id=? AND a.owner_id=?""", (message_id, user_id)).fetchone()
            if not account:
                return "missing", None
            db.execute("""INSERT INTO reply_requests(owner_id,request_id,message_id,account_id,body_digest,status)
                VALUES(?,?,?,?,?,'pending') ON CONFLICT(owner_id,request_id) DO UPDATE SET status='pending'""",
                       (user_id, request_id, message_id, account["account_id"], fingerprint))
        return "new", account["account_id"]

    def finish_reply(self, message_id, request_id, status, *, user_id="owner"):
        # Only a definite rejection may be retried. A crash before completion
        # leaves the reservation pending, even across service restarts.
        if status not in {"failed", "unknown", "sent"}:
            raise ValueError("Invalid reply outcome")
        with self.db() as db:
            db.execute("""UPDATE reply_requests SET status=? WHERE owner_id=? AND request_id=?
                AND message_id=? AND status='pending'""", (status, user_id, request_id, message_id))

    def _message(self, r):
        result = self.decrypt(r["payload"])
        result.update(id=r["id"], account_id=r["account_id"], account_email=r["account_email"], provider=r["provider"])
        result["sync"] = {"last_sync": r["last_sync"], "days": r["snapshot_days"], "per_account_limit": r["snapshot_limit"]}
        return result

    def search(self, query="", account_id=None, limit=30, offset=0, *, user_id="owner"):
        query = query.casefold().strip()
        args = (user_id, account_id or None, account_id or None)
        select = f"SELECT {_MESSAGE_COLUMNS}" + _MESSAGE_SCOPE + " ORDER BY m.received_at DESC,m.id DESC"
        with self.db() as db:
            if query:
                total = 0
                rows = db.execute(select, args)
            else:
                # Both reads must describe the same snapshot during a concurrent sync.
                db.execute("BEGIN")
                total = db.execute("SELECT COUNT(*)" + _MESSAGE_SCOPE, args).fetchone()[0]
                rows = db.execute(select + " LIMIT ? OFFSET ?", (*args, limit, offset))
            found = []
            for row in rows:
                message = self._message(row)
                if query:
                    haystack = " ".join(str(message.get(k, "")) for k in ("subject", "sender", "recipients", "body"))
                    if query not in haystack.casefold():
                        continue
                    total += 1
                    if not offset < total <= offset + limit:
                        continue
                message.pop("body", None)
                found.append(message)
        return {"messages": found, "total": total, "offset": offset, "limit": limit}

    def tokens(self, *, user_id="owner"):
        with self.db() as db:
            return [dict(r) for r in db.execute("""SELECT id,name,created_at,last_used_at,scope FROM tokens
                WHERE owner_id=? ORDER BY created_at DESC""", (user_id,))]

    def create_token(self, name, *, user_id="owner", allow_reply=False):
        secret = "mh_" + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(12)
        scope = READ_REPLY_SCOPE if allow_reply is True else READ_SCOPE
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM tokens WHERE owner_id=?", (user_id,)).fetchone()[0] >= 20:
                raise ValueError("最多保留 20 組存取金鑰。")
            db.execute("INSERT INTO tokens(id,owner_id,name,digest,created_at,scope) VALUES(?,?,?,?,?,?)",
                       (token_id, user_id, name, hashlib.sha256(secret.encode()).hexdigest(), now(), scope))
        return {"id": token_id, "name": name, "token": secret, "scope": scope}

    def token_access(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            row = db.execute("""UPDATE tokens SET last_used_at=? WHERE digest=?
                RETURNING owner_id,scope""", (now(), digest)).fetchone()
            if not row or row["scope"] not in (READ_SCOPE, READ_REPLY_SCOPE):
                return None
            return row["owner_id"], row["scope"]

    def token_owner(self, token):
        access = self.token_access(token)
        return access[0] if access else None

    def token_valid(self, token):
        return self.token_owner(token) is not None

    def delete_token(self, token_id, *, user_id="owner"):
        with self.db() as db:
            return bool(db.execute("DELETE FROM tokens WHERE id=? AND owner_id=?", (token_id, user_id)).rowcount)
