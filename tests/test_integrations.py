"""Bridge/configuration security checks, without making mailbox connections."""
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mailhub.stdio_bridge import ALLOWED_TOOLS, ReadOnlyRemote, connection_settings, main

ROOT = Path(__file__).resolve().parents[1]


class BridgeConfigurationTests(unittest.TestCase):
    def test_requires_tls_and_env_credentials(self):
        environment = {"MAILHUB_MCP_TOKEN": "fixture-token-only"}
        self.assertEqual(connection_settings("https://mail.example.test/mcp", environment),
                         ("https://mail.example.test/mcp", "fixture-token-only"))
        self.assertEqual(connection_settings("http://127.0.0.1:8080/mcp", environment)[0],
                         "http://127.0.0.1:8080/mcp")
        for url in ("http://mail.example.test/mcp", "https://user:pw@mail.example.test/mcp",
                    "https://mail.example.test/mcp?token=bad", "https://mail.example.test/other"):
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


class BridgeForwardingTests(unittest.IsolatedAsyncioTestCase):
    def tool(self, name, read_only=True, destructive=False):
        return SimpleNamespace(name=name, annotations=SimpleNamespace(
            readOnlyHint=read_only, destructiveHint=destructive), inputSchema={"type": "object"})

    async def test_readonly_allowlist_and_rich_results_preserved(self):
        expected = object()
        remote = AsyncMock()
        remote.list_tools.return_value = SimpleNamespace(
            nextCursor=None, tools=[self.tool(name) for name in ALLOWED_TOOLS] + [self.tool("delete_email")])
        remote.call_tool.return_value = expected
        bridge = ReadOnlyRemote(remote)
        tools = await bridge.list_tools()
        self.assertEqual({tool.name for tool in tools}, ALLOWED_TOOLS)
        result = await bridge.call_tool("read_email", {"message_id": "fixture-id"})
        self.assertIs(result, expected)
        remote.call_tool.assert_awaited_once_with("read_email", {"message_id": "fixture-id"})
        with self.assertRaises(ValueError):
            await bridge.call_tool("delete_email", {})

    async def test_catalog_must_be_complete_and_readonly(self):
        for tools, cursor in (([], None), ([self.tool(name, False) for name in ALLOWED_TOOLS], None),
                              ([self.tool(name) for name in ALLOWED_TOOLS], "more")):
            session = AsyncMock()
            session.list_tools.return_value = SimpleNamespace(tools=tools, nextCursor=cursor)
            with self.assertRaises(ValueError):
                await ReadOnlyRemote(session).list_tools()


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("mailharbor_package", ROOT / "scripts/package.py")
        cls.packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.packager)

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
                "data/messages.json": "excluded",
            }
            for name, value in fixture.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            (root / "mailhub/private.py").symlink_to(root / ".env")
            actual = {str(relative) for _, relative in self.packager.source_files(root)}
            self.assertEqual(actual, {".github/workflows/ci.yml", "tests/ui_logic.cjs", ".env.example", "mailhub/app.py"})

    def test_oauth_plugin_generation_has_bound_resource_and_no_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "plugin"
            self.packager.make_plugin(output, "https://mail.example.test")
            manifest = json.loads((output / "mcp.json").read_text())
            server = manifest["mcpServers"]["mailharbor"]
            self.assertEqual(server["url"], "https://mail.example.test/mcp")
            auth = server["extensions"]["com.openai"]["auth"]
            self.assertEqual(auth["type"], "oauth")
            self.assertEqual(auth["resource"], server["url"])
            self.assertNotIn("clientSecret", json.dumps(manifest))


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("mailharbor_setup", ROOT / "scripts/setup.py")
        cls.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.setup)

    def test_password_limits_match_login_api(self):
        self.setup.validate_password("x" * 12)
        self.setup.validate_password("x" * 1024)
        for value in ("x" * 11, "x" * 1025):
            with self.assertRaises(ValueError):
                self.setup.validate_password(value)

    def test_url_is_canonical_for_browser_origin_checks(self):
        self.assertEqual(self.setup.validate_url("HTTPS://MAIL.Example.com:443/"),
                         ("https://mail.example.com", "mail.example.com"))
        self.assertEqual(self.setup.validate_url("http://[::1]:80"), ("http://[::1]", "[::1]"))
        self.assertEqual(self.setup.validate_url("https://münich.example"),
                         ("https://xn--mnich-kva.example", "xn--mnich-kva.example"))


if __name__ == "__main__":
    unittest.main()
