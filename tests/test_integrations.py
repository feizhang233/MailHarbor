"""Bridge/configuration security checks, without making mailbox connections."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from integrations import rest_client
from mailhub.stdio_bridge import READ_TOOLS, MailHarborRemote, connection_settings, main
from scripts import package, setup


class BridgeConfigurationTests(unittest.TestCase):
    def test_requires_tls_and_env_credentials(self):
        environment = {"MAILHUB_MCP_TOKEN": "fixture-token-only"}
        self.assertEqual(connection_settings("https://mail.example.test/mcp", environment),
                         ("https://mail.example.test/mcp", "fixture-token-only"))
        self.assertEqual(connection_settings("http://127.0.0.1:8080/mcp", environment)[0],
                         "http://127.0.0.1:8080/mcp")
        self.assertEqual(connection_settings("HTTPS://MÜNICH.example:443/mcp/", environment)[0],
                         "https://xn--mnich-kva.example/mcp")
        for url in ("http://mail.example.test/mcp", "https://user:pw@mail.example.test/mcp",
                    "https://mail.example.test/mcp?token=bad", "https://mail.example.test/other",
                    "https://@mail.example.test/mcp", "https://mail.example.test:0/mcp",
                    "https://mail.example.test//mcp", "https://mail.example.test/mcp#"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                connection_settings(url, environment)
        for token in ("", "has spaces", "has\nnewline", "x" * 4097):
            with self.assertRaises(ValueError):
                connection_settings("https://mail.example.test/mcp", {"MAILHUB_MCP_TOKEN": token})

    def test_startup_error_has_no_stdout_or_secret(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"MAILHUB_MCP_TOKEN": ""}, clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["--url", "https://mail.example.test/mcp"])
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("MAILHUB_MCP_TOKEN", stderr.getvalue())


class RestClientTests(unittest.TestCase):
    def test_search_uses_canonical_https_origin_and_encoded_query(self):
        with (
            patch.dict("os.environ", {"MAILHUB_MCP_TOKEN": "fixture-token"}),
            patch("urllib.request.build_opener") as build_opener,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            opener = build_opener.return_value
            opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"messages": []}')
            self.assertEqual(rest_client.main(["--url", "HTTPS://MÜNICH.example:443/", "--query", "A&B"]), 0)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://xn--mnich-kva.example/v1/messages?q=A%26B&limit=20")
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")

    def test_invalid_origin_or_bearer_is_rejected_before_request(self):
        for origin, token in (("http://localhost:8080", "token"), ("https://@example.test", "token"),
                              ("https://mail.example.test", "a\x7fb"), ("https://mail.example.test", "x" * 4097)):
            with (
                self.subTest(origin=origin),
                patch.dict("os.environ", {"MAILHUB_MCP_TOKEN": token}),
                patch("urllib.request.build_opener") as build_opener,
            ):
                with self.assertRaises(SystemExit):
                    rest_client.main(["--url", origin])
                build_opener.assert_not_called()


class BridgeForwardingTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, name, read_only=True, destructive=False, external=False):
        return SimpleNamespace(name=name, annotations=SimpleNamespace(
            readOnlyHint=read_only, destructiveHint=destructive, openWorldHint=external), inputSchema={"type": "object"})

    async def test_readonly_allowlist_and_rich_results_preserved(self):
        expected = object()
        remote = AsyncMock()
        remote.list_tools.return_value = SimpleNamespace(
            nextCursor=None, tools=[self.tool(name) for name in READ_TOOLS] + [self.tool("delete_email")])
        remote.call_tool.return_value = expected
        bridge = MailHarborRemote(remote)
        tools = await bridge.list_tools()
        self.assertEqual({tool.name for tool in tools}, READ_TOOLS)
        result = await bridge.call_tool("read_email", {"message_id": "fixture-id"})
        self.assertIs(result, expected)
        remote.call_tool.assert_awaited_once_with("read_email", {"message_id": "fixture-id"})
        with self.assertRaises(ValueError):
            await bridge.call_tool("delete_email", {})

    async def test_only_scoped_consequential_reply_is_forwarded(self):
        remote = AsyncMock()
        catalog = [self.tool(name) for name in READ_TOOLS]
        remote.list_tools.return_value = SimpleNamespace(nextCursor=None, tools=catalog + [self.tool("reply_email")])
        bridge = MailHarborRemote(remote)
        with self.assertRaises(ValueError):
            await bridge.list_tools()
        remote.list_tools.return_value.tools[-1] = self.tool("reply_email", read_only=False, destructive=True, external=True)
        await bridge.list_tools()
        args = {"message_id": "fixture", "body": "收到", "request_id": "fixture"}
        await bridge.call_tool("reply_email", args)
        remote.call_tool.assert_awaited_once_with("reply_email", args)

    async def test_catalog_must_be_complete_and_readonly(self):
        for tools, cursor in (([], None), ([self.tool(name, False) for name in READ_TOOLS], None),
                              ([self.tool(name) for name in READ_TOOLS], "more")):
            session = AsyncMock()
            session.list_tools.return_value = SimpleNamespace(tools=tools, nextCursor=cursor)
            with self.assertRaises(ValueError):
                await MailHarborRemote(session).list_tools()


class PackagingTests(unittest.TestCase):
    def test_includes_ci_and_javascript_checks_but_never_runtime_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = {
                ".github/workflows/ci.yml": "name: CI",
                "tests/ui_logic.cjs": "// fixture",
                ".env": "SECRET=excluded",
                ".env.example": "SECRET=",
                "mailhub/app.py": "# source",
                "mailhub/local.sqlite": "excluded",
                "mailhub/.env": "excluded",
                "mailhub/local_settings.py": "excluded",
                "integrations/cursor.bearer.json": "excluded",
                "integrations/cursor.bearer.json.example": "{}",
                "deploy/production-verification.json": "excluded",
                "tests/private-mail.txt": "excluded",
                "data/messages.json": "excluded",
                "data/app.js": "excluded",
            }
            for name, value in fixture.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            (root / "mailhub/config.py").symlink_to(root / ".env")
            (root / "mailhub/static").symlink_to(root / "data", target_is_directory=True)
            actual = {str(relative) for _, relative in package.source_files(root)}
            self.assertEqual(actual, {
                ".github/workflows/ci.yml", "tests/ui_logic.cjs", ".env.example", "mailhub/app.py",
                "integrations/cursor.bearer.json.example",
            })

    def test_oauth_plugin_generation_has_bound_resource_and_no_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "plugin"
            package.make_plugin(output, "https://mail.example.test")
            manifest = json.loads((output / "mcp.json").read_text())
            server = manifest["mcpServers"]["mailharbor"]
            self.assertEqual(server["url"], "https://mail.example.test/mcp")
            auth = server["extensions"]["com.openai"]["auth"]
            self.assertEqual(auth["type"], "oauth")
            self.assertEqual(auth["resource"], server["url"])
            self.assertNotIn("clientSecret", json.dumps(manifest))


class SetupTests(unittest.TestCase):
    def test_password_limits_match_login_api(self):
        setup.validate_password("x" * 12)
        setup.validate_password("x" * 1024)
        for value in ("x" * 11, "x" * 1025):
            with self.assertRaises(ValueError):
                setup.validate_password(value)

    def test_url_is_canonical_for_browser_origin_checks(self):
        self.assertEqual(setup.validate_url("HTTPS://MAIL.Example.com:443/"),
                         ("https://mail.example.com", "mail.example.com"))
        self.assertEqual(setup.validate_url("http://[::1]:80"), ("http://[::1]", "[::1]"))
        self.assertEqual(setup.validate_url("https://münich.example"),
                         ("https://xn--mnich-kva.example", "xn--mnich-kva.example"))


if __name__ == "__main__":
    unittest.main()
