"""Exercise account registration and mail isolation through the public HTTP interfaces."""
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from mailhub.config import Settings, make_hash
from mailhub.oauth_server import pkce_challenge


HAS_APP = all(importlib.util.find_spec(name) for name in ("cryptography", "fastapi", "httpx", "mcp"))
ORIGIN = "https://mail.example.test"
ADMIN_PASSWORD = "owner-fixture-password"
PASSWORD = "registered-fixture-password"


@unittest.skipUnless(HAS_APP, "Application runtime dependencies are not installed")
class IAMTests(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        from fastapi.testclient import TestClient
        from mailhub.app import create_app

        self.settings = Settings(key=Fernet.generate_key().decode(), admin_hash=make_hash(ADMIN_PASSWORD),
                                 data_dir=self.enterContext(tempfile.TemporaryDirectory()), public_url=ORIGIN,
                                 google_client_id="client-fixture", google_client_secret="secret-fixture")
        self.enterContext(patch("mailhub.providers.probe_imap", side_effect=lambda creds: {
            "email": creds["email"], "provider_uid": creds["email"]}))
        self.enterContext(patch("mailhub.providers.fetch_messages", side_effect=self.mail_snapshot))
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app, base_url=ORIGIN))
        self.origin = {"Origin": ORIGIN}

    @staticmethod
    def mail_snapshot(provider, credentials, settings, **kwargs):
        subject = "cached " + credentials.get("username", "OAuth mailbox")
        return [{"remote_id": "same-remote-message", "subject": subject, "body": subject + " private text",
                 "reply_recipients": ["sender@example.test"],
                 "received_at": "2026-10-29T10:00:00+00:00"}], credentials

    def register(self, username):
        self.client.cookies.clear()
        response = self.client.post("/api/register", json={"username": username, "password": PASSWORD}, headers=self.origin)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["user"], {"username": username.lower(), "role": "member"})
        return self.client.cookies.get("mailharbor_session"), response.json()["csrf_token"]

    def select(self, session):
        self.client.cookies.clear()
        self.client.cookies.set("mailharbor_session", session[0])
        return {**self.origin, "X-CSRF-Token": session[1]}

    def connect_imap(self, session, label, **options):
        headers = self.select(session)
        response = self.client.post("/api/accounts/imap", headers=headers, json={
            "provider": "custom", "host": "imap.example.test", "email": "shared@example.test",
            "username": label, "password": "test-only", "label": label, **options})
        self.assertEqual(response.status_code, 200, response.text)
        account_id = response.json()["account"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            messages = self.client.get("/api/messages", params={"account_id": account_id}).json()["messages"]
            if messages:
                self.assertEqual(messages[0]["subject"], "cached " + label)
                return account_id, messages[0]["id"]
            time.sleep(0.01)
        self.fail("Fixture mailbox did not sync")

    def create_key(self, session):
        response = self.client.post("/api/tokens", json={"name": "Fixture API key"}, headers=self.select(session))
        response.raise_for_status()
        return response.json()

    def test_registration_validation_session_and_restart_login(self):
        from fastapi.testclient import TestClient
        from mailhub.app import create_app

        payload = {"username": "Reader.One", "password": PASSWORD}
        for origin in ({}, {"Origin": "https://other.example.test"}):
            self.assertEqual(self.client.post("/api/register", json=payload, headers=origin).status_code, 403)
        for bad in ({"username": "ab"}, {"username": "自訂帳號"}, {"username": "a" * 65},
                    {"password": "short"}, {"role": "admin"}):
            self.assertEqual(self.client.post("/api/register", json={**payload, **bad}, headers=self.origin).status_code, 422)
        self.assertEqual(self.client.post("/api/register", json={**payload, "username": "ADMIN"},
                                          headers=self.origin).status_code, 409)
        session = self.register("Reader.One")
        headers = self.select(session)
        user = {"username": "reader.one", "role": "member"}
        self.assertEqual(self.client.get("/api/session").json()["user"], user)
        self.assertEqual(self.client.post("/api/register", json={**payload, "username": "READER.ONE"},
                                          headers=self.origin).status_code, 409)
        self.assertEqual(self.client.post("/api/logout", headers=self.origin).status_code, 403)
        self.client.post("/api/logout", headers=headers).raise_for_status()
        self.select(session)
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)
        self.assertFalse(self.client.get("/api/session").json()["authenticated"])
        with TestClient(create_app(self.settings), base_url=ORIGIN) as restarted:
            wrong = restarted.post("/api/login", json={**payload, "password": ADMIN_PASSWORD}, headers=self.origin)
            self.assertEqual(wrong.status_code, 401)
            unknown = restarted.post("/api/login", json={"username": "unknown", "password": ADMIN_PASSWORD},
                                     headers=self.origin)
            self.assertEqual(unknown.status_code, 401)
            signed_in = restarted.post("/api/login", json=payload, headers=self.origin)
            self.assertEqual(signed_in.json()["user"], user)
            self.assertNotEqual(signed_in.json()["csrf_token"], session[1])
            self.assertEqual(restarted.get("/api/session").json()["user"], user)
        self.assertNotIn(PASSWORD.encode(), self.app.state.store.path.read_bytes())

    def test_admin_compatibility_and_member_permissions(self):
        member = self.register("reader")
        headers = self.select(member)
        settings = {"sync_interval_seconds": 31, "sync_days": 3, "sync_limit": 15}
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        self.assertEqual(self.client.post("/api/settings", headers=headers, json=settings).status_code, 403)
        self.assertEqual(self.client.post("/api/diagnostics", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/tokens", json={"name": "unauthorized"}, headers=self.origin).status_code, 403)
        self.assertEqual(self.client.get("/api/tokens").json()["tokens"], [])
        login = self.client.post("/api/login", json={"password": ADMIN_PASSWORD}, headers=self.origin)
        login.raise_for_status()
        self.assertEqual(login.json()["user"], {"username": "admin", "role": "admin"})
        self.assertEqual(self.client.get("/api/settings").json()["sync_days"], self.settings.sync_days)
        self.assertEqual(self.client.get("/api/accounts", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/accounts", headers={"X-CSRF-Token": login.json()["csrf_token"]}).status_code, 200)
        self.assertEqual(self.client.post("/api/diagnostics", headers={**self.origin,
                         "X-CSRF-Token": login.json()["csrf_token"]}).status_code, 200)
        self.select(member)
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    def test_concurrent_duplicate_registrations_create_one_identity(self):
        def register(name, password):
            return self.client.post("/api/register", headers=self.origin, json={"username": name, "password": password})

        passwords = ("first-fixture-password", "second-fixture-password")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register, name, password) for name, password in zip(("Reader", "READER"), passwords)]
            statuses = [future.result().status_code for future in futures]
        self.assertCountEqual(statuses, [201, 409])
        for password, status in zip(passwords, statuses):
            login = self.client.post("/api/login", headers=self.origin, json={"username": "reader", "password": password})
            self.assertEqual(login.status_code, 200 if status == 201 else 401)

    def test_registration_throttling_preserves_existing_logins(self):
        for _ in range(10):
            response = self.client.post("/api/register", headers=self.origin, json={"username": "admin", "password": PASSWORD})
            self.assertEqual(response.status_code, 409)
        limited = self.client.post("/api/register", headers=self.origin, json={"username": "reader", "password": PASSWORD})
        self.assertEqual(limited.status_code, 429)
        owner_login = self.client.post("/api/login", headers=self.origin, json={"password": ADMIN_PASSWORD})
        self.assertEqual(owner_login.status_code, 200)

    def test_same_mailbox_different_members_and_guessed_ids(self):
        alice, bob = self.register("alice"), self.register("bob")
        alice_account, alice_message = self.connect_imap(alice, "Alice")
        alice_key = self.create_key(alice)
        self.select(bob)
        self.assertEqual(self.client.get("/api/setup").json()["counts"], {"accounts": 0, "tokens": 0})
        bob_account, bob_message = self.connect_imap(bob, "Bob")
        self.assertNotEqual(alice_account, bob_account)
        self.assertNotEqual(alice_message, bob_message)
        self.assertEqual([a["id"] for a in self.client.get("/api/accounts").json()["accounts"]], [bob_account])
        self.assertEqual(self.client.get("/api/messages").json()["total"], 1)
        self.assertEqual(self.client.get("/api/messages", params={"q": "cached Alice"}).json()["total"], 0)
        self.assertEqual(self.client.get("/api/messages", params={"account_id": alice_account}).json()["total"], 0)
        self.assertEqual(self.client.get("/api/messages/" + alice_message).status_code, 404)
        self.assertEqual(self.client.get("/api/messages/" + bob_message).json()["message"]["body"], "cached Bob private text")
        self.assertEqual(self.client.get("/api/tokens").json()["tokens"], [])
        headers = self.select(bob)
        self.assertEqual(self.client.post(f"/api/accounts/{alice_account}/sync", headers=headers).status_code, 404)
        self.assertEqual(self.client.delete("/api/accounts/" + alice_account, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete("/api/tokens/" + alice_key["id"], headers=headers).status_code, 404)
        self.assertEqual(self.client.delete("/api/accounts/" + bob_account, headers=headers).status_code, 200)
        self.select(alice)
        self.assertEqual(self.client.get("/api/setup").json()["counts"], {"accounts": 1, "tokens": 1})
        self.assertEqual(self.client.get("/api/messages/" + alice_message).status_code, 200)

    def oauth_access_token(self, session, scope="mail.read"):
        headers = self.select(session)
        callback = "https://ai.example.test/callback"
        app = self.client.post("/oauth/register", json={"redirect_uris": [callback]}).json()["client_id"]
        resource = ORIGIN + "/mcp"
        verifier = "v" * 64
        start = self.client.get("/oauth/authorize", follow_redirects=False, params={
            "client_id": app, "redirect_uri": callback, "response_type": "code", "resource": resource,
            "code_challenge_method": "S256", "code_challenge": pkce_challenge(verifier), "scope": scope})
        request_id = parse_qs(urlsplit(start.headers["location"]).query)["oauth_request"][0]
        self.client.get("/api/oauth/consent", params={"request_id": request_id}).raise_for_status()
        approved = self.client.post("/api/oauth/consent", headers=headers, json={"request_id": request_id, "approve": True})
        code = parse_qs(urlsplit(approved.json()["redirect_url"]).query)["code"][0]
        response = self.client.post("/oauth/token", data={"client_id": app, "code": code, "grant_type": "authorization_code",
                                    "resource": resource, "redirect_uri": callback, "code_verifier": verifier})
        return response.json()["access_token"]

    def call_tool(self, token, name, *, expect_error=False, **arguments):
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
        response = self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        response.raise_for_status()
        result = response.json()["result"]
        self.assertEqual(result["isError"], expect_error, result)
        if not expect_error:
            return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_web_and_mcp_submit_once_with_scoped_mailbox_and_credentials(self):
        alice, bob = self.register("alice"), self.register("bob")
        _, message_id = self.connect_imap(alice, "Alice", smtp_host="smtp.example.test", smtp_port=587)
        endpoint = f"/api/messages/{message_id}/reply"
        args = {"body": "收到，謝謝。", "request_id": str(uuid4())}
        alice_key = self.client.post("/api/tokens", headers=self.select(alice),
                                     json={"name": "reply key", "allow_reply": True}).json()["token"]
        bob_key = self.oauth_access_token(bob, scope="mail.read mail.reply")
        readonly = self.create_key(alice)["token"]
        entered, release = threading.Event(), threading.Event()

        def send(provider, credentials, settings, original, body, **kwargs):
            self.assertEqual((provider, credentials["username"], original["id"], body),
                             ("custom", "Alice", message_id, args["body"]))
            entered.set()
            self.assertTrue(release.wait(5))

        with patch("mailhub.providers.send_reply", side_effect=send) as provider:
            self.call_tool(readonly, "reply_email", message_id=message_id, expect_error=True, **args)
            self.call_tool(bob_key, "reply_email", message_id=message_id, expect_error=True, **args)
            self.assertEqual(self.client.post(endpoint, headers=self.select(bob), json=args).status_code, 404)
            headers = self.select(alice)
            self.assertEqual(self.client.post(endpoint, headers=self.origin, json=args).status_code, 403)
            self.assertEqual(self.client.post(endpoint, headers=headers, json={**args, "to": "other@example.test"}).status_code, 422)
            self.assertEqual(self.client.post(endpoint, headers=headers, json={**args, "body": "  "}).status_code, 422)
            self.assertEqual(self.client.get(endpoint[:-len("/reply")]).json()["message"]["reply"],
                             {"available": True, "recipients": ["sender@example.test"], "reason": None})
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.call_tool, alice_key, "reply_email", message_id=message_id, **args)
                try:
                    self.assertTrue(entered.wait(3))
                    pending = self.client.post(endpoint, headers=headers, json=args)
                    self.assertEqual((pending.status_code, pending.json()["code"]), (409, "reply_pending"))
                finally:
                    release.set()
                self.assertEqual(future.result(), {"accepted": True})
            self.assertEqual(self.client.post(endpoint, headers=headers, json=args).json(), {"accepted": True})
            conflict = self.client.post(endpoint, headers=headers, json={**args, "body": "different reply"})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(provider.call_count, 1)
        oauth = self.oauth_access_token(alice, scope="mail.read mail.reply")
        for token in (alice_key, oauth):
            listed = self.client.post("/mcp", headers={"Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
            self.assertFalse(tools["reply_email"]["annotations"]["readOnlyHint"])
            self.assertTrue(tools["reply_email"]["annotations"]["destructiveHint"])
            self.assertEqual(self.call_tool(token, "reply_email", message_id=message_id, **args), {"accepted": True})

    def test_uncertain_delivery_and_definite_failure_have_distinct_retries(self):
        from mailhub.providers import ProviderError
        session = self.register("alice")
        _, message_id = self.connect_imap(session, "Alice", smtp_host="smtp.example.test")
        headers = self.select(session)
        endpoint = f"/api/messages/{message_id}/reply"
        args = {"request_id": str(uuid4()), "body": "approved fixture reply"}
        with patch("mailhub.providers.send_reply", side_effect=[
            ProviderError("permission denied", code="permission_denied"),
            ProviderError("acceptance uncertain", code="delivery_unknown")]) as send:
            self.assertEqual(self.client.post(endpoint, headers=headers, json=args).status_code, 400)
            for _ in range(2):
                result = self.client.post(endpoint, headers=headers, json=args)
                self.assertEqual((result.status_code, result.json()["code"]), (409, "delivery_unknown"))
            self.assertEqual(send.call_count, 2)

    def test_rest_and_concurrent_mcp_tools_use_the_bearer_identity(self):
        alice, bob = self.register("alice"), self.register("bob")
        alice_account, alice_message = self.connect_imap(alice, "Alice")
        bob_account, bob_message = self.connect_imap(bob, "Bob")
        alice_key = self.create_key(alice)["token"]
        bob_key = self.create_key(bob)["token"]
        bob_oauth = self.oauth_access_token(bob)
        # Alice's browser cookie must never override the bearer identity.
        self.select(alice)
        self.assertEqual(self.client.get("/v1/accounts").status_code, 401)
        for key, own_account, own_message, other_message in (
                (alice_key, alice_account, alice_message, bob_message), (bob_key, bob_account, bob_message, alice_message)):
            headers = {"Authorization": "Bearer " + key}
            for url in ("/v1/accounts", "/v1/status"):
                self.assertEqual([a["id"] for a in self.client.get(url, headers=headers).json()["accounts"]], [own_account])
            result = self.client.get("/v1/messages", headers=headers, params={"q": "cached"}).json()
            self.assertEqual([m["id"] for m in result["messages"]], [own_message])
            self.assertEqual(self.client.get("/v1/messages/" + other_message, headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/v1/accounts", headers={"Authorization": "Bearer " + bob_oauth}).status_code, 401)
        with ThreadPoolExecutor(max_workers=2) as executor:
            as_alice = executor.submit(self.call_tool, alice_key, "list_accounts")
            as_bob = executor.submit(self.call_tool, bob_oauth, "get_sync_status")
            self.assertEqual([a["id"] for a in as_alice.result()["accounts"]], [alice_account])
            self.assertEqual([a["id"] for a in as_bob.result()["accounts"]], [bob_account])
        for token in (bob_key, bob_oauth):
            found = self.call_tool(token, "search_emails", query="cached")
            self.assertEqual([m["id"] for m in found["messages"]], [bob_message])
            detail = self.call_tool(token, "read_email", message_id=bob_message)
            self.assertEqual(detail["message"]["body"], "cached Bob private text")
            self.call_tool(token, "read_email", message_id=alice_message, expect_error=True)

    def test_provider_callback_and_refresh_credentials_stay_with_the_correct_user(self):
        alice, bob = self.register("alice"), self.register("bob")

        def start(session):
            response = self.client.post("/api/oauth/gmail/start", headers=self.select(session))
            return parse_qs(urlsplit(response.json()["url"]).query)["state"][0]

        def finish(session, state):
            self.select(session)
            response = self.client.get("/api/oauth/gmail/callback", params={"code": "test-code", "state": state},
                                       follow_redirects=False)
            return response.headers["location"]

        state = start(alice)
        identity = {"email": "shared@example.test", "provider_uid": "same-provider-identity"}
        alice_credentials = {"client_id": "client-fixture", "access_token": "a", "refresh_token": "alice-refresh"}
        with patch("mailhub.providers.identify_oauth", return_value=identity), \
             patch("mailhub.providers.oauth_exchange", return_value=alice_credentials) as exchange:
            self.assertEqual(finish(bob, state), "/?connection_error=1")
            exchange.assert_not_called()
            self.assertEqual(finish(alice, state), "/?connected=1")
            alice_account = self.client.get("/api/accounts").json()["accounts"][0]["id"]
            exchange.return_value = {"client_id": "client-fixture", "access_token": "b"}
            self.assertEqual(finish(bob, start(bob)), "/?connection_error=1")
            self.assertEqual(self.client.get("/api/accounts").json()["accounts"], [])
            exchange.return_value = {**exchange.return_value, "refresh_token": "bob-refresh"}
            self.assertEqual(finish(bob, start(bob)), "/?connected=1")
            bob_accounts = self.client.get("/api/accounts").json()["accounts"]
            self.assertEqual(len(bob_accounts), 1)
            self.assertNotEqual(bob_accounts[0]["id"], alice_account)
