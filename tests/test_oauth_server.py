"""OAuth state-machine and ASGI integration tests; never contact external services."""
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path

from mailhub.oauth_server import OAuthError, OAuthServer, SCOPE, digest, pkce_challenge, valid_redirect_uri, valid_scope


class OAuthValidationTests(unittest.TestCase):
    def test_exact_secure_redirects(self):
        for uri in ("https://chatgpt.com/connector/oauth", "http://127.0.0.1:3999/callback", "http://[::1]:3999/callback", "http://localhost:3999/callback?desktop=1"):
            self.assertTrue(valid_redirect_uri(uri), uri)
        for uri in ("http://attacker.example/callback", "javascript:alert(1)", "file:///tmp/callback", "https://a:b@example.com/callback", "https://good.example/#frag", "https://good.example/cb?code=foo", "https://good.example/cb?state=foo", "https://good.example/cb\r\nHeader:1", "https://good.example\\@evil.example/cb", "https://example.com:bad/cb", "https://*.example.com/cb", None, []):
            self.assertFalse(valid_redirect_uri(uri), repr(uri))

    def test_scope_does_not_escalate(self):
        self.assertTrue(valid_scope("mail.read"))
        for scope in ("", "openid", "mail.read mail.write", "mail.read mail.read", [], None):
            self.assertFalse(valid_scope(scope))

    def test_redirect_rejects_invalid_unicode(self):
        self.assertFalse(valid_redirect_uri("https://example.com/\ud800"))

    def test_pkce_matches_rfc7636(self):
        self.assertEqual(pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


HAS_RUNTIME = all(importlib.util.find_spec(x) is not None for x in ("fastapi", "httpx", "cryptography"))


class _SQLiteTestStore:
    """Real SQLite transactions with a JSON cipher double; not an encryption test."""
    def __init__(self, directory):
        self.path = Path(directory) / "test.sqlite"
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def encrypt(self, value):
        return json.dumps(value).encode()

    def decrypt(self, value):
        return json.loads(value)


class OAuthStateMachineTests(unittest.TestCase):
    """Protocol decisions and transaction boundaries, without HTTP/cipher dependencies."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _SQLiteTestStore(self.tmp.name)
        self.settings = SimpleNamespace(public_url="https://mail.example.com")
        with patch.object(OAuthServer, "_build_router", return_value=None):
            self.server = OAuthServer(self.settings, self.store)
        self.redirect = "https://ai.example.com/callback"
        self.client = self.server.register({"redirect_uris": [self.redirect], "client_name": "Test"})["client_id"]
        self.verifier = "v" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def get_code(self):
        request_id = self.server.begin_authorization({"client_id": self.client, "redirect_uri": self.redirect,
            "resource": self.server.resource, "response_type": "code", "code_challenge_method": "S256",
            "code_challenge": pkce_challenge(self.verifier), "scope": SCOPE, "state": "state"})
        self.server.consent_details(request_id, "owner")
        response = self.server.decide_consent(request_id, True, "owner")
        return parse_qs(urlsplit(response["redirect_url"]).query)["code"][0]

    def get_pair(self, code=None):
        return self.server.exchange({"grant_type": "authorization_code", "client_id": self.client,
            "code": code or self.get_code(), "code_verifier": self.verifier,
            "redirect_uri": self.redirect, "resource": self.server.resource})

    def test_refresh_replay_revocation_is_committed(self):
        pair = self.get_pair()
        self.assertTrue(self.server.validate_access_token(pair["access_token"]))
        request = {"grant_type": "refresh_token", "client_id": self.client,
                   "refresh_token": pair["refresh_token"], "resource": self.server.resource}
        newer = self.server.exchange(request)
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))
        self.assertTrue(self.server.validate_access_token(newer["access_token"]))
        with self.assertRaises(OAuthError):
            self.server.exchange(request)
        self.assertFalse(self.server.validate_access_token(newer["access_token"]))

    def test_code_replay_revocation_is_committed(self):
        code = self.get_code()
        pair = self.get_pair(code)
        with self.assertRaises(OAuthError):
            self.get_pair(code)
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))

    def test_invalid_requests_consume_committed_limits(self):
        with self.store.db() as db:
            db.execute("INSERT INTO oauth_limits VALUES(?,?,119)", ("token-client:" + digest(self.client), time.time()))
        with self.assertRaises(OAuthError) as first:
            self.get_pair("unknown-code")
        self.assertEqual(first.exception.error, "invalid_grant")
        with self.assertRaises(OAuthError) as second:
            self.get_pair("unknown-code")
        self.assertEqual(second.exception.status, 429)

    def test_invalid_unicode_client_name_is_safe_error(self):
        with self.assertRaises(OAuthError) as caught:
            self.server.register({"redirect_uris": [self.redirect], "client_name": "\ud800"})
        self.assertEqual(caught.exception.error, "invalid_client_metadata")

    def test_prune_preserves_granted_clients_but_removes_old_unconsented(self):
        self.get_pair()
        extra = self.server.register({"redirect_uris": [self.redirect]})["client_id"]
        with self.store.db() as db:
            db.execute("UPDATE oauth_clients SET created_at=?,last_used_at=?", (time.time() - 5 * 3600, time.time() - 5 * 3600))
            self.server._prune(db, time.time())
            self.assertIsNotNone(db.execute("SELECT id FROM oauth_clients WHERE id=?", (self.client,)).fetchone())
            self.assertIsNone(db.execute("SELECT id FROM oauth_clients WHERE id=?", (extra,)).fetchone())


@unittest.skipUnless(HAS_RUNTIME, "OAuth ASGI/storage dependencies are not installed")
class OAuthServerTests(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from mailhub.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name, Fernet.generate_key().decode())
        self.settings = SimpleNamespace(public_url="https://mail.example.com")
        self.server = OAuthServer(self.settings, self.store,
                                  owner_session=lambda request: {"csrf": "csrf-test"} if request.cookies.get("mailharbor_session") in {"owner-one", "owner-two"} else None)
        app = FastAPI()
        app.include_router(self.server.router)
        self.client = TestClient(app, base_url=self.settings.public_url)
        self.client.__enter__()
        self.client.cookies.set("mailharbor_session", "owner-one")
        self.headers = {"Origin": self.settings.public_url, "X-CSRF-Token": "csrf-test"}
        self.redirect = "https://ai.example.com/oauth/callback?mode=mcp"
        self.registered = self.server.register({"client_name": "Test AI", "redirect_uris": [self.redirect]})
        self.client_id = self.registered["client_id"]
        self.verifier = "v" * 64

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def parameters(self, **changes):
        result = {"client_id": self.client_id, "redirect_uri": self.redirect, "response_type": "code",
                  "resource": self.server.resource, "scope": SCOPE, "state": "opaque-state",
                  "code_challenge": pkce_challenge(self.verifier), "code_challenge_method": "S256"}
        result.update(changes)
        return result

    def begin(self, **changes):
        response = self.client.get("/oauth/authorize", params=self.parameters(**changes), follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        return parse_qs(urlsplit(response.headers["location"]).query)["oauth_request"][0]

    def consent(self, request_id, approve=True):
        details = self.client.get("/api/oauth/consent", params={"request_id": request_id})
        self.assertEqual(details.status_code, 200, details.text)
        response = self.client.post("/api/oauth/consent", json={"request_id": request_id, "approve": approve}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return parse_qs(urlsplit(response.json()["redirect_url"]).query)

    def authorized_code(self):
        return self.consent(self.begin())["code"][0]

    def exchange(self, code, **changes):
        data = {"grant_type": "authorization_code", "client_id": self.client_id, "code": code,
                "code_verifier": self.verifier, "redirect_uri": self.redirect, "resource": self.server.resource}
        data.update(changes)
        return self.client.post("/oauth/token", data=data)

    def token_pair(self):
        response = self.exchange(self.authorized_code())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_metadata_and_dynamic_registration(self):
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["issuer"], self.settings.public_url)
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertNotIn("client_id_metadata_document_supported", metadata)
        protected = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(protected["resource"], self.server.resource)
        response = self.client.post("/oauth/register", json={"client_name": "Desktop", "redirect_uris": ["http://127.0.0.1:5001/cb"], "token_endpoint_auth_method": "none"})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("client_secret", response.json())
        self.assertIn("no-store", response.headers["cache-control"])

    def test_registration_rejects_unsafe_metadata_without_500(self):
        for changes in ({"redirect_uris": ["http://remote.example/cb"]}, {"token_endpoint_auth_method": "client_secret_post"}, {"grant_types": [{"invalid": "object"}]}, {"scope": "mail.write"}, {"client_name": {"invalid": "object"}}):
            response = self.client.post("/oauth/register", json={"redirect_uris": [self.redirect], **changes})
            self.assertEqual(response.status_code, 400, response.text)

    def test_registration_cap_and_rate_limit(self):
        with patch("mailhub.oauth_server.MAX_CLIENTS", 1):
            response = self.client.post("/oauth/register", json={"redirect_uris": [self.redirect]})
            self.assertEqual(response.status_code, 429)

    def test_unconsented_old_registration_pruned(self):
        with self.store.db() as db:
            db.execute("UPDATE oauth_clients SET created_at=?,last_used_at=?", (time.time() - 5 * 3600, time.time() - 5 * 3600))
        with patch("mailhub.oauth_server.MAX_CLIENTS", 1):
            response = self.client.post("/oauth/register", json={"redirect_uris": [self.redirect]})
            self.assertEqual(response.status_code, 201, response.text)

    def test_invalid_exchange_still_consumes_rate_limit(self):
        data = {"client_id": self.client_id, "resource": self.server.resource,
                "grant_type": "authorization_code", "code": "invalid", "code_verifier": self.verifier,
                "redirect_uri": self.redirect}
        with self.store.db() as db:
            db.execute("INSERT INTO oauth_limits VALUES(?,?,119)", ("token-client:" + digest(self.client_id), time.time()))
        self.assertEqual(self.client.post("/oauth/token", data=data).status_code, 400)
        self.assertEqual(self.client.post("/oauth/token", data=data).status_code, 429)

    def test_authorize_rejects_bad_bindings_without_redirect(self):
        for changes in ({"redirect_uri": "https://evil.example/cb"}, {"code_challenge_method": "plain"}, {"code_challenge": "invalid"}, {"resource": "https://evil.example/mcp"}, {"scope": "mail.write"}, {"client_id": "unknown"}, {"response_type": "token"}):
            response = self.client.get("/oauth/authorize", params=self.parameters(**changes), follow_redirects=False)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("location", response.headers)

    def test_authorize_repeated_parameter_rejected(self):
        response = self.client.get("/oauth/authorize", params=list(self.parameters().items()) + [("resource", "https://evil.example")])
        self.assertEqual(response.status_code, 400)

    def test_consent_is_authenticated_csrf_protected_and_session_bound(self):
        request_id = self.begin()
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/oauth/consent", params={"request_id": request_id}).status_code, 401)
        self.client.cookies.set("mailharbor_session", "owner-one")
        self.assertEqual(self.client.get("/api/oauth/consent", params={"request_id": request_id}).status_code, 200)
        self.assertEqual(self.client.post("/api/oauth/consent", json={"request_id": request_id, "approve": True}).status_code, 403)
        self.client.cookies.clear()
        self.client.cookies.set("mailharbor_session", "owner-two")
        self.assertEqual(self.client.get("/api/oauth/consent", params={"request_id": request_id}).status_code, 403)
        self.assertEqual(self.client.post("/api/oauth/consent", json={"request_id": request_id, "approve": True}, headers=self.headers).status_code, 403)

    def test_consent_requires_display_and_cannot_replay(self):
        request_id = self.begin()
        response = self.client.post("/api/oauth/consent", json={"request_id": request_id, "approve": True}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        callback = self.consent(request_id)
        self.assertEqual(callback["state"], ["opaque-state"])
        self.assertEqual(callback["iss"], [self.settings.public_url])
        response = self.client.post("/api/oauth/consent", json={"request_id": request_id, "approve": True}, headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_denial_and_expiry(self):
        callback = self.consent(self.begin(), approve=False)
        self.assertEqual(callback["error"], ["access_denied"])
        self.assertNotIn("code", callback)
        self.assertEqual(self.server.grants(), [])
        request_id = self.begin()
        with self.store.db() as db:
            db.execute("UPDATE oauth_requests SET expires_at=0")
        self.assertEqual(self.client.get("/api/oauth/consent", params={"request_id": request_id}).status_code, 404)

    def test_code_exchange_token_hashing_and_audience(self):
        pair = self.token_pair()
        self.assertTrue(self.server.validate_access_token(pair["access_token"]))
        self.assertFalse(self.server.validate_access_token(pair["refresh_token"]))
        self.assertFalse(self.server.validate_access_token(pair["access_token"], resource="https://other.example/mcp"))
        data = self.store.path.read_bytes()
        self.assertNotIn(pair["access_token"].encode(), data)
        self.assertNotIn(pair["refresh_token"].encode(), data)
        self.assertNotIn(self.redirect.encode(), data)

    def test_code_must_match_pkce_redirect_client_resource(self):
        code = self.authorized_code()
        for changes in ({"code_verifier": "z" * 64}, {"redirect_uri": "https://evil.example/cb"}, {"resource": "https://evil.example/mcp"}, {"client_id": "unknown"}):
            self.assertIn(self.exchange(code, **changes).status_code, (400, 401))
        self.assertEqual(self.exchange(code).status_code, 200)

    def test_valid_code_replay_revokes_family(self):
        code = self.authorized_code()
        pair = self.exchange(code).json()
        self.assertTrue(self.server.validate_access_token(pair["access_token"]))
        self.assertEqual(self.exchange(code).status_code, 400)
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))

    def test_refresh_rotation_replay_revocation(self):
        original = self.token_pair()
        data = {"grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": original["refresh_token"], "resource": self.server.resource}
        response = self.client.post("/oauth/token", data=data)
        self.assertEqual(response.status_code, 200)
        newer = response.json()
        self.assertNotEqual(original["refresh_token"], newer["refresh_token"])
        self.assertFalse(self.server.validate_access_token(original["access_token"]))
        self.assertTrue(self.server.validate_access_token(newer["access_token"]))
        self.assertEqual(self.client.post("/oauth/token", data=data).status_code, 400)
        self.assertFalse(self.server.validate_access_token(newer["access_token"]))

    def test_token_expiry_and_origin_change(self):
        pair = self.token_pair()
        with self.store.db() as db:
            db.execute("UPDATE oauth_tokens SET expires_at=0 WHERE kind='access'")
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))
        pair = self.token_pair()
        self.settings.public_url = "https://changed.example.com"
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))

    def test_owner_revoke_and_revoke_endpoint(self):
        pair = self.token_pair()
        grants = self.client.get("/api/oauth/grants").json()["grants"]
        self.assertEqual(len(grants), 1)
        self.assertNotIn("token", json.dumps(grants))
        self.assertEqual(self.client.delete("/api/oauth/grants/" + grants[0]["id"]).status_code, 403)
        self.assertEqual(self.client.delete("/api/oauth/grants/" + grants[0]["id"], headers=self.headers).status_code, 200)
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))
        pair = self.token_pair()
        self.client.post("/oauth/revoke", data={"token": pair["refresh_token"], "client_id": "wrong"})
        self.assertTrue(self.server.validate_access_token(pair["access_token"]))
        response = self.client.post("/oauth/revoke", data={"token": pair["refresh_token"], "client_id": self.client_id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.server.validate_access_token(pair["access_token"]))

    def test_form_content_type_duplicate_and_secret_auth_rejected(self):
        self.assertEqual(self.client.post("/oauth/token", json={}).status_code, 400)
        response = self.client.post("/oauth/token", content="client_id=a&client_id=b", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/oauth/token", data={"client_id": self.client_id}, headers={"Authorization": "Basic Zm9vOmJhcg=="})
        self.assertEqual(response.status_code, 401)

    def test_router_without_owner_validator_fails_closed(self):
        self.server.owner_session = None
        self.assertEqual(self.client.get("/api/oauth/grants").status_code, 401)

    def test_non_ascii_csrf_header_is_forbidden_without_revoking_grant(self):
        from fastapi.testclient import TestClient

        pair = self.token_pair()
        grant = self.server.grants()[0]
        with TestClient(self.client.app, base_url=self.settings.public_url,
                        raise_server_exceptions=False) as client:
            client.cookies.set("mailharbor_session", "owner-one")
            for csrf in (b"\xe9", b"\xff"):
                with self.subTest(csrf=csrf):
                    headers = [(b"Origin", self.settings.public_url.encode()), (b"X-CSRF-Token", csrf)]
                    consent = client.post("/api/oauth/consent", json={}, headers=headers)
                    self.assertEqual(consent.status_code, 403, consent.text)
                    revoke = client.delete("/api/oauth/grants/" + grant["id"], headers=headers)
                    self.assertEqual(revoke.status_code, 403, revoke.text)
                    self.assertTrue(self.server.validate_access_token(pair["access_token"]))


if __name__ == "__main__":
    unittest.main()
