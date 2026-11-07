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


def connect_imap_mailboxes(page):
    from playwright.sync_api import expect
    page.locator('nav a[href="#accounts"]').click()
    add = page.locator("#page-accounts [data-open-account]")
    picker = page.locator("#provider-select")
    host = page.locator("#account-host")
    password = page.locator("#account-password")
    submit = page.locator("#account-form button[type=submit]")

    add.click()
    picker.select_option("qq")
    expect(host).to_have_value("imap.qq.com")
    page.locator("#account-email").fill("legacy@qq.com")
    password.fill("fixture-qq-code")
    held = []
    page.route("**/api/accounts/imap", lambda route: held.append(route))
    page.locator("#cancel-account").click()
    expect(page.locator("#discard-account")).to_be_visible()
    submit.click()
    expect(page.locator("#keep-account")).to_be_focused()
    assert not held, "Choosing whether to discard must not submit credentials"
    page.locator("#keep-account").click()
    with page.expect_request(lambda req: req.url.endswith("/api/accounts/imap")):
        submit.click()
    expect(page.locator("#account-email")).to_be_disabled()
    expect(page.locator("#close-account")).to_be_disabled()
    page.keyboard.press("Escape")
    expect(page.locator("#account-dialog")).to_be_visible()
    held.pop().fulfill(status=200, content_type="text/html", body="<html>Gateway response</html>")
    page.unroute("**/api/accounts/imap")
    expect(page.locator("#account-error")).to_contain_text("操作結果尚未確認")
    expect(page.locator("#account-email")).to_be_enabled()
    expect(password).to_have_value("fixture-qq-code")
    submit.click()
    expect(page.locator("#accounts-list")).to_contain_text("legacy@qq.com")

    desktop_size = page.viewport_size
    page.set_viewport_size({"width": 390, "height": 844})
    add.click()
    expect(picker).to_have_value("custom")
    expect(host).to_be_editable()
    expect(host).to_have_value("")
    picker.select_option("qq")
    page.locator("#account-email").fill("staff@example.test")
    page.locator("#account-label").fill("Work mailbox")
    password.fill("stale-qq-code")
    picker.select_option("163")
    expect(picker).to_have_value("163")
    expect(host).to_have_value("imap.163.com")
    expect(password).to_have_value("")
    expect(page.locator("#discard-account")).not_to_be_visible()

    page.locator("#advanced-settings summary").click()
    expect(host).not_to_be_editable()
    page.locator("#account-username").fill("staff-login")
    page.locator("#account-folder").fill("Archive")
    page.locator("#use-custom-imap").click()
    expect(picker).to_have_value("custom")
    expect(host).to_be_focused()
    expect(host).to_be_editable()
    expect(host).to_have_value("")
    expect(page.locator("#account-email")).to_have_value("staff@example.test")
    expect(page.locator("#account-label")).to_have_value("Work mailbox")
    host.fill("mail.example.test")
    password.fill("fixture-work-code")
    with page.expect_request(lambda req: req.url.endswith("/api/accounts/imap") and req.method == "POST") as sent:
        submit.click()
    body = sent.value.post_data_json
    assert (body["provider"], body["host"], body["password"]) == ("custom", "mail.example.test", "fixture-work-code")
    assert (body["username"], body["folder"]) == ("staff-login", "Archive")
    expect(page.locator("#accounts-list")).to_contain_text("staff@example.test")
    expect(page.locator("#accounts-list")).to_contain_text("legacy@qq.com")
    page.set_viewport_size(desktop_size)


def check_oauth_routing(page):
    from playwright.sync_api import expect

    add = page.locator("#page-accounts [data-open-account]")
    picker = page.locator("#provider-select")
    for provider, service in [("gmail", "google"), ("microsoft", "microsoft")]:
        page.locator('nav a[href="#accounts"]').click()
        add.click()
        picker.select_option(provider)
        expect(page.locator("#account-dialog")).not_to_be_visible()
        expect(page.locator("#" + service + "-settings-title")).to_be_focused()
        expect(page.locator("#" + service + "-login")).to_be_disabled()

    for email, service in [("Person@GMAIL.com", "google"), ("person@outlook.com", "microsoft")]:
        page.locator('nav a[href="#accounts"]').click()
        add.click()
        page.locator("#account-email").fill(email)
        page.locator("#account-email").press("Tab")
        expect(page.locator("#" + service + "-settings-title")).to_be_focused()

    page.locator('nav a[href="#accounts"]').click()
    add.click()
    page.locator("#account-email").fill("owner@gmail.com.example.test")
    page.locator("#account-email").press("Tab")
    expect(page.locator("#account-dialog")).to_be_visible()
    page.locator("#account-password").fill("fixture-only")
    picker.select_option("gmail")
    expect(page.locator("#discard-account")).to_be_visible()
    page.locator("#keep-account").click()
    expect(page.locator("#account-password")).to_have_value("fixture-only")
    picker.select_option("microsoft")
    page.locator("#discard-account-confirm").click()
    expect(page.locator("#microsoft-settings-title")).to_be_focused()
    expect(page.locator("#account-password")).to_have_value("")


def check_sidebar_layout(page):
    size = page.viewport_size
    for width, height in [(1024, 500), (780, 360), (640, 500), (390, 844), (320, 640)]:
        page.set_viewport_size({"width": width, "height": height})
        logout = page.locator("#logout")
        logout.scroll_into_view_if_needed()
        logout.click(trial=True)
        assert page.evaluate("""() => {
            const controls = document.querySelectorAll('.sidebar .wordmark, .main-nav a, #logout');
            const boxes = [...controls].map((node) => node.getBoundingClientRect());
            return boxes.every((a, i) => boxes.slice(i + 1).every((b) =>
                a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top));
        }"""), f"Sidebar controls overlap at {width} × {height}"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), f"Page overflows at {width}px"
    page.set_viewport_size(size)


def check_languages(browser, origin, password):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        page.goto(origin)
        languages = page.locator("#login-language")
        expect(languages).to_be_visible()
        for code in languages.locator("option").evaluate_all("(items) => items.map(item => item.value)"):
            languages.select_option(code)
            expect(page.locator("html")).to_have_attribute("lang", code)
            expect(page.locator("#login-language")).to_have_value(code)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), f"Login overflow in {code}"
        languages.select_option("en")
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        page.reload()
        expect(languages).to_have_value("en")
        page.locator("#login-password").fill(password)
        page.locator("#login-form button[type=submit]").click()
        expect(page.locator("#inbox-title")).to_have_text("Unified inbox")
        page.locator('nav a[href="#settings"]').click()
        page.locator("#category-accounts").click()
        page.locator("#google-client-id").fill("draft-client-id")
        page.locator("#category-display").click()
        page.locator("#settings-language").select_option("fr")
        expect(page.locator("#settings-title")).to_have_text("Paramètres")
        page.locator("#category-accounts").click()
        expect(page.locator("#google-client-id")).to_have_value("draft-client-id")
        expect(page.locator("#settings-dirty")).not_to_have_text("You have unsaved changes.")
        page.locator("#discard-settings").click()
        page.locator("#confirm-action").click()
        expect(page.locator("#google-client-id")).not_to_have_value("draft-client-id")
        page.set_viewport_size({"width": 320, "height": 640})
        page.locator("#category-display").click()
        page.locator("#settings-language").select_option("de")
        page.locator("#category-accounts").click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "German mailbox category overflows at 320px"
        page.locator("#category-display").click()
        page.locator("#settings-language").select_option("fr")
        page.locator('nav a[href="#inbox"]').click()
        expect(page.locator("#mail-list")).to_contain_text("CI fixture <script>")
        expect(page.locator("#coverage")).not_to_contain_text("同步目標")
        page.locator("#logout").click()
        expect(languages).to_have_value("fr")
        assert page.evaluate("Object.keys(localStorage)") == ["mailharbor.language"]
    finally:
        context.close()


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
        connect_imap_mailboxes(page)
        check_oauth_routing(page)
        check_sidebar_layout(page)
        page.locator('nav a[href="#settings"]').click()
        page.locator("#category-sync").click()
        expect(page.locator("#sync-days")).to_be_enabled()
        page.locator("#sync-days").fill("400")
        page.locator("#category-accounts").click()
        page.locator("#select-google").click()
        expect(page.locator("#google-login")).to_be_disabled()
        page.locator("#google-client-id").fill("synthetic-client-id")
        page.locator("#google-client-secret").fill("synthetic-client-secret")
        page.locator("#select-microsoft").click()
        expect(page.locator("#microsoft-login")).to_be_visible()
        page.locator("#save-settings").click()
        expect(page.locator("#sync-days")).to_be_focused()
        page.locator("#sync-days").fill("45")
        page.locator("#category-accounts").click()
        page.locator("#select-google").click()
        expect(page.locator("#google-client-secret")).to_have_value("synthetic-client-secret")
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
        expect(page.locator("#google-login")).to_be_enabled()
        page.locator("#category-service").click()
        page.locator("#run-diagnostics").click()
        expect(page.locator("#diagnostics-results")).not_to_contain_text("尚未執行檢查")
        page.locator("#category-mcp").click()
        expect(page.locator("#group-mcp code")).to_have_text(["list_accounts", "get_sync_status", "search_emails", "read_email"])
        page.locator('#group-mcp a[href="#connect"]').click()
        expect(page.locator("#ai-client")).to_be_visible()
        page.locator("#open-mcp-guide").click()
        expect(page.locator("#guide-title")).to_be_focused()
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
        check_languages(browser, origin, password)
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
            print("Live fixture smoke passed: HTTP app, stdio MCP bridge, login, mailbox routing, settings, language switching and responsive navigation.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
