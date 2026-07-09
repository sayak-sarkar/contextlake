"""Minimal MCP client for querying external MCP servers.

Used by knowledge-source connectors to talk to an MCP server, either spawned over
stdio (e.g. the Atlassian Rovo MCP, reached via the ``mcp-remote`` stdio bridge) or
reached directly over streamable-HTTP (``url``). Each call performs the MCP
handshake, invokes one tool, and returns the parsed result. Authentication is the
transport's concern (the spawned command, or the HTTP endpoint itself), so no
credentials live here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


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


async def _call_in_session(session, tool, arguments, timeout) -> Any:
    """Shared session body: handshake, invoke the tool, and parse the result."""
    await asyncio.wait_for(session.initialize(), timeout)
    res = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout)
    return _parse_result(res)


async def _acall(command, args, tool, arguments, timeout, env, url=None):
    if url:
        async with streamablehttp_client(url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                return await _call_in_session(session, tool, arguments, timeout)

    params = StdioServerParameters(command=command, args=list(args or ()), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            return await _call_in_session(session, tool, arguments, timeout)


async def _alist(command, args, timeout, env) -> list[str]:
    params = StdioServerParameters(command=command, args=list(args), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout)
            tools = await asyncio.wait_for(session.list_tools(), timeout)
            return [t.name for t in tools.tools]


def call_tool(
    command: str | None = None, args: Sequence[str] = (), tool: str = "",
    arguments: dict | None = None, timeout: float = 90, env: dict | None = None,
    url: str | None = None,
) -> Any:
    """Call one tool on an MCP server and return its parsed result.

    Connects via stdio (spawning ``command``/``args``) unless ``url`` is given, in
    which case it connects to a hosted MCP server over streamable-HTTP instead.
    """
    return asyncio.run(_acall(command, args, tool, arguments or {}, timeout, env, url))


def list_tools(
    command: str, args: Sequence[str], timeout: float = 90, env: dict | None = None
) -> list[str]:
    """Spawn an MCP server and return the names of the tools it exposes."""
    return asyncio.run(_alist(command, args, timeout, env))
