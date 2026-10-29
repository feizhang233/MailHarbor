"""A stdio facade for MailHarbor's authenticated, read-only remote MCP tools.

Only protocol messages go to stdout. Credentials come from the process
environment, never command-line arguments or the plugin package.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import urlsplit

ALLOWED_TOOLS = frozenset({"list_accounts", "search_emails", "read_email", "get_sync_status"})


def connection_settings(url: str | None = None, environ=None) -> tuple[str, str]:
    environment = os.environ if environ is None else environ
    endpoint = url or environment.get("MAILHUB_MCP_URL", "")
    token = environment.get("MAILHUB_MCP_TOKEN", "")
    parsed = urlsplit(endpoint)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/mcp" or any(ch.isspace() for ch in endpoint)):
        raise ValueError("MAILHUB_MCP_URL must be an origin followed by /mcp, without credentials or query parameters.")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("The remote MCP endpoint must use HTTPS; HTTP is allowed only for loopback development.")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("The MCP endpoint has an invalid port.") from None
    if not token or len(token) > 4096 or not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        raise ValueError("Set a nonempty MAILHUB_MCP_TOKEN in the bridge process environment.")
    return endpoint.rstrip("/"), token


class ReadOnlyRemote:
    """Allowlist tools and preserve SDK result content, annotations and error flags."""

    def __init__(self, session):
        self.session = session
        self.tools = {}

    async def list_tools(self):
        result = await self.session.list_tools()
        # MailHarbor has four tools and does not need tool-list pagination.
        if result.nextCursor:
            raise ValueError("Unexpected paginated tool catalog from MailHarbor.")
        tools = {}
        for tool in result.tools:
            if tool.name not in ALLOWED_TOOLS:
                continue
            if (not tool.annotations or tool.annotations.readOnlyHint is not True
                    or tool.annotations.destructiveHint is True):
                raise ValueError("The remote tool catalog did not declare read-only access.")
            tools[tool.name] = tool
        if set(tools) != ALLOWED_TOOLS:
            raise ValueError("The remote endpoint does not expose the expected MailHarbor tools.")
        self.tools = tools
        return list(tools.values())

    async def call_tool(self, name, arguments):
        if name not in self.tools:
            raise ValueError("Tool is not in the read-only MailHarbor allowlist.")
        # ClientSession returns CallToolResult, preserving structuredContent,
        # content blocks and isError rather than flattening them to text.
        return await self.session.call_tool(name, arguments or {})


def _http_client_factory(headers=None, timeout=None, auth=None):
    import httpx
    return httpx.AsyncClient(headers=headers, timeout=timeout if timeout is not None else httpx.Timeout(30, read=120), auth=auth,
                             follow_redirects=False, trust_env=False)


async def serve(endpoint: str, token: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent

    async with streamablehttp_client(
        endpoint, headers={"Authorization": f"Bearer {token}"},
        timeout=30, sse_read_timeout=120, httpx_client_factory=_http_client_factory,
    ) as (remote_read, remote_write, _):
        async with ClientSession(remote_read, remote_write) as session:
            await session.initialize()
            remote = ReadOnlyRemote(session)
            await remote.list_tools()
            server = Server("mailharbor-bridge")

            @server.list_tools()
            async def list_tools():
                return list(remote.tools.values())

            @server.call_tool()
            async def call_tool(name: str, arguments: dict | None):
                try:
                    return await remote.call_tool(name, arguments)
                except Exception:
                    # Never send remote exception bodies, tokens or request URLs.
                    return CallToolResult(isError=True, content=[TextContent(
                        type="text", text="MailHarbor request failed. Check the endpoint, token and service status."
                    )])

            async with stdio_server() as (local_read, local_write):
                await server.run(local_read, local_write, server.create_initialization_options())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Expose a private MailHarbor MCP service over stdio.")
    parser.add_argument("--url", help="HTTPS /mcp endpoint (or MAILHUB_MCP_URL environment variable)")
    args = parser.parse_args(argv)
    try:
        endpoint, token = connection_settings(args.url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    logging.basicConfig(stream=sys.stderr, level=logging.CRITICAL)
    try:
        asyncio.run(serve(endpoint, token))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("MailHarbor bridge could not connect. Check its URL, credentials, dependencies and network.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
