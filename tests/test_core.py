"""Core invariants. Standard-library tests run even before runtime dependencies exist."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mailhub.config import Settings, verify_password, normalize_origin


PASSWORD = "unit-test-password-only"
SALT = b"testing-salt-1234"
ADMIN_HASH = SALT.hex() + ":" + hashlib.scrypt(PASSWORD.encode(), salt=SALT, n=2**14, r=8, p=1, dklen=64).hex()


class ConfigTests(unittest.TestCase):
    def test_password_hash_and_invalid_hash(self):
        self.assertTrue(verify_password(PASSWORD, ADMIN_HASH))
        self.assertFalse(verify_password("wrong", ADMIN_HASH))
        self.assertFalse(verify_password(PASSWORD, "broken"))

    def test_https_required_remotely(self):
        for url in ("http://mail.example.com", "https://name:password@mail.example.com", "https://mail.example.com/path"):
            with self.assertRaises(ValueError):
                Settings(key="test", admin_hash=ADMIN_HASH, public_url=url)
        self.assertTrue(Settings(key="test", admin_hash=ADMIN_HASH, public_url="https://mail.example.com").secure)
        self.assertFalse(Settings(key="test", admin_hash=ADMIN_HASH).secure)

    def test_sync_limits(self):
        for overrides in ({"sync_interval_seconds": 0}, {"sync_days": 0}, {"sync_limit": 100000}):
            with self.assertRaises(ValueError):
                Settings(key="test", admin_hash=ADMIN_HASH, **overrides)

    def test_origin_canonicalization_matches_browser_origin(self):
        for url in ("HTTPS://MAIL.example.com", "https://mail.example.com:443", "https://mail.example.com/"):
            self.assertEqual(normalize_origin(url), "https://mail.example.com")
            self.assertTrue(Settings(key="test", admin_hash=ADMIN_HASH, public_url=url).secure)
        self.assertEqual(normalize_origin("http://LOCALHOST:80"), "http://localhost")
        self.assertEqual(normalize_origin("http://[::1]:8080"), "http://[::1]:8080")


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
HAS_APP = all(importlib.util.find_spec(x) is not None for x in ("cryptography", "fastapi", "httpx", "mcp"))


@unittest.skipUnless(HAS_CRYPTO, "cryptography runtime dependency is not installed")
class StoreTests(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        from mailhub.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, Fernet.generate_key().decode())

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, email="first@example.com"):
        return self.store.save_account("custom", {"email": email, "provider_uid": email},
                                       {"password": "SECRET_PASSWORD", "folder": "INBOX"})

    def mail(self, subject="測試工程文件", body="confidential body 中文搜尋"):
        return {"remote_id": "identical-provider-id", "subject": subject, "body": body,
                "sender": "Test <sender@example.com>", "recipients": "first@example.com",
                "received_at": "2026-10-29T10:00:00+00:00", "snippet": body[:10], "attachments": [], "unread": True}

    def test_encryption_account_isolation_search_and_delete(self):
        a, b = self.add(), self.add("second@example.com")
        self.store.replace_snapshot(a["id"], [self.mail()], {"password": "SECRET_PASSWORD"})
        self.store.replace_snapshot(b["id"], [self.mail("other")], {"password": "other"})
        data = self.store.path.read_bytes()
        self.assertNotIn(b"SECRET_PASSWORD", data)
        self.assertNotIn(b"confidential body", data)
        result = self.store.search("中文搜尋", account_id=a["id"])
        self.assertEqual(result["total"], 1)
        self.assertNotIn("body", result["messages"][0])
        self.assertEqual(len({m["id"] for m in self.store.search()["messages"]}), 2)
        self.assertEqual(self.store.search("' OR 1=1 --")["total"], 0)
        self.store.delete_account(a["id"])
        self.assertEqual(self.store.search()["total"], 1)
        self.assertIsNone(self.store.message(result["messages"][0]["id"]))

    def test_snapshot_replacement_prunes_old_cache(self):
        a = self.add()
        self.store.replace_snapshot(a["id"], [self.mail()], {})
        self.store.replace_snapshot(a["id"], [], {})
        self.assertEqual(self.store.search()["total"], 0)

    def test_reauthorization_blocks_old_sync_and_refresh(self):
        a = self.add()
        old = self.store.account(a["id"])
        self.store.save_account("custom", {"email": a["email"], "provider_uid": a["email"]},
                                {"password": "NEW_SECRET", "folder": "INBOX"})
        self.store.replace_snapshot(a["id"], [self.mail()], {"password": "OLD"}, old["generation"])
        self.store.update_credentials(a["id"], {"password": "OLD"}, old["generation"])
        self.assertEqual(self.store.account(a["id"])["credentials"]["password"], "NEW_SECRET")
        self.assertEqual(self.store.search()["total"], 0)

    def test_deleted_account_cannot_be_resurrected(self):
        a = self.add()
        self.store.delete_account(a["id"])
        self.store.replace_snapshot(a["id"], [self.mail()], {})
        self.assertEqual(self.store.accounts(), [])

    def test_tokens_hashed_revocable_and_never_relisted(self):
        token = self.store.create_token("Laptop")
        self.assertTrue(self.store.token_valid(token["token"]))
        self.assertFalse(self.store.token_valid("wrong"))
        self.assertNotIn(token["token"].encode(), self.store.path.read_bytes())
        self.assertNotIn("token", self.store.tokens()[0])
        self.store.delete_token(token["id"])
        self.assertFalse(self.store.token_valid(token["token"]))

    def test_empty_search_decrypts_only_requested_page(self):
        a = self.add()
        mails = [dict(self.mail(), remote_id=str(i)) for i in range(20)]
        self.store.replace_snapshot(a["id"], mails, {}, sync_days=30, sync_limit=200)
        with patch.object(self.store, "decrypt", wraps=self.store.decrypt) as decrypt:
            result = self.store.search(limit=3, offset=4)
            self.assertEqual(result["total"], 20)
            self.assertEqual(len(result["messages"]), 3)
            self.assertEqual(decrypt.call_count, 3)
            self.assertEqual(result["messages"][0]["sync"]["days"], 30)

    def test_settings_encrypted_and_snapshot_range_not_relabelled(self):
        a = self.add()
        self.store.replace_snapshot(a["id"], [self.mail()], {}, sync_days=30, sync_limit=200)
        self.store.save_settings({"google_client_secret": "secret-DO-NOT-LEAK", "sync_days": 7})
        self.assertNotIn(b"secret-DO-NOT-LEAK", self.store.path.read_bytes())
        self.assertEqual(self.store.settings()["sync_days"], 7)
        self.assertEqual(self.store.accounts()[0]["snapshot_days"], 30)


@unittest.skipUnless(HAS_APP, "FastAPI/MCP/httpx/cryptography runtime dependencies are not installed")
class ApplicationTests(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        from fastapi.testclient import TestClient
        from mailhub.app import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(key=Fernet.generate_key().decode(), admin_hash=ADMIN_HASH, data_dir=self.tmp.name,
                                 google_client_id="test-client", google_client_secret="test-secret")
        self.app = create_app(self.settings)
        self.sync_patch = patch("mailhub.providers.fetch_messages", side_effect=lambda provider, credentials, settings, **kwargs: ([], credentials))
        self.sync_patch.start()
        self.client = TestClient(self.app, base_url="http://localhost:8080")
        self.client.__enter__()
        self.headers = {"Origin": "http://localhost:8080"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.sync_patch.stop()
        self.tmp.cleanup()

    def login(self):
        response = self.client.post("/api/login", json={"password": PASSWORD}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.headers["X-CSRF-Token"] = response.json()["csrf_token"]

    def test_admin_auth_csrf_session_and_token_separation(self):
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"password": PASSWORD}).status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/api/accounts").status_code, 200)
        self.assertEqual(self.client.post("/api/tokens", json={"name": "Test"}, headers={"Origin": "http://localhost:8080"}).status_code, 403)
        token = self.client.post("/api/tokens", json={"name": "Test"}, headers=self.headers).json()
        self.assertEqual(self.client.post("/api/logout", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/accounts", headers={"Authorization": "Bearer " + token["token"]}).status_code, 401)

    def test_mcp_auth_tool_list_and_revocation(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.login()
        token = self.client.post("/api/tokens", json={"name": "Codex"}, headers=self.headers).json()
        headers = {"Authorization": "Bearer " + token["token"], "Accept": "application/json, text/event-stream"}
        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = response.json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"list_accounts", "get_sync_status", "search_emails", "read_email"})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        self.client.delete("/api/tokens/" + token["id"], headers=self.headers)
        self.assertEqual(self.client.post("/mcp", headers=headers, json={}).status_code, 401)

    def test_host_origin_body_and_unconfigured_oauth(self):
        self.assertEqual(self.client.get("/health", headers={"Host": "attacker.example"}).status_code, 400)
        self.login()
        self.assertEqual(self.client.post("/api/tokens", json={"name": "No"}, headers={**self.headers, "Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/tokens", content=b"x" * 70000, headers=self.headers).status_code, 413)
        self.assertEqual(self.client.post("/api/oauth/microsoft/start", headers=self.headers).status_code, 503)

    def test_oauth_state_is_session_bound_and_single_use(self):
        self.login()
        def start():
            result = self.client.post("/api/oauth/gmail/start", headers=self.headers).json()
            return parse_qs(urlsplit(result["url"]).query)["state"][0]
        old_state = start()
        self.client.post("/api/logout", headers=self.headers)
        self.login()
        with patch("mailhub.providers.oauth_exchange", return_value={"refresh_token": "test-token"}) as exchange, \
             patch("mailhub.providers.identify_oauth", return_value={"email": "test@example.com", "provider_uid": "test"}):
            response = self.client.get("/api/oauth/gmail/callback", params={"state": old_state, "code": "code"}, follow_redirects=False)
            self.assertEqual(response.headers["location"], "/?connection_error=1")
            exchange.assert_not_called()
            current = start()
            response = self.client.get("/api/oauth/gmail/callback", params={"state": current, "code": "code"}, follow_redirects=False)
            self.assertEqual(response.headers["location"], "/?connected=1")
            response = self.client.get("/api/oauth/gmail/callback", params={"state": current, "code": "code"}, follow_redirects=False)
            self.assertEqual(response.headers["location"], "/?connection_error=1")
            self.assertEqual(exchange.call_count, 1)

    def test_preset_host_cannot_diverge_from_sync_host(self):
        self.login()
        with patch("mailhub.providers.probe_imap") as probe:
            response = self.client.post("/api/accounts/imap", headers=self.headers,
                json={"provider": "icloud", "email": "a@example.com", "password": "app-password", "host": "attacker.example"})
            self.assertEqual(response.status_code, 400)
            probe.assert_not_called()

    def test_settings_secret_retention_clearing_persistence_and_csrf(self):
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        self.login()
        payload = {"sync_interval_seconds": 120, "sync_days": 7, "sync_limit": 50,
                   "google_client_id": "new-client", "google_client_secret": "new-secret-keep-private"}
        self.assertEqual(self.client.post("/api/settings", json=payload).status_code, 403)
        response = self.client.post("/api/settings", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("new-secret-keep-private", response.text)
        self.assertTrue(response.json()["google"]["secret_configured"])
        payload["google_client_secret"] = ""
        self.assertEqual(self.client.post("/api/settings", headers=self.headers, json=payload).status_code, 200)
        self.assertEqual(self.app.state.store.settings()["google_client_secret"], "new-secret-keep-private")
        from mailhub.app import create_app
        restarted = create_app(self.settings)
        self.assertEqual(restarted.state.settings.sync_days, 7)
        self.assertEqual(restarted.state.settings.google_client_secret, "new-secret-keep-private")
        payload["clear_google_secret"] = True
        response = self.client.post("/api/settings", headers=self.headers, json=payload)
        self.assertFalse(response.json()["google"]["secret_configured"])

    def test_rest_bearer_only_and_openapi_excludes_management(self):
        self.login()
        self.assertEqual(self.client.get("/v1/accounts").status_code, 401)
        token = self.client.post("/api/tokens", headers=self.headers, json={"name": "API"}).json()["token"]
        response = self.client.get("/v1/accounts", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accounts"], [])
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(set(schema["paths"]), {"/v1/accounts", "/v1/status", "/v1/messages", "/v1/messages/{message_id}"})
        self.assertIn("MailHarborKey", schema["components"]["securitySchemes"])
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "Bearer " + token}).status_code, 200)
        self.client.post("/api/logout", headers=self.headers)
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "Bearer " + token}).status_code, 401)

    def test_setup_and_diagnostics_do_not_claim_external_connection(self):
        self.login()
        setup = self.client.get("/api/setup").json()
        self.assertFalse(setup["ready"])
        self.assertTrue(setup["ai"]["oauth_supported"])
        self.assertEqual(setup["counts"]["accounts"], 0)
        diagnostics = self.client.post("/api/diagnostics", headers=self.headers).json()
        self.assertTrue(any(c["id"] == "database" and c["status"] == "pass" for c in diagnostics["checks"]))
        self.assertIn("未驗證", next(c["detail"] for c in diagnostics["checks"] if c["id"] == "origin"))


if __name__ == "__main__":
    unittest.main()
