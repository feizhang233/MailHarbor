"""Real HTTP + stdio MCP + Chromium smoke, against synthetic CI mailbox data."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


async def bridge_smoke(origin, token):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "scripts/stdio_bridge.py"), "--url", origin + "/mcp"],
        env={"MAILHUB_MCP_TOKEN": token})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"list_accounts", "get_sync_status", "search_emails", "read_email"}
            result = await session.call_tool("search_emails", {"query": "CI fixture", "limit": 20})
            assert not result.isError, "Bridge search failed"
            payload = result.structuredContent
            if payload is None:
                payload = json.loads(next(block.text for block in result.content if block.type == "text"))
            assert payload["total"] == 1
            message_id = payload["messages"][0]["id"]
            read_result = await session.call_tool("read_email", {"message_id": message_id})
            assert not read_result.isError


def browser_smoke(origin, password):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin, wait_until="networkidle")
        expect(page.locator("#login-view")).to_be_visible()
        page.locator("#login-password").fill("wrong-password")
        page.locator("#login-form button[type=submit]").click()
        expect(page.locator("#login-error")).not_to_be_empty()
        page.locator("#login-password").fill(password)
        page.locator("#login-form button[type=submit]").click()
        expect(page.locator("#app-view")).to_be_visible()
        page.locator("#mail-search").fill("CI fixture")
        expect(page.locator("#mail-list button")).to_have_count(1)
        page.locator("#mail-list button").first.click()
        expect(page.locator("#reader-dialog")).to_be_visible()
        expect(page.locator("#reader-content")).to_contain_text("CI fixture body")
        assert page.evaluate("window.__mailXss") is None
        page.locator("#close-reader").click()
        page.locator('nav a[href="#settings"]').click()
        expect(page.locator("#sync-days")).to_be_enabled()
        page.locator("#sync-days").fill("45")
        page.locator("#google-client-id").fill("synthetic-client-id")
        page.locator("#google-client-secret").fill("synthetic-client-secret")
        pending_saves = []

        def hold_settings_save(route):
            if route.request.method == "POST":
                pending_saves.append(route)
            else:
                route.continue_()

        page.route("**/api/settings", hold_settings_save)
        page.locator("#save-settings").click()
        expect(page.locator("#save-settings")).to_be_disabled()
        page.locator("#logout").click()
        expect(page.locator("#app-view")).to_be_visible()
        expect(page.locator("#confirm-dialog")).not_to_be_visible()
        assert len(pending_saves) == 1, "Settings save must remain pending during the logout check"
        pending_saves[0].continue_()
        page.unroute("**/api/settings", hold_settings_save)
        expect(page.locator("#google-client-secret")).to_have_value("")
        expect(page.locator("#save-settings")).to_be_enabled()
        settings = page.request.get(origin + "/api/settings").json()
        assert settings["sync_days"] == 45 and settings["google"]["secret_configured"] is True
        assert "synthetic-client-secret" not in json.dumps(settings)
        page.locator("#run-diagnostics").click()
        expect(page.locator("#diagnostics-results")).not_to_contain_text("尚未執行檢查")
        page.locator('nav a[href="#connect"]').click()
        page.locator("#token-name").fill("CI browser token")
        page.locator("#token-form button[type=submit]").click()
        expect(page.locator("#secret-dialog")).to_be_visible()
        assert page.locator("#new-token").input_value().startswith("mh_")
        page.locator("#saved-token").click()
        expect(page.locator("#new-token")).to_have_value("")
        row = page.locator("#tokens-list").locator("article, .token-row, .token-item").filter(has_text="CI browser token")
        if row.count() == 0:
            row = page.locator("#tokens-list > *").filter(has_text="CI browser token")
        row.get_by_role("button").click()
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.locator("#confirm-action").click()
        expect(page.locator("#tokens-list")).not_to_contain_text("CI browser token")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('nav a[href="#settings"]').click()
        expect(page.locator("#settings-title")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "Narrow viewport overflows"
        page.locator("#logout").click()
        expect(page.locator("#login-view")).to_be_visible()
        assert not errors, "Browser raised JavaScript errors: " + "; ".join(errors)
        context.close()
        browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="mailharbor-ci-") as temporary:
        data = Path(temporary)
        with socket.socket() as socket_:
            socket_.bind(("127.0.0.1", 0))
            port = socket_.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, str(ROOT / "tests/live_fixture.py"), "--port", str(port), "--data-dir", str(data)],
                                  cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("Fixture server exited before startup: " + server.stderr.read())
                try:
                    with urllib.request.urlopen(origin + "/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Fixture server startup timed out")
            fixture = json.loads((data / "fixture.json").read_text())
            asyncio.run(bridge_smoke(origin, fixture["token"]))
            browser_smoke(origin, fixture["password"])
            print("Live fixture smoke passed: HTTP app, stdio MCP bridge, browser login/settings/mail/token/mobile flows.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
