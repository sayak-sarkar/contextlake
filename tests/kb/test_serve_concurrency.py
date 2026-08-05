"""Tool concurrency and signal handling for `contextlake kb serve`.

Two defects live here, and they share one seam.

*Concurrency.* The MCP SDK runs every synchronous tool through
``anyio.to_thread.run_sync`` with no limiter, so it takes anyio's default of 40
worker threads. Our tools are graph traversals over SQLite -- thousands of small
round trips each -- and forty of those interleaving contend rather than work.
The server saturated at about four concurrent calls.

*SIGTERM.* Supervisors send SIGTERM first. Python's default action for it kills
the process on the spot, so ``cmd_serve``'s ``finally`` never ran and stores
were never closed.

Both fixes need code running inside the event loop -- the thread limiter is
run-scoped and asyncio's signal handling is loop-scoped -- which is why stdio
grew an ``anyio.run`` wrapper. The HTTP transports cannot use it (uvicorn owns
its loop), so they take the bound through an ASGI lifespan hook instead. That
asymmetry is the thing most likely to rot, so both paths are asserted here.

The concurrency assertions are structural (what is the limiter set to?) rather
than timed: a wall-clock threshold on a shared CI runner is a flake generator,
and the limiter's value is the thing the fix actually controls.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from contextlake.kb.server import (
    _TRANSPORT_IO_RESERVE,
    DEFAULT_TOOL_CONCURRENCY,
    TOOL_CONCURRENCY_ENV,
    _run_stdio,
    build_http_app,
    build_server,
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
    assert server.tokens == 3 + _TRANSPORT_IO_RESERVE


def test_stdio_limiter_is_read_inside_the_loop_not_at_import():
    """The limiter lives in a run-scoped variable, so a second run must get its
    own value rather than inheriting the first one."""
    a, b = _FakeServer(), _FakeServer()
    anyio.run(_run_stdio, a, 2)
    anyio.run(_run_stdio, b, 6)
    assert (a.tokens, b.tokens) == (2 + _TRANSPORT_IO_RESERVE,
                                    6 + _TRANSPORT_IO_RESERVE)


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
    assert seen.get("tokens") == 3 + _TRANSPORT_IO_RESERVE


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
    assert seen.get("tokens") == DEFAULT_TOOL_CONCURRENCY + _TRANSPORT_IO_RESERVE


# --- SIGTERM ----------------------------------------------------------------

_DRIVER = """
import sys
sys.path.insert(0, {src!r})
from contextlake.cli import main
sys.exit(main(["kb", "serve", "--config", {cfg!r}]))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_stdio_serve_stops_cleanly_on_a_stop_signal(tmp_path, sig):
    """Both stop signals were broken on an idle stdio server, for one reason.

    SIGTERM -- what a supervisor sends first -- took the default action, so
    cmd_serve's `finally` never ran and stores were never closed. SIGINT was no
    better: Python only runs a signal handler at a bytecode boundary in the main
    thread, and an idle server's main thread is parked in the selector, so Ctrl-C
    sat unhandled until traffic happened to arrive. Measured on the unfixed
    server, both hung indefinitely.

    Driven as a real subprocess: the failure mode of the unfixed code is a
    process that will not die, which is not something to invite into the runner.
    """
    src = str(Path(__file__).resolve().parents[2] / "src")
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    _store(store_dir).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER.format(src=src, cfg=str(cfg)))

    env = dict(os.environ, PYTHONPATH=src)
    proc = subprocess.Popen([sys.executable, str(driver)], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        # Readiness is gated on a real MCP handshake, not on the startup log
        # line: that line is printed before run_server is even called, and the
        # signal handler is only installed once the loop is up. Signalling on
        # the log raced the handler into place and read as "the fix does not
        # work" when it simply had not run yet.
        _handshake(proc, deadline=time.time() + 60)

        proc.send_signal(sig)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(f"{sig.name} did not stop the stdio server")
        err = proc.stderr.read().decode(errors="ignore")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdin.close()

    # Dying *by* the signal is the default action -- exactly the path that skips
    # cmd_serve's `finally` and leaves the store open.
    assert rc != -sig and rc != 128 + sig, f"{sig.name} took the default action"
    assert rc == 0
    # The `finally` really ran, rather than the process merely exiting 0.
    assert "Stopping MCP server" in err
    # The teardown stays quiet: cmd_serve skips the interpreter's thread join
    # precisely so shutdown does not spray a join traceback.
    assert "Exception ignored" not in err


def _handshake(proc, *, deadline: float) -> None:
    """Drive an MCP `initialize` over stdio and wait for the reply."""
    import json
    import select

    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "sigterm-test", "version": "1"}},
    }
    proc.stdin.write((json.dumps(request) + "\n").encode())
    proc.stdin.flush()

    buf = b""
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited during handshake (rc={proc.returncode})")
        if select.select([proc.stdout], [], [], 0.25)[0]:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            if b'"result"' in buf:
                return
    pytest.fail("stdio server never answered initialize")


# --- the bound moved off the worker pool and onto the tool bodies -----------

_STDIO_PROBE = """
import json, subprocess, sys
proc = subprocess.Popen(
    [sys.executable, "-c", {driver!r}],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
req = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {{"protocolVersion": "2024-11-05", "capabilities": {{}},
                              "clientInfo": {{"name": "probe", "version": "0"}}}}}})
proc.stdin.write(req + chr(10))
proc.stdin.flush()
line = proc.stdout.readline()
print("GOT" if line.strip() else "EMPTY")
proc.kill()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_stdio_answers_initialize_at_a_tool_concurrency_of_one(tmp_path):
    """A limit of one used to hang the stdio transport outright.

    The bound was applied by shrinking anyio's default thread limiter, which is
    not private to tool bodies: the SDK's stdio transport wraps stdin and stdout
    with anyio.wrap_file and passes no limiter, so readline, write and flush all
    borrowed the same tokens. At one token the stdin_reader task sat inside a
    blocking readline holding it, and stdout_writer could never acquire one to
    flush a reply. The server started, printed its banner, and answered nothing:
    no error, no warning, no timeout, on the transport every editor uses, at the
    value the module's own benchmark recommends as fastest.

    Driven as a real subprocess speaking raw JSON-RPC with no client library, so
    nothing can mask a hang, and out of process because the failure mode is
    something that never replies.
    """
    src = str(Path(__file__).resolve().parents[2] / "src")
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    _store(store_dir).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')

    driver = (f"import sys; sys.path.insert(0, {src!r})\n"
              "from contextlake.cli import main\n"
              f"sys.exit(main(['kb', 'serve', '--config', {str(cfg)!r}, "
              "'--tool-concurrency', '1']))\n")
    probe = tmp_path / "probe.py"
    probe.write_text(_STDIO_PROBE.format(driver=driver))

    out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                         text=True, timeout=90)
    assert "GOT" in out.stdout, f"stdio never answered initialize: {out.stdout!r}"


def test_the_tool_bound_still_bounds_concurrent_tool_bodies(tmp_path):
    """Moving the bound off the thread limiter must not quietly remove it.

    Drives the REGISTERED callable, which is the wrapper a wire call goes
    through, rather than the bare function every in-process caller sees. The
    count is taken *inside* the tool body, since that is what the bound is
    about -- counting arrivals at the wrapper would measure the callers.
    """
    store = _store(tmp_path)
    real = store.stats
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_stats():
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            time.sleep(0.05)   # wide enough for contention to be observable
            return real()
        finally:
            with lock:
                state["now"] -= 1

    try:
        server = build_server(store, tool_concurrency=2)
        store.stats = slow_stats
        registered = server._tool_manager.get_tool("graph_stats").fn
        threads = [threading.Thread(target=registered) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert state["peak"] <= 2, state
        assert state["peak"] >= 2, "the bound must not have serialised to one"
    finally:
        store.stats = real
        store.close()


def test_ask_can_call_other_tools_while_holding_the_bound(tmp_path):
    """`ask` calls find_definition, find_callers, blast_radius and others
    directly. If registration replaced those names with the guarded wrapper, a
    non-reentrant bound would deadlock `ask` against itself at a limit of one --
    trading a transport hang for a tool hang. MCPServer.tool returns the original
    function, and the registration here preserves that.
    """
    store = _store(tmp_path)
    try:
        server = build_server(store, tool_concurrency=1)
        registered = server._tool_manager.get_tool("ask").fn
        done = []

        def call():
            done.append(registered(question="where is CatalogService defined"))

        worker = threading.Thread(target=call)
        worker.start()
        worker.join(timeout=20)
        assert not worker.is_alive(), "ask deadlocked against its own tool bound"
        assert done and done[0].route
    finally:
        store.close()
