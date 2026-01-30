"""Minimal MCP client for querying external MCP servers.

Used by knowledge-source connectors to talk to a hosted MCP server (e.g. the
Atlassian Rovo MCP, reached via the ``mcp-remote`` stdio bridge). Each call spawns
the server command, performs the MCP handshake, invokes one tool, and returns the
parsed result. Authentication is the spawned command's concern (``mcp-remote``
handles OAuth and token caching), so no credentials live here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _parse_result(res: Any) -> Any:
    """Extract a tool result as structured data, falling back to JSON/plain text."""
    if res.structuredContent:
        data = res.structuredContent
        return data.get("result", data) if isinstance(data, dict) else data
    text = "".join(getattr(c, "text", "") for c in (res.content or []))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


async def _acall(command: str, args: Sequence[str], tool: str, arguments: dict, timeout: float):
    params = StdioServerParameters(command=command, args=list(args))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout)
            res = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout)
            return _parse_result(res)


async def _alist(command: str, args: Sequence[str], timeout: float) -> list[str]:
    params = StdioServerParameters(command=command, args=list(args))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout)
            tools = await asyncio.wait_for(session.list_tools(), timeout)
            return [t.name for t in tools.tools]


def call_tool(
    command: str, args: Sequence[str], tool: str,
    arguments: dict | None = None, timeout: float = 90,
) -> Any:
    """Spawn an MCP server, call one tool, and return its parsed result."""
    return asyncio.run(_acall(command, args, tool, arguments or {}, timeout))


def list_tools(command: str, args: Sequence[str], timeout: float = 90) -> list[str]:
    """Spawn an MCP server and return the names of the tools it exposes."""
    return asyncio.run(_alist(command, args, timeout))
