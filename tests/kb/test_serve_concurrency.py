"""Tool concurrency for `contextlake kb serve`.

The MCP SDK runs every synchronous tool through
``anyio.to_thread.run_sync`` with no limiter, so it takes anyio's default of 40
worker threads. Our tools are graph traversals over SQLite -- thousands of small
round trips each -- and forty of those interleaving contend rather than work.
The server saturated at about four concurrent calls.

The limiter is run-scoped, so it can only be set inside the event loop, which is
why stdio grew an ``anyio.run`` wrapper. The HTTP transports cannot use it
(uvicorn owns its loop), so they take the bound through an ASGI lifespan hook
instead. That asymmetry is the thing most likely to rot, so both paths are
asserted here.

The concurrency assertions are structural (what is the limiter set to?) rather
than timed: a wall-clock threshold on a shared CI runner is a flake generator,
and the limiter's value is the thing the fix actually controls.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from contextlake.kb.server import (
    DEFAULT_TOOL_CONCURRENCY,
    TOOL_CONCURRENCY_ENV,
    _run_stdio,
    build_http_app,
    resolve_tool_concurrency,
)
from contextlake.kb.state import check_schema
from contextlake.kb.store.sqlite_store import SqliteStore

# anyio's own default. The pre-fix server ran every tool body at this width.
ANYIO_DEFAULT_TOKENS = 40


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "index.sqlite")
    check_schema(store)
    return store


# --- resolution ------------------------------------------------------------

def test_tool_concurrency_precedence_is_flag_then_env_then_default(monkeypatch):
    monkeypatch.delenv(TOOL_CONCURRENCY_ENV, raising=False)
    assert resolve_tool_concurrency() == DEFAULT_TOOL_CONCURRENCY
    assert resolve_tool_concurrency(7) == 7

    monkeypatch.setenv(TOOL_CONCURRENCY_ENV, "5")
    assert resolve_tool_concurrency() == 5
    assert resolve_tool_concurrency(7) == 7  # explicit still wins


@pytest.mark.parametrize("bad", ["", "nonsense", "0", "-3", "2.5"])
def test_a_junk_env_value_falls_back_instead_of_refusing_to_start(monkeypatch, bad):
    """This is a perf knob on a server an editor launches. Dying over a typo in
    a shell profile is worse than serving at the default."""
    monkeypatch.setenv(TOOL_CONCURRENCY_ENV, bad)
    assert resolve_tool_concurrency() == DEFAULT_TOOL_CONCURRENCY


def test_the_default_is_below_the_measured_knee():
    """Contention climbs sharply past a handful of concurrent tool bodies; the
    default has to sit under that, not merely below anyio's 40."""
    assert 1 <= DEFAULT_TOOL_CONCURRENCY <= 4
    assert DEFAULT_TOOL_CONCURRENCY < ANYIO_DEFAULT_TOKENS


# --- stdio: the anyio.run wrapper -------------------------------------------

class _FakeServer:
    """Stands in for MCPServer: records the limiter it was run under."""

    def __init__(self) -> None:
        self.tokens = None
        self.ran = False

    async def run_stdio_async(self) -> None:
        self.ran = True
        self.tokens = anyio.to_thread.current_default_thread_limiter().total_tokens


def test_stdio_bounds_the_tool_thread_pool():
    server = _FakeServer()
    anyio.run(_run_stdio, server, 3)
    assert server.ran
    # Without the fix this is anyio's default of 40.
    assert server.tokens == 3


def test_stdio_limiter_is_read_inside_the_loop_not_at_import():
    """The limiter lives in a run-scoped variable, so a second run must get its
    own value rather than inheriting the first one."""
    a, b = _FakeServer(), _FakeServer()
    anyio.run(_run_stdio, a, 2)
    anyio.run(_run_stdio, b, 6)
    assert (a.tokens, b.tokens) == (2, 6)


# --- HTTP: the ASGI lifespan hook -------------------------------------------

async def _drive_lifespan(app):
    """Run an ASGI app's lifespan startup and report the limiter it left set."""
    seen = {}
    pending = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return pending.pop(0) if pending else {"type": "lifespan.shutdown"}

    async def send(message):
        if message.get("type") == "lifespan.startup.complete":
            seen["tokens"] = anyio.to_thread.current_default_thread_limiter().total_tokens

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return seen


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_http_transports_bound_the_pool_on_lifespan_startup(tmp_path, transport):
    """uvicorn owns the loop for these, so the bound cannot come from an
    anyio.run of ours -- it has to ride the lifespan. It also has to survive
    BearerAuthMiddleware, which only inspects `http` scopes."""
    store = _store(tmp_path)
    try:
        app = build_http_app(store, transport=transport, host="127.0.0.1",
                             token="t", tool_concurrency=3)
        seen = anyio.run(_drive_lifespan, app)
    finally:
        store.close()
    assert seen.get("tokens") == 3


def test_http_lifespan_still_starts_the_sdks_own_session_manager(tmp_path):
    """The bound is added by wrapping, not by passing `lifespan=` -- that would
    replace the SDK's own lifespan and leave the session manager unstarted."""
    from starlette.testclient import TestClient

    store = _store(tmp_path)
    try:
        app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                             token="t", tool_concurrency=2)
        with TestClient(app) as client:
            # 401 proves the app is up and the auth gate ran; a dead session
            # manager surfaces as a 500 or a hang instead.
            r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            headers={"Content-Type": "application/json",
                                     "Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
    finally:
        store.close()


def test_http_app_falls_back_to_the_default_bound(tmp_path, monkeypatch):
    monkeypatch.delenv(TOOL_CONCURRENCY_ENV, raising=False)
    store = _store(tmp_path)
    try:
        app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                             token="t")
        seen = anyio.run(_drive_lifespan, app)
    finally:
        store.close()
    assert seen.get("tokens") == DEFAULT_TOOL_CONCURRENCY
