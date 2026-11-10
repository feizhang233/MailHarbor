import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from mailhub.store import Store


class IdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Fernet.generate_key()
        self.path = Path(self.directory.name) / "mailharbor.sqlite3"

    def open_store(self):
        return Store(self.directory.name, self.key.decode())

    def seed_legacy(self, *, early_schema=False, orphan=False):
        cipher = Fernet(self.key)
        credentials = cipher.encrypt(json.dumps({"refresh_token": "owner-refresh", "client_id": "app"}).encode())
        message = cipher.encrypt(json.dumps({"remote_id": "1", "body": "owner message"}).encode())
        digest = hashlib.sha256(b"synthetic-old-api-key").hexdigest()
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_uid TEXT NOT NULL, email TEXT NOT NULL,
                    label TEXT NOT NULL, credentials BLOB NOT NULL, folder TEXT NOT NULL, status TEXT NOT NULL,
                    last_sync TEXT, error TEXT, UNIQUE(provider,provider_uid,folder));
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    remote_id TEXT NOT NULL, received_at TEXT NOT NULL, payload BLOB NOT NULL, UNIQUE(account_id,remote_id));
                CREATE TABLE tokens (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT);
                CREATE TABLE app_settings (name TEXT PRIMARY KEY, payload BLOB NOT NULL);
            """)
            db.execute("INSERT INTO accounts VALUES('a','gmail','uid','a@example.test','My mailbox',?,'INBOX','ready','date',NULL)", (credentials,))
            db.execute("INSERT INTO messages VALUES('m',?,'1','date',?)", ("missing" if orphan else "a", message))
            db.execute("INSERT INTO tokens VALUES('t','Legacy key',?,'date',NULL)", (digest,))
            db.execute("INSERT INTO app_settings VALUES('runtime',?)", (cipher.encrypt(b'{"sync_days":15}'),))
            if not early_schema:
                db.execute("ALTER TABLE accounts ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE accounts ADD COLUMN snapshot_days INTEGER")
                db.execute("ALTER TABLE accounts ADD COLUMN snapshot_limit INTEGER")
                db.execute("UPDATE accounts SET generation=3,snapshot_days=15,snapshot_limit=70")

    def test_existing_mail_credentials_snapshots_and_keys_stay_with_owner(self):
        self.seed_legacy()
        store = self.open_store()
        member = store.create_user("member", "test-hash")["id"]
        self.assertEqual(store.user_by_username("member")["password_hash"], "test-hash")
        self.assertEqual(store.all_account_ids(), [("owner", "a")])
        self.assertEqual(store.token_owner("synthetic-old-api-key"), "owner")
        self.assertEqual(store.token_access("synthetic-old-api-key"), ("owner", "mail.read"))
        self.assertEqual(store.settings()["sync_days"], 15)
        self.assertEqual(store.account("a")["generation"], 3)
        self.assertEqual(store.account("a")["credentials"]["refresh_token"], "owner-refresh")
        self.assertEqual(store.message("m")["body"], "owner message")
        self.assertEqual(store.message("m")["sync"], {"last_sync": "date", "days": 15, "per_account_limit": 70})
        self.assertEqual([t["id"] for t in store.tokens()], ["t"])
        self.assertEqual(store.accounts(user_id=member), [])
        self.assertEqual(store.tokens(user_id=member), [])
        self.assertFalse(store.delete_token("t", user_id=member))
        self.assertIsNone(store.message("m", user_id=member))
        self.assertEqual(store.search("owner message", account_id="a", user_id=member)["total"], 0)

        identity = {"provider_uid": "uid", "email": "a@example.test"}
        with self.assertRaises(ValueError):
            store.save_account("gmail", identity, {"client_id": "app"}, user_id=member)
        credentials = {"client_id": "app", "refresh_token": "member-refresh"}
        member_account = store.save_account("gmail", identity, credentials, user_id=member)["id"]
        self.assertNotEqual(member_account, "a")
        self.assertEqual(store.save_account("gmail", identity, {"client_id": "app"}, user_id=member)["id"], member_account)
        store.replace_snapshot(member_account, [{"remote_id": "1", "body": "member message"}], credentials, user_id=member)
        member_key = store.create_token("My key", user_id=member)

        # Reopen after migration to verify idempotency and durable separation.
        store = self.open_store()
        self.assertEqual(store.token_owner(member_key["token"]), member)
        self.assertFalse(store.delete_token(member_key["id"]))
        self.assertIsNone(store.account(member_account))
        self.assertEqual(store.search("member message")["total"], 0)
        store.set_status("a", "error", user_id=member)
        store.update_credentials("a", {"wrong": "owner"}, 3, user_id=member)
        store.replace_snapshot("a", [], {}, user_id=member)
        self.assertEqual(store.account("a")["status"], "ready")
        self.assertEqual(store.account("a")["credentials"]["refresh_token"], "owner-refresh")
        self.assertFalse(store.delete_account("a", user_id=member))
        self.assertEqual(store.search()["total"], 1)
        self.assertTrue(store.delete_account("a"))
        self.assertIsNone(store.message("m"))
        self.assertEqual(store.search(user_id=member)["total"], 1)
        with store.db() as db:
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_old_schema_without_snapshot_metadata(self):
        self.seed_legacy(early_schema=True)
        store = self.open_store()
        self.assertEqual(store.account("a")["generation"], 0)
        self.assertIsNone(store.message("m")["sync"]["days"])
        self.assertEqual(store.message("m")["body"], "owner message")

    def test_unfiltered_page_and_count_use_the_same_snapshot(self):
        self.seed_legacy()
        reader, writer = self.open_store(), self.open_store()
        database = reader.db
        with database() as db:
            db.execute("PRAGMA journal_mode=WAL")

        @contextmanager
        def synchronize_between_reads():
            with database() as db:
                def trace(statement):
                    if statement.startswith("SELECT m.*"):
                        writer.replace_snapshot("a", [], {})
                db.set_trace_callback(trace)
                yield db

        with patch.object(reader, "db", synchronize_between_reads):
            page = reader.search()
        self.assertEqual((page["total"], [m["id"] for m in page["messages"]]), (1, ["m"]))
        self.assertEqual(reader.search()["total"], 0)

    def test_failed_migration_rolls_back_whole_transaction(self):
        self.seed_legacy(orphan=True)
        with self.assertRaisesRegex(RuntimeError, "foreign key"):
            self.open_store()
        with sqlite3.connect(self.path) as db:
            self.assertNotIn("owner_id", {r[1] for r in db.execute("PRAGMA table_info(accounts)")})
            self.assertNotIn("owner_id", {r[1] for r in db.execute("PRAGMA table_info(tokens)")})
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name IN ('users','accounts_new')").fetchall())
            self.assertEqual(db.execute("SELECT id FROM accounts").fetchall(), [("a",)])
            db.execute("UPDATE messages SET account_id='a'")
        self.assertEqual(self.open_store().message("m")["body"], "owner message")

    def test_duplicate_name_does_not_replace_credentials(self):
        store = self.open_store()
        first = store.create_user("a@example.test", "first-hash")
        with self.assertRaises(ValueError):
            store.create_user("A@example.test", "second-hash")
        self.assertEqual(store.user_by_username("A@example.test"), {**first, "password_hash": "first-hash"})

    def test_reply_permission_requires_opt_in_by_key_owner(self):
        store = self.open_store()
        member = store.create_user("member", "test-hash")["id"]
        read_key = store.create_token("Read")
        reply_key = store.create_token("Reply", allow_reply=True, user_id=member)
        store = self.open_store()
        self.assertEqual(store.token_access(read_key["token"]), ("owner", "mail.read"))
        self.assertEqual(store.token_access(reply_key["token"]), (member, "mail.read mail.reply"))
        self.assertEqual(store.tokens(user_id=member)[0]["scope"], reply_key["scope"])
        self.assertTrue(store.delete_token(reply_key["id"], user_id=member))
        self.assertIsNone(store.token_access(reply_key["token"]))

    def test_duplicate_sends_are_reserved_across_workers_sync_and_restarts(self):
        self.seed_legacy()
        store, worker = self.open_store(), self.open_store()
        member = store.create_user("member", "test-hash")["id"]
        self.assertEqual(store.begin_reply("m", "key", "私密回覆", user_id=member), ("missing", None))
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda db: db.begin_reply("m", "key", "私密回覆"), (store, worker)))
        self.assertCountEqual(statuses, [("pending", None), ("new", "a")])
        worker.finish_reply("m", "key", "failed", user_id=member)
        self.assertEqual(store.begin_reply("m", "key", "changed"), ("conflict", None))
        self.assertEqual(store.begin_reply("other", "key", "私密回覆"), ("conflict", None))
        store.finish_reply("m", "key", "failed")
        self.assertEqual(worker.begin_reply("m", "key", "私密回覆"), ("new", "a"))
        worker.finish_reply("m", "key", "sent")
        self.assertEqual(store.begin_reply("m", "second", "different"), ("new", "a"))
        store.finish_reply("m", "second", "unknown")
        store.replace_snapshot("a", [], {})
        store = self.open_store()
        self.assertEqual(store.begin_reply("m", "key", "私密回覆"), ("sent", None))
        self.assertEqual(store.begin_reply("m", "second", "different"), ("unknown", None))
        self.assertEqual(store.begin_reply("m", "key", "私密回覆", user_id=member), ("missing", None))
        self.assertNotIn("私密回覆".encode(), self.path.read_bytes())
        store.delete_account("a")
        self.assertEqual(store.begin_reply("m", "key", "私密回覆"), ("missing", None))


if __name__ == "__main__":
    unittest.main()
