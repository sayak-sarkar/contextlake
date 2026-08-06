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
from mcp.client.streamable_http import streamable_http_client

from .resilience import breaker_for, endpoint_key


class McpToolError(RuntimeError):
    """An MCP server answered a tool call with an error result.

    Distinct from a transport failure: the handshake worked, the tool ran, and the
    server reported that it failed. Carries the server's own text so a caller can
    say why rather than reporting an empty result.
    """

    def __init__(self, tool: str, detail: str):
        self.tool = tool
        self.detail = detail
        super().__init__(f"MCP tool {tool!r} failed: {detail or 'no detail given'}")


def _result_text(res: Any) -> str:
    return "".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))


def _parse_result(res: Any, tool: str = "") -> Any:
    """Extract a tool result as structured data, falling back to JSON/plain text.

    An error result raises :class:`McpToolError` rather than returning. MCP carries
    a failed tool call as ``isError`` with the reason in ``content``, so returning
    it made the error text indistinguishable from data -- and callers that iterate
    the result then found a *string*, silently yielded nothing, and reported an
    empty answer. That is how an unscoped Atlassian token read as "0 sites
    reachable" instead of "the server said no".
    """
    if getattr(res, "isError", False):
        raise McpToolError(tool, _result_text(res).strip())
    if res.structured_content:
        data = res.structured_content
        return data.get("result", data) if isinstance(data, dict) else data
    text = _result_text(res)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


async def _call_in_session(session, tool, arguments, timeout) -> Any:
    """Shared session body: handshake, invoke the tool, and parse the result."""
    await asyncio.wait_for(session.initialize(), timeout)
    res = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout)
    return _parse_result(res, tool)


async def _acall(command, args, tool, arguments, timeout, env, url=None):
    if url:
        async with streamable_http_client(url) as streams:
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


def server_key(command: str | None, args: Sequence[str], url: str | None) -> str:
    """A circuit-breaker key identifying the MCP server a call is aimed at.

    Every connector reaches its server through :func:`call_tool`, so keying the
    breaker here gives Atlassian, Figma, Slack and the generic MCP query path one
    shared, per-server health record for free -- rather than four copies of the
    same guard, which is the drift this codebase already refuses elsewhere (see
    :mod:`.http_base`).

    Built from the endpoint's scheme/host/port only, never its path, query or
    userinfo: a hosted MCP URL can carry a token and this key is printed in log
    lines. A stdio server is identified by its program name plus the host of the
    first URL-shaped argument, which is what distinguishes two ``mcp-remote``
    bridges from each other.
    """
    if url:
        return endpoint_key("mcp", url)
    program = (command or "?").rsplit("/", 1)[-1]
    for arg in args or ():
        text = str(arg)
        if text.startswith(("http://", "https://")):
            return endpoint_key(f"mcp:{program}", text)
    return f"mcp:{program}"


def call_tool(
    command: str | None = None, args: Sequence[str] = (), tool: str = "",
    arguments: dict | None = None, timeout: float = 90, env: dict | None = None,
    url: str | None = None,
) -> Any:
    """Call one tool on an MCP server and return its parsed result.

    Connects via stdio (spawning ``command``/``args``) unless ``url`` is given, in
    which case it connects to a hosted MCP server over streamable-HTTP instead.

    Guarded by a per-server circuit breaker (:mod:`.resilience`): a server that
    has failed repeatedly is skipped outright, with :class:`CircuitOpenError`,
    instead of costing every remaining repo in the run another full ``timeout``.
    """
    breaker = breaker_for(server_key(command, args, url))
    return breaker.call(
        lambda: asyncio.run(_acall(command, args, tool, arguments or {}, timeout, env, url)))


def list_tools(
    command: str, args: Sequence[str], timeout: float = 90, env: dict | None = None
) -> list[str]:
    """Spawn an MCP server and return the names of the tools it exposes."""
    return asyncio.run(_alist(command, args, timeout, env))
