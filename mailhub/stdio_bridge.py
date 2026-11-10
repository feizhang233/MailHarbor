"""A stdio facade for MailHarbor's authenticated, permission-scoped MCP tools.

Only protocol messages go to stdout. Credentials come from the process
environment, never command-line arguments or the plugin package.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

if __package__:
    from .config import client_bearer_token, normalize_origin
else:  # Direct invocation of this file.
    from config import client_bearer_token, normalize_origin

READ_TOOLS = frozenset({"list_accounts", "search_emails", "read_email", "get_sync_status"})


def connection_settings(url: str | None = None, environ=None) -> tuple[str, str]:
    environment = os.environ if environ is None else environ
    endpoint = url or environment.get("MAILHUB_MCP_URL", "")
    origin, _, service = endpoint.rstrip("/").rpartition("/")
    if service != "mcp" or origin.endswith("/") or any(ch.isspace() for ch in endpoint):
        raise ValueError("MAILHUB_MCP_URL must be a service origin followed by /mcp.")
    try:
        origin = normalize_origin(origin)
    except ValueError:
        raise ValueError("Use an HTTPS origin without credentials, or loopback HTTP for development.") from None
    return origin + "/mcp", client_bearer_token(environment)


class MailHarborRemote:
    """Allowlist tools and preserve SDK result content, annotations and error flags."""

    def __init__(self, session):
        self.session = session
        self.tools = {}

    async def list_tools(self):
        result = await self.session.list_tools()
        if result.nextCursor:
            raise ValueError("Unexpected paginated tool catalog from MailHarbor.")
        tools = {}
        for tool in result.tools:
            if tool.name == "reply_email":
                if (not tool.annotations or tool.annotations.readOnlyHint is not False
                        or tool.annotations.destructiveHint is not True or tool.annotations.openWorldHint is not True):
                    raise ValueError("The reply tool must declare an external send operation.")
            elif tool.name not in READ_TOOLS:
                continue
            elif (not tool.annotations or tool.annotations.readOnlyHint is not True
                    or tool.annotations.destructiveHint is True):
                raise ValueError("The remote tool catalog did not declare read-only access.")
            tools[tool.name] = tool
        if not READ_TOOLS.issubset(tools):
            raise ValueError("The remote endpoint does not expose the expected MailHarbor tools.")
        self.tools = tools
        return list(tools.values())

    async def call_tool(self, name, arguments):
        if name not in self.tools:
            raise ValueError("Tool is not available for this MailHarbor connection.")
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
            remote = MailHarborRemote(session)
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
