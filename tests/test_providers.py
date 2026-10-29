"""Provider safety/behavior tests; all network and mailbox access is mocked."""
import base64
import json
import socket
import ssl
import time
import unittest
from datetime import datetime, timezone
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
        self.assertIn("select_account", p.oauth_authorize_url("gmail", SETTINGS, "s", "c"))

    def test_token_refresh_rotates_and_preserves_existing_refresh(self):
        old = {"access_token": "old", "refresh_token": "refresh-old", "expires_at": 0}
        same = p._token_credentials("gmail", {"access_token": "new", "expires_in": 120}, old)
        self.assertEqual(same["refresh_token"], "refresh-old")
        new = p._token_credentials("gmail", {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 120}, old)
        self.assertEqual(new["refresh_token"], "refresh-new")
        self.assertEqual(old["refresh_token"], "refresh-old")
        self.assertGreater(new["expires_at"], time.time())

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
        fake = MagicMock()
        fake.capabilities = ("IMAP4REV1", "ID")
        fake.select.return_value = ("OK", [b"0"])
        with patch.object(p, "_PinnedIMAPSSL", return_value=fake):
            p.probe_imap(dict(CREDS, host="imap.163.com"))
        fake._simple_command.assert_called_once_with("ID", '("name" "MailHarbor" "version" "1.0")')


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
                return "OK", [(b"header", b"Subject: Sample\r\nFrom: a@example.com\r\nTo: b@example.com\r\n\r\n")]
            if "BODY.PEEK[1]" in query:
                return "OK", [(b"body", b"Hello mail")]
            self.fail("unexpected fetch: " + query)
        fake.uid.side_effect = uid
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_imap(CREDS, 200, 30)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["remote_id"], "12345:5")
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
                return "OK", [(b"header", b"Subject: Old\r\n\r\n")]
            self.fail("old message body must not be downloaded")
        fake.uid.side_effect = uid
        with patch.object(p, "_imap_open", return_value=fake), patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            self.assertEqual(p._fetch_imap(CREDS, 200, 30), [])
        self.assertFalse(any("BODY.PEEK[1]" in q for q in seen_queries))


class SnapshotTests(unittest.TestCase):
    def graph_message(self, remote_id):
        return {"id": remote_id, "receivedDateTime": "2026-10-29T10:00:00Z",
                "body": {"contentType": "text", "content": "Synthetic message"}}

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
        with patch.object(p, "_request_json", return_value={"value": [self.graph_message("first")],
                                                           "@odata.nextLink": base}) as request, \
             patch.object(p, "_cutoff", return_value=FIXED_CUTOFF):
            messages = p._fetch_microsoft(None, {"access_token": "synthetic"}, 1, 30)
        self.assertEqual([m["remote_id"] for m in messages], ["first"])
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
