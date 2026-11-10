"""Provider safety/behavior tests; all network and mailbox access is mocked."""
import base64
import json
import socket
import time
import unittest
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

try:
    import httpx
except ModuleNotFoundError:
    httpx = None

from mailhub import providers as p


SETTINGS = SimpleNamespace(google_client_id="google-client", google_client_secret="google-SECRET",
                           microsoft_client_id="microsoft-client", microsoft_client_secret="microsoft-SECRET",
                           public_url="https://mail.example.com")
CREDS = {"email": "person@example.com", "username": "person@example.com", "password": "DO-NOT-LEAK",
         "host": "imap.example.com", "port": 993, "folder": "INBOX"}
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 993))]
FIXED_CUTOFF = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
IMAP_HEADER = b"HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID REPLY-TO REFERENCES IN-REPLY-TO)"


def imap_literal(section, value):
    return (b"1 (BODY[" + section + b"]<0> {" + str(len(value)).encode("ascii") + b"}", value)


class OAuthTests(unittest.TestCase):
    def test_authorization_pkce_scopes_and_no_secret(self):
        for provider in ("gmail", "microsoft"):
            url = p.oauth_authorize_url(provider, SETTINGS, "csrf-state", "pkce-challenge")
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["state"], ["csrf-state"])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertEqual(params["redirect_uri"], [f"https://mail.example.com/api/oauth/{provider}/callback"])
            self.assertNotIn("SECRET", url)
            self.assertNotIn("Mail.ReadWrite", url)
            self.assertNotIn("gmail.modify", url)
            send_scope = "https://www.googleapis.com/auth/gmail.send" if provider == "gmail" else "Mail.Send"
            self.assertNotIn(send_scope, params["scope"][0].split())
            enabled = p.oauth_authorize_url(provider, SETTINGS, "csrf-state", "pkce-challenge", allow_reply=True)
            self.assertEqual(set(parse_qs(urlsplit(enabled).query)["scope"][0].split()),
                             {*params["scope"][0].split(), send_scope})
        self.assertIn("select_account", p.oauth_authorize_url("gmail", SETTINGS, "s", "c"))

    def test_token_refresh_rotates_and_preserves_existing_refresh(self):
        old = {"access_token": "old", "refresh_token": "refresh-old", "expires_at": 0}
        same = p._token_credentials("gmail", {"access_token": "new", "expires_in": 120}, old)
        self.assertEqual(same["refresh_token"], "refresh-old")
        new = p._token_credentials("gmail", {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 120}, old)
        self.assertEqual(new["refresh_token"], "refresh-new")
        self.assertEqual(old["refresh_token"], "refresh-old")
        self.assertGreater(new["expires_at"], time.time())

    @unittest.skipIf(httpx is None, "httpx dependency is not installed; HTTP integration test not run")
    def test_legacy_credentials_bind_to_client_after_successful_refresh(self):
        old = {"access_token": "expired", "refresh_token": "old", "expires_at": 0}
        with patch.object(p, "_request_json", return_value={"access_token": "new-access"}):
            refreshed = p._refresh("gmail", old, SETTINGS)
        self.assertEqual(refreshed["client_id"], SETTINGS.google_client_id)
        self.assertEqual(refreshed["refresh_token"], "old")
        self.assertNotIn("client_id", old)

    def test_scope_checks_and_missing_background_access(self):
        with self.assertRaises(p.ProviderError):
            p._token_credentials("gmail", {"access_token": "x", "refresh_token": "r", "scope": "openid email"})
        with self.assertRaises(p.ProviderError):
            p._token_credentials("gmail", {"access_token": "x"})
        result = p._token_credentials("microsoft", {"access_token": "x", "refresh_token": "r", "scope": "https://graph.microsoft.com/Mail.Read"})
        self.assertEqual(result["access_token"], "x")

    @unittest.skipIf(httpx is None, "httpx dependency is not installed; HTTP integration test not run")
    def test_exchange_uses_fixed_endpoint_and_pkce(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "https://www.googleapis.com/auth/gmail.readonly"})
        with patch.object(p, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))):
            p.oauth_exchange("gmail", SETTINGS, "auth-code", "pkce-verifier")
        self.assertEqual(str(observed[0].url), "https://oauth2.googleapis.com/token")
        self.assertIn("code_verifier=pkce-verifier", observed[0].content.decode())

    @unittest.skipIf(httpx is None, "httpx dependency is not installed; HTTP integration test not run")
    def test_provider_error_never_contains_remote_body_or_credentials(self):
        def handler(request):
            return httpx.Response(401, json={"error_description": "DO-NOT-LEAK secret mailbox body"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(p.ProviderError) as ctx:
                p._request_json(client, "GET", "https://gmail.googleapis.com/test", headers={"Authorization": "Bearer DO-NOT-LEAK"})
        self.assertNotIn("DO-NOT-LEAK", str(ctx.exception))
        self.assertNotIn("mailbox body", str(ctx.exception))
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="private-deleted-body"))) as client:
            self.assertIsNone(p._request_json(client, "GET", "https://gmail.googleapis.com/messages/gone", missing_ok=True))
            with self.assertRaises(p.ProviderError) as missing:
                p._request_json(client, "GET", "https://gmail.googleapis.com/messages")
            self.assertEqual(missing.exception.code, "not_found")
            self.assertNotIn("private-deleted-body", str(missing.exception))
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "private-token"}))) as client:
            with self.assertRaises(p.ProviderError) as expired:
                p._request_json(client, "POST", "https://oauth2.googleapis.com/token", oauth=True)
            self.assertTrue(expired.exception.reauth_required)
            self.assertNotIn("private-token", str(expired.exception))

    def test_reject_cross_origin_graph_pagination(self):
        for url in ["http://graph.microsoft.com/v1.0/me/messages", "https://attacker.example/v1.0/me/messages",
                    "https://graph.microsoft.com:444/v1.0/me/messages", "https://user@graph.microsoft.com/v1.0/me/messages",
                    "https://graph.microsoft.com:invalid/v1.0/me/messages"]:
            with self.subTest(url=url), self.assertRaises(p.ProviderError):
                p._graph_page(url)

    def test_catalog_declares_unsupported_and_unconfigured(self):
        empty = SimpleNamespace(google_client_id="", google_client_secret="", microsoft_client_id="", microsoft_client_secret="")
        catalog = {x["id"]: x for x in p.provider_catalog(empty)}
        self.assertFalse(catalog["gmail"]["configured"])
        self.assertEqual(catalog["proton"]["kind"], "unsupported")
        self.assertEqual(catalog["tuta"]["kind"], "unsupported")
        self.assertEqual(catalog["icloud"]["host"], "imap.mail.me.com")

    def test_rotated_credentials_saved_before_mail_fetch_failure(self):
        events = []
        rotated = {"access_token": "new-access", "refresh_token": "new-refresh"}
        def save(credentials):
            events.append(("save", dict(credentials)))
        def fail(*args):
            events.append(("fetch", None))
            raise p.ProviderError("temporary failure")
        with patch.object(p, "_refresh", return_value=rotated), patch.object(p, "_client", return_value=MagicMock()), patch.object(p, "_fetch_microsoft", side_effect=fail):
            with self.assertRaises(p.ProviderError):
                p.fetch_messages("microsoft", {}, SETTINGS, on_credentials_updated=save)
        self.assertEqual([event[0] for event in events], ["save", "fetch"])
        self.assertEqual(events[0][1]["refresh_token"], "new-refresh")

    def test_oauth_error_classification_preserves_safe_action(self):
        error = p._http_error(400, {"error": "invalid_grant", "error_description": "private-token"}, oauth=True)
        self.assertEqual(error.code, "reauth_required")
        self.assertTrue(error.reauth_required)
        self.assertFalse(error.retryable)
        self.assertNotIn("private-token", str(error))
        config_error = p._http_error(401, {"error": "invalid_client", "error_description": "private-secret"}, oauth=True)
        self.assertEqual(config_error.code, "oauth_configuration")
        self.assertFalse(config_error.reauth_required)
        self.assertNotIn("private-secret", str(config_error))
        transient = p._http_error(503, {"error": "server_error"}, oauth=True)
        self.assertTrue(transient.retryable)
        self.assertFalse(transient.reauth_required)
        quota = p._http_error(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        self.assertEqual(quota.code, "rate_limited")
        self.assertTrue(quota.retryable)


class ImapNetworkTests(unittest.TestCase):
    def test_reject_private_loopback_linklocal_multicast_and_mixed_dns(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.2", "::1", "fc00::1", "224.0.0.1"]:
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 993))]
            with self.subTest(address=address), patch.object(p.socket, "getaddrinfo", return_value=answers):
                with self.assertRaises(p.ProviderError):
                    p._public_addresses("mail.example.com", 993)
        mixed = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 993))]
        with patch.object(p.socket, "getaddrinfo", return_value=mixed), self.assertRaises(p.ProviderError):
            p._public_addresses("mail.example.com", 993)

    def test_reject_unsafe_host_and_plaintext_port_without_dns(self):
        with patch.object(p.socket, "getaddrinfo") as resolver:
            for host, port in [("http://mail.example.com", 993), ("mail.example.com\r\n", 993), ("mail.example.com", 143)]:
                with self.assertRaises(p.ProviderError):
                    p._public_addresses(host, port)
            resolver.assert_not_called()

    def test_connection_uses_pinned_ip_and_original_tls_hostname(self):
        obj = object.__new__(p._PinnedIMAPSSL)
        obj._addresses = PUBLIC_DNS
        obj.host = "imap.example.com"
        obj.ssl_context = MagicMock()
        sock = MagicMock()
        with patch.object(p.socket, "socket", return_value=sock), patch.object(p.socket, "getaddrinfo") as resolver:
            obj._create_socket(20)
        resolver.assert_not_called()
        sock.connect.assert_called_once_with(("93.184.216.34", 993))
        obj.ssl_context.wrap_socket.assert_called_once_with(sock, server_hostname="imap.example.com")

    def test_literal_size_guard(self):
        obj = object.__new__(p._PinnedIMAPSSL)
        with self.assertRaises(p.ProviderError):
            obj.read(p.MAX_WIRE_BODY + p.MAX_HEADER + 1)

    def test_unavailable_ipv6_socket_falls_back_to_validated_ipv4(self):
        client = object.__new__(p._PinnedIMAPSSL)
        client._addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 993, 0, 0)), *PUBLIC_DNS]
        client.host = "imap.example.com"
        client.ssl_context = MagicMock()
        ipv4_socket = MagicMock()
        with patch.object(p.socket, "socket", side_effect=[OSError("IPv6 unavailable"), ipv4_socket]):
            connection = client._create_socket(20)
        self.assertIs(connection, client.ssl_context.wrap_socket.return_value)
        ipv4_socket.connect.assert_called_once_with(("93.184.216.34", 993))
        client.ssl_context.wrap_socket.assert_called_once_with(ipv4_socket, server_hostname=client.host)

    def test_probe_selects_readonly_and_never_changes_messages(self):
        fake = MagicMock()
        fake.select.return_value = ("OK", [b"1"])
        with patch.object(p, "_PinnedIMAPSSL", return_value=fake):
            identity = p.probe_imap(CREDS)
        fake.select.assert_called_once_with('"INBOX"', readonly=True)
        fake.login.assert_called_once_with('"person@example.com"', "DO-NOT-LEAK")
        fake.logout.assert_called_once()
        fake.store.assert_not_called()
        fake.expunge.assert_not_called()
        self.assertEqual(identity["email"], CREDS["email"])
        self.assertNotIn(CREDS["password"], json.dumps(identity))


    def test_select_failure_keeps_its_error_when_logout_fails(self):
        fake = MagicMock()
        fake.select.return_value = ("NO", [])
        fake.logout.side_effect = OSError("disconnected")
        with patch.object(p, "_PinnedIMAPSSL", return_value=fake), self.assertRaisesRegex(p.ProviderError, "資料夾"):
            p.probe_imap(CREDS)
        fake.logout.assert_called_once_with()

    def test_imap_password_command_injection_rejected_before_connect(self):
        for field in ("username", "password", "folder"):
            for control in ("\r", "\n", "\x00"):
                credentials = dict(CREDS, **{field: "value" + control + "A123 STORE 1 +FLAGS (\\Deleted)"})
                with self.subTest(field=field, control=repr(control)), patch.object(p, "_PinnedIMAPSSL") as connect:
                    with self.assertRaises(p.ProviderError):
                        p._imap_open(credentials)
                    connect.assert_not_called()

    def test_unicode_mailbox_modified_utf7_and_literal_ampersand(self):
        # RFC 3501 section 5.1.3 example, not generated by the implementation under test.
        self.assertEqual(p._imap_mailbox_name("~peter/mail/台北/日本語"), "~peter/mail/&U,BTFw-/&ZeVnLIqe-")
        self.assertEqual(p._imap_mailbox_name("A & B"), "A &- B")
        fake = MagicMock()
        fake.select.return_value = ("OK", [b"0"])
        with patch.object(p, "_PinnedIMAPSSL", return_value=fake):
            p.probe_imap(dict(CREDS, folder="台北"))
        fake.select.assert_called_once_with('"&U,BTFw-"', readonly=True)

    def test_netease_sends_required_id_for_stdlib_string_capabilities(self):
        for host in ("imap.163.com", "imap.126.com", "imap.yeah.net", "imap.vip.163.com", "imap.vip.126.com", "imap.188.com"):
            with self.subTest(host=host), patch.object(p, "_PinnedIMAPSSL") as connect:
                fake = connect.return_value
                fake.capabilities = ("IMAP4REV1", "ID")
                fake.select.return_value = ("OK", [b"0"])
                p.probe_imap(dict(CREDS, host=host))
                fake._simple_command.assert_called_once_with("ID", '("name" "MailHarbor" "version" "1.0")')


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.original = {"remote_id": "provider/id", "account_email": "owner@example.test", "subject": "Re: 確認",
                         "sender": "From <sender@example.test>", "recipients": "someone.else@example.test",
                         "reply_recipients": ["reply@example.test"], "reply_thread_id": "conversation",
                         "reply_message_id": "<original@example.test>", "reply_references": ["<root@example.test>"]}
        self.credentials = {"allow_reply": True, "scope": "Mail.Read Mail.Send https://www.googleapis.com/auth/gmail.send",
                            "access_token": "synthetic", "expires_at": time.time() + 3600}

    def test_reply_to_precedence_and_malformed_headers(self):
        sender = ("From", '"Example, From" <sender@example.test>')
        self.assertEqual(p._reply_headers([sender])["reply_recipients"], ["sender@example.test"])
        valid = ("Reply-To", '"Example, Reply" <reply@example.test>,\r\n other@example.test')
        self.assertEqual(p._reply_headers([sender, valid])["reply_recipients"], ["reply@example.test", "other@example.test"])
        for headers in [[("Reply-To", "")], [("Reply-To", "valid@example.test, bad@")],
                        [("Reply-To", "reply@example.test\r\nBcc: hidden@example.test")], [valid, valid]]:
            with self.subTest(headers=headers):
                self.assertEqual(p._reply_headers([sender, *headers])["reply_recipients"], [])
        self.assertEqual(p._graph_reply_recipients({"from": {"emailAddress": {"address": "from@example.test"}},
                                                   "replyTo": [{"emailAddress": {"address": "to@example.test"}}]}), ["to@example.test"])
        self.assertEqual(p._graph_reply_recipients({"from": {"emailAddress": {"address": "from@example.test"}}, "replyTo": [{}]}), [])

    def test_legacy_grant_may_refresh_for_sync_and_requires_consent_to_send(self):
        old = {"scope": "Mail.Read", "refresh_token": "old", "expires_at": 0}
        with patch.object(p, "_request_json", return_value={"access_token": "new"}) as http:
            updated = p._refresh("microsoft", old, SETTINGS)
            self.assertNotIn("scope", http.call_args.kwargs["data"])
        self.assertEqual(updated["scope"], "Mail.Read")
        with patch.object(p, "_client") as client, self.assertRaises(p.ProviderError) as denied:
            p.send_reply("microsoft", updated, SETTINGS, self.original, "body")
        self.assertTrue(denied.exception.reauth_required)
        client.assert_not_called()

    def test_reply_requires_local_opt_in_and_actual_grant_including_after_refresh(self):
        # Some providers return previously granted scopes even when this request only asked for read.
        response = {"access_token": "new", "refresh_token": "refresh", "scope": "Mail.Read Mail.Send"}
        with patch.object(p, "_request_json", return_value=response) as http:
            read = p.oauth_exchange("microsoft", SETTINGS, "code", "verifier")
            self.assertNotIn("Mail.Send", http.call_args.kwargs["data"]["scope"])
            opted = p.oauth_exchange("microsoft", SETTINGS, "code", "verifier", allow_reply=True)
            self.assertIn("Mail.Send", http.call_args.kwargs["data"]["scope"])
            refreshed_read = p._refresh("microsoft", dict(read, expires_at=0), SETTINGS)
        for blocked in (response, read, refreshed_read, dict(opted, scope="Mail.Read")):
            self.assertEqual(p.reply_unavailable_reason("microsoft", blocked), "reauth_required")
        self.assertIsNone(p.reply_unavailable_reason("microsoft", opted))

    def test_legacy_cache_and_unsafe_destinations_are_blocked(self):
        for recipients in (None, [], ["good@example.test,hidden@example.test"], ["a@example.test\nCc: misc@example.test"]):
            original = dict(self.original, reply_recipients=recipients)
            if recipients is None:
                del original["reply_recipients"]
            with self.subTest(recipients=recipients), patch.object(p, "_client") as client, self.assertRaises(p.ProviderError):
                p.send_reply("gmail", self.credentials, SETTINGS, original, "body")
            client.assert_not_called()

    @unittest.skipIf(httpx is None, "httpx dependency is not installed")
    def test_gmail_submits_attached_thread_and_plain_text_mime_to_approved_recipients(self):
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(200, json={"id": "sent-id"})
        with patch.object(p, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle))):
            p.send_reply("gmail", self.credentials, SETTINGS, dict(self.original, subject="確認"), "OK\n收到")
        self.assertEqual(len(observed), 1)
        self.assertEqual(str(observed[0].url), "https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
        payload = json.loads(observed[0].content)
        self.assertEqual(payload["threadId"], "conversation")
        msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
        self.assertEqual(str(msg["To"]), "reply@example.test")
        self.assertIsNone(msg["Cc"])
        self.assertIsNone(msg["Bcc"])
        self.assertEqual(str(msg["From"]), "owner@example.test")
        self.assertEqual(str(msg["Subject"]), "確認")
        self.assertEqual(str(msg["In-Reply-To"]), "<original@example.test>")
        self.assertEqual(str(msg["References"]), "<root@example.test> <original@example.test>")
        self.assertEqual(msg.get_content().strip().splitlines(), ["OK", "收到"])
        self.assertEqual(msg.get_content_type(), "text/plain")

    def test_gmail_missing_thread_headers_cannot_send_a_standalone_message(self):
        for missing in ("reply_thread_id", "reply_message_id"):
            original = dict(self.original)
            del original[missing]
            with self.subTest(missing=missing), patch.object(p, "_client") as network, self.assertRaises(p.ProviderError) as failed:
                p.send_reply("gmail", self.credentials, SETTINGS, original, "body")
            self.assertEqual(failed.exception.code, "thread_unavailable")
            network.assert_not_called()

    @unittest.skipIf(httpx is None, "httpx dependency is not installed")
    def test_graph_empty_accepted_response_and_sanitized_send_outcomes(self):
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(202)
        with patch.object(p, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle))):
            p.send_reply("microsoft", self.credentials, SETTINGS, self.original, "<b>text</b>")
        self.assertEqual(str(observed[0].url), "https://graph.microsoft.com/v1.0/me/messages/provider%2Fid/reply")
        self.assertEqual(json.loads(observed[0].content)["message"],
                         {"toRecipients": [{"emailAddress": {"address": "reply@example.test"}}],
                          "body": {"contentType": "Text", "content": "<b>text</b>"}})
        for response, code in [(403, "permission_denied"), (504, "delivery_unknown"), (0, "delivery_unknown")]:
            def error(request):
                if not response:
                    raise httpx.ReadTimeout("private")
                return httpx.Response(response, text="private")
            with self.subTest(response=response), patch.object(p, "_client", lambda: httpx.Client(transport=httpx.MockTransport(error))), self.assertRaises(p.ProviderError) as failed:
                p.send_reply("microsoft", self.credentials, SETTINGS, self.original, "body")
            self.assertEqual(failed.exception.code, code)
            self.assertNotIn("private", str(failed.exception))

    def test_custom_smtp_is_opt_in_presets_use_fixed_hosts(self):
        self.assertEqual(p.reply_unavailable_reason("custom", CREDS), "smtp_not_configured")
        smtp = MagicMock()
        smtp.mail.return_value = smtp.rcpt.return_value = smtp.data.return_value = (250, b"ok")
        with patch.object(p, "_PinnedSMTP", return_value=smtp) as connect:
            credentials = dict(CREDS, username="person", smtp_host="attacker.example", smtp_port=587)
            p.send_reply("icloud", credentials, SETTINGS, self.original, "body")
            connect.assert_called_once_with("smtp.mail.me.com", 587)
            smtp.login.assert_called_once_with(credentials["email"], credentials["password"])
            smtp.rcpt.assert_called_once_with("reply@example.test")
            smtp.data.assert_called_once()
            p.send_reply("custom", credentials, SETTINGS, self.original, "body")
            connect.assert_called_with("attacker.example", 587)

    def test_smtp_refuses_partial_delivery_and_handles_uncertain_data_result(self):
        original = dict(self.original, reply_recipients=["first@example.test", "second@example.test"])
        smtp = MagicMock()
        smtp.mail.return_value = (250, b"ok")
        smtp.rcpt.side_effect = [(250, b"ok"), (550, b"private")]
        with patch.object(p, "_PinnedSMTP", return_value=smtp), self.assertRaises(p.ProviderError) as failed:
            p.send_reply("qq", CREDS, SETTINGS, original, "body")
        self.assertNotEqual(failed.exception.code, "delivery_unknown")
        self.assertNotIn("private", str(failed.exception))
        smtp.data.assert_not_called()
        smtp.rcpt.side_effect = None
        smtp.rcpt.return_value = (250, b"ok")
        smtp.data.side_effect = p.smtplib.SMTPServerDisconnected("private password")
        with patch.object(p, "_PinnedSMTP", return_value=smtp), self.assertRaises(p.ProviderError) as uncertain:
            p.send_reply("qq", CREDS, SETTINGS, original, "body")
        self.assertEqual(uncertain.exception.code, "delivery_unknown")
        self.assertFalse(uncertain.exception.retryable)

    def test_smtp_requires_pinned_public_dns_and_verified_tls_before_authentication(self):
        for port in (465, 587):
            with self.subTest(port=port), patch.object(p.socket, "getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]), self.assertRaises(p.ProviderError):
                p._PinnedSMTP("internal.example", port)
        smtp = object.__new__(p._PinnedSMTP)
        smtp._addresses = PUBLIC_DNS
        smtp._tls = MagicMock()
        with patch.object(p, "_pinned_socket") as pinned:
            smtp._get_socket("smtp.example.test", 465, 10)
            pinned.assert_called_once_with(PUBLIC_DNS, 10, "smtp.example.test", smtp._tls)
        with patch.object(p, "_public_addresses", return_value=("smtp.example.test", PUBLIC_DNS)), \
                patch.object(p.smtplib.SMTP, "__init__", return_value=None), patch.object(p._PinnedSMTP, "close"), \
                patch.object(p._PinnedSMTP, "starttls", side_effect=p.smtplib.SMTPNotSupportedError("private")) as tls, \
                patch.object(p._PinnedSMTP, "login") as login, self.assertRaises(p.ProviderError):
            p.send_reply("icloud", CREDS, SETTINGS, self.original, "body")
        tls.assert_called_once()
        self.assertTrue(tls.call_args.kwargs["context"].check_hostname)
        login.assert_not_called()


class BodyParsingTests(unittest.TestCase):
    def test_html_removed_and_script_style_content_discarded(self):
        value = '<head><style>secret</style></head><p>Hello &amp; welcome</p><script>steal()</script><img src="https://tracker.example"><a href="https://example.com">Read</a>'
        text = p._plain_html(value)
        self.assertIn("Hello & welcome", text)
        self.assertIn("Read", text)
        for forbidden in ("secret", "steal", "https://tracker", "<script", "<p>"):
            self.assertNotIn(forbidden, text)

    def test_html_markup_length_does_not_falsely_claim_text_truncation(self):
        status = {"truncated": False}
        value = "<!--" + "x" * p.MAX_BODY + "--><p>Complete body</p>"
        self.assertEqual(p._plain_html(value, status), "Complete body")
        self.assertFalse(status["truncated"])

    def test_bodystructure_handles_nested_alternatives_and_attachments(self):
        raw = b'((("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL)("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 30 1 NIL NIL) "ALTERNATIVE")("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 500 NIL ("ATTACHMENT" ("FILENAME" "report.pdf"))) "MIXED")'
        structure = p._imap_tokens(raw)
        sections, attachments = p._mime_sections(structure)
        self.assertEqual([x["section"] for x in sections], ["1.1", "1.2"])
        self.assertEqual(attachments, [{"filename": "report.pdf", "content_type": "application/pdf", "size": 500}])

    def test_text_attachment_is_not_read_as_message_body(self):
        node = p._imap_tokens(b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 20 1 NIL ("ATTACHMENT" ("FILENAME" "notes.txt")))')
        sections, attachments = p._mime_sections(node)
        self.assertEqual(sections, [])
        self.assertEqual(attachments[0]["filename"], "notes.txt")

    def test_imap_multipart_attachment_does_not_expose_its_text_parts(self):
        raw = b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1)(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 14 1) "MIXED" NIL ("ATTACHMENT" ("FILENAME" "notes.mime"))) "MIXED")'
        sections, attachments = p._mime_sections(p._imap_tokens(raw))
        self.assertEqual([part["section"] for part in sections], ["1"])
        self.assertEqual(attachments, [{"filename": "notes.mime", "content_type": "multipart/mixed"}])

    def test_imap_literal_tokens_and_depth_limit(self):
        self.assertEqual(p._imap_tokens(b'("TEXT" {5}\r\nPLAIN NIL)'), ["TEXT", "PLAIN", None])
        with self.assertRaises(p.ProviderError):
            p._imap_tokens(b"(" * 40 + b"NIL" + b")" * 40)

    def test_gmail_plaintext_preferred_and_attachments_metadata_only(self):
        encode = lambda x: base64.urlsafe_b64encode(x.encode()).decode().rstrip("=")
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": encode("plain body")}},
            {"mimeType": "text/html", "body": {"data": encode("<p>html body</p>")}},
            {"mimeType": "text/plain", "filename": "secret.txt", "body": {"data": encode("DO NOT INDEX ATTACHMENT"), "size": 23}},
        ]}
        plain, html, attachments = p._gmail_body(payload)
        self.assertEqual(plain, ["plain body"])
        self.assertEqual(html, ["html body"])
        self.assertEqual(attachments[0]["filename"], "secret.txt")
        self.assertNotIn("DO NOT INDEX", json.dumps((plain, html, attachments)))

    def test_gmail_unnamed_text_attachment_remains_metadata_only(self):
        payload = {"mimeType": "text/plain", "filename": "",
                   "headers": [{"name": "Content-Disposition", "value": "Attachment"}],
                   "body": {"data": base64.urlsafe_b64encode(b"private").decode(), "size": 7}}
        plain, html, attachments = p._gmail_body(payload)
        self.assertEqual((plain, html), ([], []))
        self.assertEqual(attachments, [{"filename": "attachment", "content_type": "text/plain", "size": 7}])

    def test_unfetched_large_gmail_body_marked_incomplete(self):
        status = {"truncated": False}
        texts, htmls, attachments = p._gmail_body({"mimeType": "text/plain", "body": {"attachmentId": "large-body", "size": 999999}}, status=status)
        self.assertEqual(texts, [])
        self.assertTrue(status["truncated"])

    def test_gmail_body_text_truncation_is_disclosed(self):
        status = {"truncated": False}
        value = b"a" * (p.MAX_BODY + 10)
        payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(value).decode(), "size": len(value)}}
        texts, _, _ = p._gmail_body(payload, status=status)
        self.assertEqual(len(texts[0]), p.MAX_BODY)
        self.assertTrue(status["truncated"])

    def test_dates_normalized_to_utc(self):
        self.assertEqual(p._date("Thu, 29 Oct 2026 10:00:00 +0100"), "2026-10-29T09:00:00+00:00")
        self.assertEqual(p._date("29-Oct-2026 10:00:00 +0100"), "2026-10-29T09:00:00+00:00")

    def test_imap_fetch_only_selected_body_parts_and_retains_unread(self):
        fake = MagicMock()
        fake.response.return_value = ("UIDVALIDITY", [b"12345"])
        commands = []
        structure = b'1 (UID 5 FLAGS () INTERNALDATE "29-Oct-2026 10:00:00 +0100" BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 10 1 NIL NIL)("APPLICATION" "PDF" ("NAME" "file.pdf") NIL NIL "BASE64" 999 NIL ("ATTACHMENT" ("FILENAME" "file.pdf"))) "MIXED"))'
        def uid(command, *args):
            commands.append((command, args))
            if command == "search":
                return "OK", [b"5"]
            query = args[-1]
            if query == "(BODYSTRUCTURE FLAGS INTERNALDATE)":
                return "OK", [structure]
            if "HEADER.FIELDS" in query:
                return "OK", [imap_literal(IMAP_HEADER, b"Subject: Sample\r\nFrom: a@example.com\r\nTo: b@example.com\r\n"
                                          b"Reply-To: Reply <other@example.com>\r\nReferences: <root@example.com>\r\nMessage-ID: <sample@example.com>\r\n\r\n")]
            if "BODY.PEEK[1]" in query:
                return "OK", [imap_literal(b"1", b"Hello mail")]
            self.fail("unexpected fetch: " + query)
        fake.uid.side_effect = uid
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_imap(CREDS, 200, 30)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["remote_id"], "12345:5")
        self.assertEqual(messages[0]["reply_recipients"], ["other@example.com"])
        self.assertEqual(messages[0]["reply_references"], ["<root@example.com>"])
        self.assertEqual(messages[0]["reply_message_id"], "<sample@example.com>")
        self.assertEqual(messages[0]["body"], "Hello mail")
        self.assertTrue(messages[0]["unread"])
        self.assertFalse(messages[0]["body_truncated"])
        self.assertEqual(messages[0]["attachments"][0]["filename"], "file.pdf")
        self.assertFalse(any("BODY.PEEK[2]" in str(args) or "RFC822" in str(args) for _, args in commands))
        fake.logout.assert_called_once()

    def test_imap_cutoff_checks_exact_timestamp_before_body_download(self):
        fake = MagicMock()
        fake.response.return_value = ("UIDVALIDITY", [b"12345"])
        seen_queries = []
        def uid(command, *args):
            if command == "search":
                self.assertEqual(args[-1], "30-Sep-2026")
                return "OK", [b"5"]
            query = args[-1]
            seen_queries.append(query)
            if query == "(BODYSTRUCTURE FLAGS INTERNALDATE)":
                return "OK", [b'1 (UID 5 FLAGS () INTERNALDATE "01-Oct-2026 08:59:59 +0000" BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1))']
            if "HEADER.FIELDS" in query:
                return "OK", [imap_literal(IMAP_HEADER, b"Subject: Old\r\n\r\n")]
            self.fail("old message body must not be downloaded")
        fake.uid.side_effect = uid
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            self.assertEqual(p._fetch_imap(CREDS, 200, 30), [])
        self.assertFalse(any("BODY.PEEK[1]" in q for q in seen_queries))

    def test_imap_fetch_failure_cannot_replace_a_snapshot_with_missing_messages(self):
        fake = MagicMock()
        fake.response.return_value = ("UIDVALIDITY", [b"42"])
        fake.uid.side_effect = [("OK", [b"5"]), ("NO", [b"Private server detail"])]
        with patch.object(p, "_imap_open", return_value=fake), self.assertRaises(p.ProviderError) as failure:
            p._fetch_imap(CREDS, 1, 30)
        self.assertNotIn("Private server detail", str(failure.exception))
        fake.logout.assert_called_once()

    def test_imap_fills_the_limit_after_a_message_disappears(self):
        fake = MagicMock()
        fake.response.return_value = ("UIDVALIDITY", [b"42"])
        structure = b'1 (UID 2 FLAGS () INTERNALDATE "29-Oct-2026 10:00:00 +0000" BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1))'
        fake.uid.side_effect = [
            ("OK", [b"1 2 3"]), ("OK", [None]), ("OK", [structure]),
            ("OK", [imap_literal(IMAP_HEADER, b"Subject: Remaining\r\n\r\n")]), ("OK", [imap_literal(b"1", b"hello")]),
        ]
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_imap(CREDS, 1, 30)
        self.assertEqual([message["remote_id"] for message in messages], ["42:2"])
        self.assertEqual([call.args[1] for call in fake.uid.call_args_list[1:]], [b"3", b"2", b"2", b"2"])

    def test_imap_body_fetch_distinguishes_quoted_empty_and_expunged(self):
        client = MagicMock()
        client.uid.side_effect = [
            ("OK", [br'2 (UID 7 BODY[1]<0> "say \"hi\"")']),
            ("OK", [b'2 (UID 7 BODY[1]<0> "")']), ("OK", [None]),
        ]
        self.assertEqual(p._fetch_literal(client, b"7", "1", 20), b'say "hi"')
        self.assertEqual(p._fetch_literal(client, b"7", "1", 20), b"")
        self.assertIsNone(p._fetch_literal(client, b"7", "1", 20))

    def test_imap_matching_uid_requires_requested_structure_and_body(self):
        incomplete_fetch = ("OK", [b"3 (FLAGS () UID 7)"])
        client = MagicMock()
        client.response.return_value = ("UIDVALIDITY", [b"42"])
        client.uid.side_effect = [("OK", [b"7"]), incomplete_fetch]
        with patch.object(p, "_imap_open", return_value=client), self.assertRaises(p.ProviderError):
            p._fetch_imap(CREDS, 10, 30)
        wrong_section = ("OK", [imap_literal(b"2", b"other"), b" UID 7)"])
        client.uid.side_effect = [incomplete_fetch, wrong_section, ("OK", [b"3 (UID 70 FLAGS ())"])]
        for response in ("missing section", "wrong section"):
            with self.subTest(response=response), self.assertRaises(p.ProviderError):
                p._fetch_literal(client, b"7", "1", 20)
        self.assertIsNone(p._fetch_literal(client, b"7", "1", 20))

    def test_imap_expunged_during_body_fetch_does_not_leave_a_cached_message(self):
        fake = MagicMock()
        fake.response.return_value = ("UIDVALIDITY", [b"42"])
        structure = b'1 (UID 5 FLAGS () INTERNALDATE "29-Oct-2026 10:00:00 +0000" BODYSTRUCTURE ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 5 1))'
        fake.uid.side_effect = [
            ("OK", [b"5"]), ("OK", [structure]), ("OK", [imap_literal(IMAP_HEADER, b"Subject: Gone\r\n\r\n")]), ("OK", [None]),
        ]
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            self.assertEqual(p._fetch_imap(CREDS, 10, 30), [])


class SnapshotTests(unittest.TestCase):
    def graph_message(self, remote_id):
        return {"id": remote_id, "receivedDateTime": "2026-10-29T10:00:00Z",
                "body": {"contentType": "text", "content": "Synthetic message"}}

    def test_graph_malformed_collections_cannot_complete_a_snapshot(self):
        for page in ({}, {"value": {}}, {"value": ["invalid"]}):
            with self.subTest(page=page), patch.object(p, "_request_json", return_value=page):
                with self.assertRaises(p.ProviderError):
                    p._fetch_microsoft(None, {"access_token": "synthetic"}, 10, 30)
                with self.assertRaises(p.ProviderError):
                    p._graph_attachments(None, {}, "message")
        with patch.object(p, "_request_json", return_value={"value": [{}]}), self.assertRaises(p.ProviderError):
            p._fetch_microsoft(None, {"access_token": "synthetic"}, 10, 30)

    def test_graph_repeated_message_page_fails_instead_of_returning_partial_snapshot(self):
        next_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=repeat"
        responses = [{"value": [self.graph_message("first")], "@odata.nextLink": next_url},
                     {"value": [self.graph_message("second")], "@odata.nextLink": next_url}]
        with patch.object(p, "_request_json", side_effect=responses) as request, \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            with self.assertRaises(p.ProviderError):
                p._fetch_microsoft(None, {"access_token": "synthetic"}, 10, 30)
        self.assertEqual(request.call_count, 2)

    def test_graph_excessive_empty_pages_do_not_return_success(self):
        base = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        responses = [{"value": [], "@odata.nextLink": base + f"?$skiptoken={page}"}
                     for page in range(100)]
        responses.append({"value": [self.graph_message("too-late")]})
        with patch.object(p, "_request_json", side_effect=responses) as request, \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            with self.assertRaises(p.ProviderError):
                p._fetch_microsoft(None, {"access_token": "synthetic"}, 1, 30)
        self.assertEqual(request.call_count, 100)

    def test_graph_duplicate_messages_do_not_fill_the_snapshot_limit(self):
        next_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=second"
        responses = [{"value": [self.graph_message("first")], "@odata.nextLink": next_url},
                     {"value": [self.graph_message("first"), self.graph_message("second")]}]
        with patch.object(p, "_request_json", side_effect=responses), \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_microsoft(None, {"access_token": "synthetic"}, 2, 30)
        self.assertEqual([m["remote_id"] for m in messages], ["first", "second"])

    def test_graph_target_limit_finishes_without_following_unneeded_next_page(self):
        base = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        first = {**self.graph_message("first"), "from": {"emailAddress": {"address": "sender@example.test"}},
                 "replyTo": [{"emailAddress": {"address": "reply@example.test"}}]}
        with patch.object(p, "_request_json", return_value={"value": [first],
                                                           "@odata.nextLink": base}) as request, \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_microsoft(None, {"access_token": "synthetic"}, 1, 30)
        self.assertEqual([m["remote_id"] for m in messages], ["first"])
        self.assertIn("replyTo", request.call_args.kwargs["params"]["$select"].split(","))
        self.assertEqual(messages[0]["reply_recipients"], ["reply@example.test"])
        self.assertEqual(request.call_count, 1)

    def test_graph_last_allowed_page_can_complete_the_snapshot(self):
        base = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        responses = [{"value": [], "@odata.nextLink": base + f"?$skiptoken={page}"}
                     for page in range(99)]
        responses.append({"value": [self.graph_message("last")], "@odata.nextLink": base + "?$skiptoken=unused"})
        with patch.object(p, "_request_json", side_effect=responses) as request, \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_microsoft(None, {"access_token": "synthetic"}, 1, 30)
        self.assertEqual([m["remote_id"] for m in messages], ["last"])
        self.assertEqual(request.call_count, 100)

    def gmail_message(self, remote_id, *, date="2026-10-29T10:00:00+00:00", labels=None, payload=None):
        return {"id": remote_id, "internalDate": str(int(datetime.fromisoformat(date).timestamp() * 1000)),
                "labelIds": ["INBOX", "UNREAD"] if labels is None else labels,
                "payload": payload or {"mimeType": "text/plain", "body": {"data": "aGVsbG8="}}}

    def test_gmail_deleted_moved_and_old_messages_do_not_break_snapshot(self):
        seen = []
        def request(client, method, url, **kwargs):
            seen.append((url, kwargs))
            remote_id = url.rsplit("/", 1)[-1]
            if remote_id == "messages":
                return {"messages": [{"id": item} for item in ["removed", "moved", "old", "current"]]}
            self.assertTrue(kwargs["missing_ok"])
            return {"removed": None, "moved": self.gmail_message("moved", labels=["SENT"]),
                    "old": self.gmail_message("old", date="2026-10-01T08:59:59+00:00"), "current": self.gmail_message("current")}[remote_id]
        with patch.object(p, "_request_json", side_effect=request), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_gmail(None, {"access_token": "x"}, 200, 30)
        self.assertEqual([m["remote_id"] for m in messages], ["current"])
        self.assertEqual(seen[0][1]["params"]["q"], "in:inbox after:" + str(int(FIXED_CUTOFF.timestamp())))

    def test_gmail_follows_empty_pages_and_replaces_deleted_candidates(self):
        headers = [{"name": "From", "value": "From <from@example.com>"}, {"name": "Reply-To", "value": "reply@example.com"},
                   {"name": "Message-ID", "value": "<id@example.com>"}]
        remaining = self.gmail_message("remaining", payload={"mimeType": "text/plain", "headers": headers})
        remaining["threadId"] = "thread"
        responses = [{"nextPageToken": "second"}, {"messages": [{"id": "removed"}], "nextPageToken": "third"},
                     None, {"messages": [{"id": "remaining"}], "nextPageToken": "unused"}, remaining]
        with patch.object(p, "_request_json", side_effect=responses) as request, patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_gmail(None, {"access_token": "x"}, 1, 30)
        self.assertEqual([message["remote_id"] for message in messages], ["remaining"])
        self.assertEqual(messages[0]["reply_recipients"], ["reply@example.com"])
        self.assertEqual(messages[0]["reply_thread_id"], "thread")
        self.assertEqual(messages[0]["reply_message_id"], "<id@example.com>")
        self.assertEqual(request.call_count, 5)

    def test_gmail_repeated_empty_page_rejects_incomplete_sync(self):
        with patch.object(p, "_request_json", return_value={"nextPageToken": "again"}) as request:
            with self.assertRaises(p.ProviderError):
                p._fetch_gmail(None, {"access_token": "x"}, 1, 30)
        self.assertEqual(request.call_count, 2)

    def test_gmail_distinguishes_empty_and_malformed_collections(self):
        for page in ({}, {"messages": []}):
            with self.subTest(page=page), patch.object(p, "_request_json", return_value=page):
                self.assertEqual(p._fetch_gmail(None, {"access_token": "x"}, 1, 30), [])
        for items in ({}, "", None, False, ["invalid"]):
            with self.subTest(items=items), patch.object(p, "_request_json", return_value={"messages": items}):
                with self.assertRaises(p.ProviderError):
                    p._fetch_gmail(None, {"access_token": "x"}, 1, 30)

    def test_malformed_pagination_markers_cannot_end_a_snapshot(self):
        for marker in ({}, [], False, 42):
            for fetch, page in ((p._fetch_gmail, {"nextPageToken": marker}),
                                (p._fetch_microsoft, {"value": [], "@odata.nextLink": marker})):
                with self.subTest(marker=marker, provider=fetch.__name__), patch.object(p, "_request_json", return_value=page) as request:
                    with self.assertRaises(p.ProviderError):
                        fetch(None, {"access_token": "x"}, 1, 30)
                    self.assertEqual(request.call_count, 1)

    def test_gmail_attachment_only_message_does_not_use_snippet_as_body(self):
        raw = self.gmail_message("one", payload={"mimeType": "text/plain", "filename": "notes.txt",
                                                 "body": {"data": "cHJpdmF0ZQ==", "size": 7}})
        raw["snippet"] = "private"
        with patch.object(p, "_request_json", side_effect=[{"messages": [{"id": "one"}]}, raw]), \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            result = p._fetch_gmail(None, {"access_token": "x"}, 1, 30)
        self.assertEqual((result[0]["body"], result[0]["snippet"]), ("", ""))
        self.assertEqual(result[0]["attachments"][0]["filename"], "notes.txt")

    def test_gmail_complete_plain_alternative_not_flagged_for_unused_large_html(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": "aGVsbG8="}},
            {"mimeType": "text/html", "body": {"attachmentId": "too-large", "size": 999999}},
        ]}
        responses = [{"messages": [{"id": "one"}]}, self.gmail_message("one", payload=payload)]
        with patch.object(p, "_request_json", side_effect=responses), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            message = p._fetch_gmail(None, {"access_token": "x"}, 1, 30)[0]
        self.assertEqual(message["body"], "hello")
        self.assertFalse(message["body_truncated"])

    def test_graph_metadata_pagination_excludes_attachment_content(self):
        path = "https://graph.microsoft.com/v1.0/me/messages/mail-id/attachments"
        next_url = path + "?%24select=name%2CcontentType%2Csize&%24skiptoken=page2"
        responses = [{"value": [{"name": "a.pdf", "contentType": "application/pdf", "size": 10}], "@odata.nextLink": next_url},
                     {"value": [{"name": "b.txt", "contentType": "text/plain", "size": 2, "contentBytes": "never-export"}]}]
        with patch.object(p, "_request_json", side_effect=responses) as request:
            result = p._graph_attachments(None, {}, "mail-id")
        self.assertEqual([item["filename"] for item in result], ["a.pdf", "b.txt"])
        self.assertEqual(request.call_args_list[0].kwargs["params"], {"$select": "name,contentType,size"})
        self.assertNotIn("contentBytes", json.dumps(result))
        self.assertNotIn("never-export", json.dumps(result))

    def test_graph_attachment_page_cannot_widen_projection_or_change_resource(self):
        for next_url in ["https://graph.microsoft.com/v1.0/me/messages/mail-id/attachments?$skiptoken=x",
                         "https://graph.microsoft.com/v1.0/me/messages/other/attachments?$select=name,contentType,size",
                         "https://attacker.example/v1.0/me/messages/mail-id/attachments?$select=name,contentType,size"]:
            with self.subTest(next_url=next_url), patch.object(p, "_request_json", return_value={"value": [], "@odata.nextLink": next_url}) as request:
                with self.assertRaises(p.ProviderError):
                    p._graph_attachments(None, {}, "mail-id")
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
