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
import hashlib
import json
from collections.abc import Sequence
from typing import Any, NamedTuple

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

    def __reduce__(self):
        """Rebuild from the two values ``__init__`` needs.

        The default ``cls(*self.args)`` is one argument short and raises
        TypeError. See ``kb.parse.RepoTooLarge.__reduce__``.
        """
        return (self.__class__, (self.tool, self.detail))


class McpProbeShapeError(ValueError):
    """The SDK handed back a ``tools/list`` result object with no ``tools``.

    Narrow, and it is the *second* way a malformed answer arrives, not the
    first. Measured on this build against a raw JSON-RPC server: answering
    ``tools/list`` with ``{"result": {}}`` or with ``{"tools": "not-a-list"}``
    never reaches the check below at all, because the SDK validates the frame
    into ``ListToolsResult`` first and raises ``pydantic.ValidationError`` two
    ``ExceptionGroup``s deep. So a classifier that watched only for this class
    called every real malformed answer a rejection. See
    :func:`.capabilities.probe_tools`, which reads both.

    This class still earns its place: it covers the case where the SDK stops
    validating, or returns some other object, and it is raised on this module's
    own type rather than an SDK exception name because the SDK renamed
    ``McpError`` to ``MCPError`` between majors.
    """


class ToolInfo(NamedTuple):
    """One tool a server advertises: its name, its one-line help, its schema."""

    name: str
    description: str
    input_schema: dict


class ToolList(NamedTuple):
    """Every tool one ``tools/list`` answer carried, plus whether it was page one.

    ``truncated`` is the load-bearing half. A bare list cannot say "this is not
    all of them", and a partial list read as complete turns "that tool is not
    advertised" into a false claim about a tool that is there.
    """

    tools: list[ToolInfo]
    truncated: bool


def _result_text(res: Any) -> str:
    return "".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))


def _parse_result(res: Any, tool: str = "") -> Any:
    """Extract a tool result as structured data, falling back to JSON/plain text.

    An error result raises :class:`McpToolError` rather than returning. MCP carries
    a failed tool call as ``is_error`` with the reason in ``content``, so returning
    it made the error text indistinguishable from data -- and callers that iterate
    the result then found a *string*, silently yielded nothing, and reported an
    empty answer. That is how an unscoped Atlassian token read as "0 sites
    reachable" instead of "the server said no".

    The field is ``is_error``, not ``isError``. mcp 2.0 renamed both
    ``CallToolResult`` fields to snake_case (see the note in ``pyproject.toml``);
    line 63's ``structured_content`` was updated and this read was not, so on
    mcp>=2.0 the ``getattr`` default won every time and :class:`McpToolError`
    was never raised at all. Measured against a spawned mock server: calling a
    tool that does not exist returned the string "Unknown tool: ..." as data.
    """
    if getattr(res, "is_error", False):
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


def _spawn_env(env: dict | None) -> dict[str, str] | None:
    """The overrides for a stdio spawn, normalised. Never an ambient snapshot.

    Callers pass **only what they override**, because that is the only part that
    says which server they reach (see :func:`server_key`). This returns those
    overrides and nothing else.

    An earlier version merged ``os.environ`` in here, on the stated reason that
    "the MCP SDK replaces the child environment outright when it is given one".
    That reason is false, and it is worth recording because it was load-bearing.
    The SDK builds the child environment as
    ``get_default_environment() | (server.env or {})``
    (``mcp/client/stdio.py``), so an overrides-only caller gets a SAFE ALLOWLIST
    plus its overrides: measured, 7 variables, not 2. The merge added nothing the
    child needed and handed a user-configured third-party server every variable
    in this process, 60 of them here, including the caller's own tokens.

    ``get_default_environment`` is the deliberate boundary: a fixed allowlist
    (HOME, LOGNAME, PATH, SHELL, TERM, USER on POSIX) that also drops any value
    beginning with ``()``, because those are exported shell functions. Merging
    ambient bypassed all of it.

    ``None`` stays ``None``, so a caller with no overrides takes the SDK default
    unchanged.
    """
    if not env:
        return None
    return {str(k): str(v) for k, v in env.items()}


async def _acall(command, args, tool, arguments, timeout, env, url=None):
    if url:
        async with streamable_http_client(url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                return await _call_in_session(session, tool, arguments, timeout)

    params = StdioServerParameters(
        command=command, args=list(args or ()), env=_spawn_env(env))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            return await _call_in_session(session, tool, arguments, timeout)


async def _list_in_session(session, timeout) -> ToolList:
    """Shared session body: handshake, ask ``tools/list``, and read the answer.

    Both schema spellings are tried because ``pyproject.toml`` floors the SDK at
    ``mcp>=2.0`` with no upper pin: 2.1.0 exposes ``Tool.input_schema`` and 1.x
    used ``inputSchema``. Sorted by name so two runs of the same probe read the
    same and a numbered picker built on top of it is stable.
    """
    await asyncio.wait_for(session.initialize(), timeout)
    res = await asyncio.wait_for(session.list_tools(), timeout)
    raw = getattr(res, "tools", None)
    if raw is None:
        raise McpProbeShapeError(
            "the server answered tools/list with no 'tools' field")
    tools = [
        ToolInfo(
            name=getattr(t, "name", "") or "",
            description=(getattr(t, "description", None) or "").strip(),
            input_schema=dict(
                getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}),
        )
        for t in raw
    ]
    return ToolList(
        tools=sorted(tools, key=lambda t: t.name),
        truncated=bool(getattr(res, "next_cursor", None)),
    )


async def _alist(command, args, timeout, env, url=None) -> ToolList:
    if url:
        async with streamable_http_client(url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                return await _list_in_session(session, timeout)

    params = StdioServerParameters(
        command=command, args=list(args or ()), env=_spawn_env(env))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            return await _list_in_session(session, timeout)


def _identity_digest(command: str | None, args: Sequence[str], env: dict | None,
                     url: str | None = None) -> str:
    """Eight hex characters standing for the exact server a source reaches.

    JSON rather than a joined string, because JSON escapes and delimits: without
    it ``["a", "bc"]`` and ``["ab", "c"]`` digest alike and the collision this
    exists to remove comes straight back. ``None`` and ``{}`` normalize to one
    empty env, so a source that sets no environment matches itself run to run.

    ``env`` holds a caller's overrides and nothing else, so every entry in it
    distinguishes one source from another and all of them count. Keys and values
    are coerced to ``str`` because a TOML config can hand this an int, and ``1``
    and ``"1"`` spawn the same child and must key the same.
    """
    payload = json.dumps(
        {
            "command": command or "",
            "args": [str(a) for a in (args or ())],
            "env": sorted((str(k), str(v)) for k, v in (env or {}).items()),
            "url": url or "",
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=4).hexdigest()


def server_key(command: str | None, args: Sequence[str], url: str | None,
               *, env: dict | None = None) -> str:
    """A key identifying the MCP server a call is aimed at.

    Two things read it and both ask the same question, so both take the same
    answer: the circuit breaker (:mod:`.resilience`) asking "is this the server
    I already wrote off", and the capability record (:mod:`.capabilities`)
    asking "is this the server I already have a tool list for".

    Every connector reaches its server through :func:`call_tool`, so keying the
    breaker here gives Atlassian, Figma, Slack and the generic MCP query path one
    shared, per-server health record for free, rather than four copies of the
    same guard, which is the drift this codebase already refuses elsewhere (see
    :mod:`.http_base`).

    **What makes two sources the same server**

    * stdio: they would spawn the same program, from the same path, with the
      same argv and the same *overrides* on the environment.
    * hosted: they would dial the same URL, path and query included.

    Nothing narrower is safe, and both halves of that were measured here. The
    key used to stop at the program basename whenever no argument was
    URL-shaped, so ``uvx server-a`` and ``uvx server-b``, or any two servers run
    by ``python``, shared one record. The hosted branch then kept the same
    defect one transport over: it stopped at scheme/host/port, so two MCP
    servers on one host at different paths shared one record, and a URL with no
    parseable host collapsed to the bare string ``"mcp"`` whatever it pointed
    at. In both shapes the breaker wrote off both servers after three failures
    of one, and :mod:`.capabilities` read one server's tool list back as the
    other server's inventory, by name, in a line a user acts on.

    Two properties have to hold together, and they pull against each other:

    * **stable** -- the same source keyed twice in one run gives one key, so
      failures accumulate on one record and the breaker opens. Unrelated
      environment variables set between the two keyings must not move it.
    * **complete** -- two sources that reach different servers give different
      keys, including the ``mcp-proxy`` shape whose target host lives entirely
      in the environment and the hosted shape whose target lives in the path.

    The caller is what holds both at once on the stdio side. Every caller passes
    only what it overrides, and :func:`_spawn_env` merges the ambient
    environment underneath at the spawn. So the digest covers what separates one
    source from another and nothing else, with no ambient snapshot to subtract
    back out. Two earlier attempts moved that work in here and each broke one
    property: digesting the whole snapshot the connectors used to pass made the
    key move whenever any code set a variable, and dropping every entry whose
    value matched ambient put two sources with genuinely different child
    environments on one key.

    Nothing on this path reads ``os.environ``, so a variable set between two
    keyings of one source cannot move its key. (The connectors call
    ``os.path.expanduser`` on a configured auth directory, which reads ``HOME``;
    a run that moved ``HOME`` would key that source anew, and it would also be
    reaching a different credential store.)

    One config-driven path changes behaviour with this. ``connectors/mcp_query``
    and ``source_cmd`` pass a config's own ``env`` table straight through. That
    table used to *replace* the child environment and now merges on top of the
    ambient one, so such a child gains PATH and HOME it did not have.

    The configured source *name* is deliberately not part of it. Two sources
    pointed at one server are still one server, and its health and its tool list
    are each one fact about that server.

    The readable half is scrubbed and the rest is a digest. A stdio argv or env
    can carry a token and a home directory, and a URL path or query can carry a
    token too, so only the program basename (or the scheme, host and port) is
    spelled out. Everything that separates two sources beyond that goes into
    :func:`_identity_digest` and is appended after ``#``, which separates them
    without printing what separates them.

    Digesting the whole URL rather than its path alone costs one extra record
    when two sources reach one endpoint with different credentials in the query.
    That is the same trade the stdio branch already makes for two credentials in
    the env, and it errs the safe way: an extra health record never writes off a
    server that is working, while a shared one does.
    """
    if url:
        return f"{endpoint_key('mcp', url)}#{_identity_digest(None, (), None, url=url)}"
    program = (command or "?").rsplit("/", 1)[-1]
    readable = f"mcp:{program}"
    for arg in args or ():
        text = str(arg)
        if text.startswith(("http://", "https://")):
            readable = endpoint_key(f"mcp:{program}", text)
            break
    return f"{readable}#{_identity_digest(command, args, env)}"


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
    breaker = breaker_for(server_key(command, args, url, env=env))
    return breaker.call(
        lambda: asyncio.run(_acall(command, args, tool, arguments or {}, timeout, env, url)))


def list_tools(
    command: str | None = None, args: Sequence[str] = (), timeout: float = 90,
    env: dict | None = None, url: str | None = None,
) -> ToolList:
    """Ask an MCP server which tools it offers, with descriptions and schemas.

    Takes the same transport arguments as :func:`call_tool`, in the same order:
    stdio (spawning ``command``/``args``) unless ``url`` is given, in which case
    it connects to a hosted MCP server over streamable-HTTP. Before this it was
    stdio-only, so a hosted server could be configured and never probed.

    Guarded by the same per-server circuit breaker as :func:`call_tool`, and by
    the same key, so a probe aimed at a dead server counts toward the one health
    record the enrich path reads rather than a second one nobody consults. It
    was a bare ``asyncio.run`` before, which is how the guard came to be missing
    on the one call this module makes at the *worst* moment: straight at a
    provider that has just failed.
    """
    breaker = breaker_for(server_key(command, args, url, env=env))
    return breaker.call(lambda: asyncio.run(_alist(command, args, timeout, env, url)))
