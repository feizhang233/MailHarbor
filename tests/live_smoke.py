"""Real HTTP + stdio MCP + Chromium smoke, against synthetic CI mailbox data."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


async def bridge_smoke(origin, token, *, allow_reply=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "scripts/stdio_bridge.py"), "--url", origin + "/mcp"],
        env={"MAILHUB_MCP_TOKEN": token})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            expected = {"list_accounts", "get_sync_status", "search_emails", "read_email"}
            if allow_reply:
                expected.add("reply_email")
            assert {tool.name for tool in tools.tools} == expected
            result = await session.call_tool("search_emails", {"query": "CI fixture", "limit": 20})
            assert not result.isError, "Bridge search failed"
            payload = result.structuredContent
            if payload is None:
                payload = json.loads(next(block.text for block in result.content if block.type == "text"))
            assert payload["total"] == 1
            message_id = payload["messages"][0]["id"]
            read_result = await session.call_tool("read_email", {"message_id": message_id})
            assert not read_result.isError
            if allow_reply:
                args = {"message_id": message_id, "body": "CI bridge reply", "request_id": str(uuid4())}
                for _ in range(2):
                    reply = await session.call_tool("reply_email", args)
                    assert not reply.isError
                    receipt = reply.structuredContent or json.loads(next(block.text for block in reply.content if block.type == "text"))
                    assert receipt == {"accepted": True}, receipt


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
    page.locator("#account-smtp-host").fill("smtp.example.test")
    page.locator("#account-smtp-port").select_option("587")
    password.fill("fixture-work-code")
    with page.expect_request(lambda req: req.url.endswith("/api/accounts/imap") and req.method == "POST") as sent:
        submit.click()
    body = sent.value.post_data_json
    assert (body["provider"], body["host"], body["password"]) == ("custom", "mail.example.test", "fixture-work-code")
    assert (body["username"], body["folder"]) == ("staff-login", "Archive")
    assert (body["smtp_host"], body["smtp_port"]) == ("smtp.example.test", 587)
    expect(page.locator("#accounts-list")).to_contain_text("staff@example.test")
    expect(page.locator("#accounts-list")).to_contain_text("legacy@qq.com")

    add.click()
    address = page.locator("#account-email")
    address.fill("person@163.com")
    address.press("Tab")
    expect(picker).to_have_value("163")
    expect(host).to_have_value("imap.163.com")
    password.fill("synthetic-163-code")
    address.fill("person@163.COM")
    address.press("Tab")
    expect(password).to_have_value("synthetic-163-code")
    address.fill("person@VIP.126.COM")
    address.press("Tab")
    expect(picker).to_have_value("vip126")
    expect(host).to_have_value("imap.vip.126.com")
    expect(password).to_have_value("")
    address.fill("person@126.com.example.test")
    address.press("Tab")
    expect(picker).to_have_value("custom")
    expect(host).to_have_value("")
    address.fill("person@yeah.net")
    address.press("Tab")
    page.locator("#advanced-settings summary").click()
    page.locator("#use-custom-imap").click()
    host.fill("imap.example.test")
    address.fill("person@188.com")
    address.press("Tab")
    expect(host).to_have_value("imap.example.test")
    page.locator("#cancel-account").click()
    page.locator("#discard-account-confirm").click()

    add.click()
    password.fill("synthetic-188-code")
    address.fill("person@188.com")
    # A credential manager can fill both fields without blurring the address.
    with page.expect_request(lambda req: req.url.endswith("/api/accounts/imap")) as sent:
        page.locator("#account-form").evaluate("(form) => form.requestSubmit()")
    body = sent.value.post_data_json
    assert (body["provider"], body["host"], body["password"]) == ("188", "imap.188.com", "synthetic-188-code")
    expect(page.locator("#accounts-list")).to_contain_text("person@188.com")
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


def check_oauth_reply_opt_in(page, checkbox, button):
    from playwright.sync_api import expect
    path = "**/api/oauth/gmail/start"
    # Inspect the requested scope without leaving the local synthetic workspace.
    page.route(path, lambda route: route.fulfill(status=400, json={"detail": "Fixture login stopped"}))
    expect(checkbox).not_to_be_checked()
    for allowed in (False, True):
        checkbox.set_checked(allowed)
        with page.expect_response(lambda res: res.url.endswith("/api/oauth/gmail/start")) as result:
            button.click()
        assert result.value.request.post_data_json == {"allow_reply": allowed}
        expect(button).to_be_enabled()
    checkbox.uncheck()
    page.unroute(path)


def check_member_workspaces(browser, origin):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={"width": 320, "height": 700})
    page = context.new_page()
    password = "fixture-workspace-password"
    member_requests = []
    page.on("request", lambda request: member_requests.append(request.url))
    try:
        page.goto(origin)
        page.locator("#login-language").select_option("en")
        page.locator("#auth-switch").click()
        expect(page.locator("#login-username")).to_be_focused()
        page.locator("#login-username").fill("Member-A")
        page.locator("#login-password").fill(password)
        page.locator("#login-password").press("Enter")
        expect(page.locator("#workspace-username")).to_contain_text("member-a")
        expect(page.locator("#account-count")).to_have_text("0")
        page.locator('nav a[href="#accounts"]').click()
        page.locator("#page-accounts [data-open-account]").click()
        page.locator("#provider-select").select_option("gmail")
        check_oauth_reply_opt_in(page, page.locator("#mailbox-allow-reply"), page.locator("#mailbox-oauth-login"))
        page.locator("#provider-select").select_option("qq")
        page.locator("#account-email").fill("signup-fixture@example.test")
        page.locator("#account-password").fill("synthetic-mailbox-password")
        page.locator("#account-form button[type=submit]").click()
        expect(page.locator("#accounts-list")).to_contain_text("signup-fixture@example.test")
        page.locator('nav a[href="#settings"]').click()
        expect(page.locator("#category-sync")).to_be_hidden()
        expect(page.locator("#category-service")).to_be_hidden()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        other_tab = context.new_page()
        other_tab.goto(origin + "/#accounts")
        expect(other_tab.locator("#accounts-list")).to_contain_text("signup-fixture@example.test")
        page.locator("#logout").click()
        expect(other_tab.locator("#login-view")).to_be_visible()
        page.locator("#auth-switch").click()
        page.locator("#login-username").fill("member-a")
        page.locator("#login-password").fill(password)
        page.locator("#auth-submit").click()
        expect(page.locator("#login-username-error")).not_to_be_empty()
        page.locator("#login-username").fill("Member-B")
        page.locator("#auth-submit").click()
        expect(page.locator("#workspace-username")).to_contain_text("member-b")
        page.locator('nav a[href="#accounts"]').click()
        expect(page.locator("#account-count")).to_have_text("0")
        expect(page.locator("#accounts-list")).not_to_contain_text("signup-fixture")
        expect(other_tab.locator("#workspace-username")).to_contain_text("member-b")
        expect(other_tab.locator("#accounts-list")).not_to_contain_text("signup-fixture")

        page.locator("#logout").click()
        page.locator("#login-username").fill("MEMBER-A")
        page.locator("#login-password").fill(password)
        page.locator("#auth-submit").click()
        expect(page.locator("#workspace-username")).to_contain_text("member-a")
        page.locator('nav a[href="#accounts"]').click()
        expect(page.locator("#accounts-list")).to_contain_text("signup-fixture@example.test")
        assert origin + "/api/settings" not in member_requests
        assert origin + "/api/diagnostics" not in member_requests
    finally:
        context.close()


def check_web_reply(page):
    from playwright.sync_api import expect
    from uuid import UUID

    page.locator("#open-reply").click()
    body = page.locator("#reply-body")
    send = page.locator("#send-reply")
    expect(body).to_be_focused()
    expect(page.locator("#reply-to")).to_have_text("replies@example.test")
    expect(page.locator("#reply-from")).to_have_text("fixture@example.test")
    send.click()
    expect(body).to_have_attribute("aria-invalid", "true")
    body.fill("CI reply\nSecond line")
    page.keyboard.press("Escape")
    expect(page.locator("#confirm-cancel")).to_be_focused()
    page.locator("#confirm-cancel").click()
    expect(body).to_have_value("CI reply\nSecond line")

    page.set_viewport_size({"width": 320, "height": 640})
    assert page.locator("#reader-dialog").evaluate("(el) => el.scrollWidth <= el.clientWidth + 2")
    held = []
    page.route("**/api/messages/*/reply", lambda route: held.append(route))
    with page.expect_request(lambda req: req.url.endswith("/reply")):
        send.click()
    expect(send).to_be_disabled()
    expect(page.locator("#close-reader")).to_be_disabled()
    request = held.pop()
    payload = request.request.post_data_json
    assert payload["body"] == "CI reply\nSecond line" and UUID(payload["request_id"]).version == 4
    # Simulate a lost success response after the synthetic provider accepted the message.
    assert request.fetch().json() == {"accepted": True}
    request.abort("connectionreset")
    expect(page.locator("#reply-error")).to_contain_text("尚未確認")
    expect(body).not_to_be_editable()
    page.unroute("**/api/messages/*/reply")
    with page.expect_request(lambda req: req.url.endswith("/reply")) as retried:
        send.click()
    assert retried.value.post_data_json == payload
    expect(page.locator("#reply-status")).to_have_text("寄件服務已接受回覆。")
    expect(page.locator("#reply-form")).not_to_be_visible()
    expect(body).to_have_value("")
    page.set_viewport_size({"width": 1440, "height": 1000})


def browser_smoke(origin, password):
    from playwright.sync_api import sync_playwright, expect
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=os.getenv("MAILHARBOR_TEST_CHROMIUM"))
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
        check_web_reply(page)
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
        check_oauth_reply_opt_in(page, page.locator("#google-allow-reply"), page.locator("#google-login"))
        page.locator("#category-service").click()
        page.locator("#run-diagnostics").click()
        expect(page.locator("#diagnostics-results")).not_to_contain_text("尚未執行檢查")
        page.locator("#category-mcp").click()
        expect(page.locator("#group-mcp code")).to_have_text(["list_accounts", "get_sync_status", "search_emails", "read_email", "reply_email"])
        page.locator('#group-mcp a[href="#connect"]').click()
        expect(page.locator("#ai-client")).to_be_visible()
        page.locator("#ai-client").select_option("deepseek")
        expect(page.locator("#ai-auth")).to_have_value("token")
        expect(page.locator("#ai-auth")).to_be_disabled()
        expect(page.locator("#endpoint")).to_have_value(origin + "/mcp")
        expect(page.locator("#codex-config")).to_contain_text("process.env.MAILHUB_MCP_TOKEN")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        page.locator("#copy-config").click()
        assert page.evaluate("navigator.clipboard.readText()") == page.locator("#codex-config").text_content()
        page.set_viewport_size({"width": 320, "height": 640})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), "DeepSeek setup overflows at 320px"
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#ai-client").select_option("codex")
        expect(page.locator("#ai-auth")).to_be_enabled()
        page.locator("#open-mcp-guide").click()
        expect(page.locator("#guide-title")).to_be_focused()
        page.locator('nav a[href="#connect"]').click()
        page.locator("#token-name").fill("CI browser token")
        expect(page.locator("#token-allow-reply")).not_to_be_checked()
        page.locator("#token-allow-reply").check()
        page.locator("#token-form button[type=submit]").click()
        expect(page.locator("#secret-dialog")).to_be_visible()
        assert any(key["name"] == "CI browser token" and "mail.reply" in key["scope"].split()
                   for key in page.request.get(origin + "/api/tokens").json()["tokens"])
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
        check_member_workspaces(browser, origin)
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
            asyncio.run(bridge_smoke(origin, fixture["reply_token"], allow_reply=True))
            browser_smoke(origin, fixture["password"])
            print("Live fixture smoke passed: HTTP app, stdio MCP bridge, signup, isolated workspaces, login, mailbox routing, reply acceptance and retry, scoped keys, settings, language switching and responsive navigation.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
