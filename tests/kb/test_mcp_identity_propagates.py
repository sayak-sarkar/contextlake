"""The identity gate: does a key set at the socket reach a tool body, and is it
the RIGHT key for that request?

Every per-key control planned for this server reads one value: the key id of the
request being served. That value crosses a boundary this project does not own.
It is set in ASGI middleware and read inside a synchronous tool body, and
between those two points the MCP SDK hands the request to an anyio task group
and then to ``anyio.to_thread.run_sync``.

If that hand-off stops carrying the ContextVar, nothing raises. The middleware
still sets it, the tool body still runs, the request still returns 200. The
access rules would then enforce on nothing and the usage rows would all be
written against the same missing id, while every other test in this suite
passed. So the assertion here is TWO DIFFERENT IDS, not one value arriving: a
hard-coded constant satisfies the weaker form and proves nothing.

The mechanism is undocumented behaviour of two dependencies. A synchronous tool
runs through ``anyio.to_thread.run_sync`` and its context is restored from a
per-message snapshot, reached by two duck-typed
``getattr(stream, "last_context", None)`` reads whose fallback is the server
task's own context, which holds no principal. The fallback is silent. This file
is the detector.

Measured 2026-09-05 on this repo's venv (mcp 2.1.0, anyio 4.14.2, starlette
1.3.1, uvicorn 0.51.0, CPython 3.14):

* streamable-http propagates. Two requests, two keys, two ids.
* sse propagates too, and it is the ``POST /messages/`` request's context that
  reaches the tool body, not the long-lived ``GET /sse`` connection's. That was
  the open question: it means an sse call can be attributed, and re-checked,
  per message rather than once at connect. It is asserted directly in
  ``test_two_sse_calls_on_one_stream_are_attributed_per_message``.

The sse half runs against a REAL BOUND SOCKET. ``starlette.testclient.TestClient``
runs a single portal, so a long-lived ``GET /sse`` from one thread plus a
``POST /messages/`` from another deadlocks: measured past two minutes on
2026-09-03. uvicorn on an ephemeral port is what makes the case runnable at all.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from mcp import Client

from contextlake.kb import server as server_mod
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import (
    SHARED_TOKEN_KEY_ID,
    Principal,
    build_http_app,
    build_server,
    current_principal,
)
from contextlake.kb.store.sqlite_store import SqliteStore

KEY_A = "key-alpha"
KEY_B = "key-bravo"
SHARED = "shared-secret-value"

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
# The Host the SDK's rebinding check expects for the default loopback bind.
LOOPBACK_BASE = "http://127.0.0.1:8765"

_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "identity-gate", "version": "1"}},
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call_stats(request_id: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "graph_stats", "arguments": {}}}


# --------------------------------------------------------------------------
# The probe. It reads the principal from INSIDE a tool body, in whatever thread
# the SDK chose to run that body in, which is the only place the answer means
# anything. `graph_stats` calls `store.stats()` and nothing else, so overriding
# that one method puts the read inside `fn`, past the wrapper, with no
# production seam added for the test's benefit.
# --------------------------------------------------------------------------
class ProbeStore(SqliteStore):
    """Records the key id each tool body saw, and can be made to overlap."""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.seen: list[str | None] = []
        self.threads: list[int] = []
        # When set, every body waits here until a second body arrives, so an
        # overlap is forced rather than hoped for.
        self.rendezvous: threading.Barrier | None = None

    def stats(self):
        principal = current_principal()
        self.seen.append(None if principal is None else principal.key_id)
        self.threads.append(threading.get_ident())
        if self.rendezvous is not None:
            # Raises BrokenBarrierError on timeout, which surfaces as a failed
            # tool call. A serialised run therefore FAILS here rather than
            # passing with two ids that never coexisted.
            self.rendezvous.wait(timeout=30)
        return super().stats()


class FakeKeyring:
    """The whole read shape the real multi-key keyring has to implement.

    One method. Fixed here so the key-store area does not invent a second one,
    and so this test does not depend on a module that has not been written.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._by_value = {value.encode(): Principal(key_id)
                          for key_id, value in mapping.items()}

    def resolve(self, presented: bytes) -> Principal | None:
        return self._by_value.get(presented)


class CountingVar:
    """A stand-in for the module's ContextVar that counts set and reset.

    Wrapping the module global rather than putting counters on the middleware:
    the counters are test instrumentation, and instrumentation that ships in
    production is state nothing reads. Both the gate and ``current_principal``
    look the name up on the module at call time, so both go through this.
    """

    def __init__(self, var) -> None:
        self._var = var
        self.sets = 0
        self.resets = 0

    def set(self, value):
        self.sets += 1
        return self._var.set(value)

    def reset(self, token) -> None:
        self.resets += 1
        self._var.reset(token)

    def get(self):
        return self._var.get()


def seed(store) -> None:
    store.upsert_nodes("team/api", [
        Node(id="a", repo="team/api", kind="function", name="ForecastService",
             file="svc.py"),
        Node(id="b", repo="team/api", kind="function", name="ingest"),
    ])
    store.upsert_edges("team/api", [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.EXTRACTED,
        provenance=Provenance(source_file="svc.py", source_line=5,
                              verified_at=date(2026, 6, 21)),
    )])


@pytest.fixture
def probe_store(tmp_path):
    store = ProbeStore(tmp_path / "kb.sqlite")
    seed(store)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def two_key_app(probe_store):
    """The real ``build_http_app`` output, streamable-http, two live keys."""
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    return build_http_app(probe_store, transport="streamable-http",
                          host="127.0.0.1", token=SHARED, keyring=keyring)


@pytest.fixture(autouse=True)
def _quiet_identity_fault():
    """The fault log is once per PROCESS, so it has to be cleared per test.

    Without this the test that asserts one warning reads zero whenever an
    earlier test in the same worker already tripped the flag.
    """
    server_mod._reset_identity_fault_log()
    yield
    server_mod._reset_identity_fault_log()


# --------------------------------------------------------------------------
# Socket harness. The sse case cannot be driven any other way.
# --------------------------------------------------------------------------
@contextmanager
def bound_server(app):
    """Run ``app`` on uvicorn on an ephemeral port. Yields ``host:port``."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]),
                              daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("uvicorn did not start within 30s")
        time.sleep(0.02)
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def read_sse_event(response, deadline: float) -> tuple[str | None, str | None]:
    """One SSE event block off a live response -> (event name, data)."""
    buf = b""
    while time.monotonic() < deadline:
        chunk = response.read(1)
        if not chunk:
            return None, None
        buf += chunk
        if buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n"):
            name = data = None
            for line in buf.decode(errors="replace").splitlines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
            return name, data
    raise TimeoutError("no sse event arrived before the deadline")


class SseSession:
    """One ``GET /sse`` stream plus the ``POST /messages/`` endpoint it names."""

    def __init__(self, hostport: str, key: str) -> None:
        self.hostport = hostport
        host, port = hostport.split(":")
        self.stream = http.client.HTTPConnection(host, int(port), timeout=30)
        self.stream.request("GET", "/sse", headers={
            "Host": hostport, "Accept": "text/event-stream",
            "Authorization": f"Bearer {key}"})
        self.response = self.stream.getresponse()
        assert self.response.status == 200, self.response.status
        name, endpoint = read_sse_event(self.response, time.monotonic() + 30)
        assert name == "endpoint", (name, endpoint)
        self.endpoint = endpoint
        self.poster = http.client.HTTPConnection(host, int(port), timeout=30)

    def post(self, payload: dict, key: str) -> int:
        self.poster.request("POST", self.endpoint, body=json.dumps(payload),
                            headers={"Host": self.hostport,
                                     "Content-Type": "application/json",
                                     "Authorization": f"Bearer {key}"})
        response = self.poster.getresponse()
        response.read()
        return response.status

    def reply(self) -> str:
        return read_sse_event(self.response, time.monotonic() + 30)[1] or ""

    def handshake(self, key: str) -> None:
        assert self.post(_INITIALIZE, key) == 202
        self.reply()
        assert self.post(_INITIALIZED, key) == 202

    def close(self) -> None:
        self.stream.close()
        self.poster.close()


# ==========================================================================
# AC1 -- two ids over streamable-http, through the real build_http_app output.
#
# WHY THIS HALF RUNS ON A TEST CLIENT AND THE SSE HALF ON A BOUND SOCKET. The
# sse case needs a long-lived `GET /sse` held open while a `POST /messages/`
# goes out on a second connection. `starlette.testclient.TestClient` runs a
# single portal, so those two deadlock: measured past two minutes on
# 2026-09-03. streamable-http here is built `stateless_http=True`, so a call is
# one request that opens and closes, with nothing held open beside it, and the
# test client drives it whole. The socket path is not left unmeasured for this
# transport either: `test_overlapping_tool_bodies_read_their_own_keys` runs
# streamable-http against a real bound socket, with two requests in flight.
# ==========================================================================
def test_two_keys_reach_two_tool_bodies_as_two_different_ids(two_key_app,
                                                             probe_store):
    from starlette.testclient import TestClient

    with TestClient(two_key_app, base_url=LOOPBACK_BASE) as client:
        for key in ("value-for-alpha", "value-for-bravo"):
            auth = {**_JSON_HEADERS, "Authorization": f"Bearer {key}"}
            assert client.post("/mcp", json=_INITIALIZE,
                               headers=auth).status_code == 200
            answer = client.post("/mcp", json=_call_stats(), headers=auth)
            assert answer.status_code == 200
            assert "error" not in answer.json(), answer.text

    # Two ids, not one value arriving. A hard-coded constant passes
    # `is not None` on both rows and fails this line.
    assert probe_store.seen == [KEY_A, KEY_B], probe_store.seen
    assert len(set(probe_store.seen)) == 2
    assert None not in probe_store.seen


def test_the_tool_body_really_crossed_the_thread_hand_off(probe_store):
    """The read is worth nothing if the body ran on the ASGI thread.

    The whole fragility is the hand-off out of the event loop into a worker
    thread; a ContextVar needs no help to survive staying put. So record which
    thread ran the ASGI app and which ran the tool body, and require them to
    DIFFER. Asserted on thread identity, not on a thread NAME: the name is an
    anyio internal, and this file is meant to keep running against newer SDKs.
    """
    from starlette.testclient import TestClient

    asgi_threads: set[int] = set()

    class RecordAsgiThread:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            if scope.get("type") == "http":
                asgi_threads.add(threading.get_ident())
            await self.app(scope, receive, send)

    keyring = FakeKeyring({KEY_A: "value-for-alpha"})
    app = RecordAsgiThread(build_http_app(
        probe_store, transport="streamable-http", host="127.0.0.1",
        token=SHARED, keyring=keyring))

    auth = {**_JSON_HEADERS, "Authorization": "Bearer value-for-alpha"}
    with TestClient(app, base_url=LOOPBACK_BASE) as client:
        client.post("/mcp", json=_INITIALIZE, headers=auth)
        assert client.post("/mcp", json=_call_stats(),
                           headers=auth).status_code == 200

    assert probe_store.threads, "the tool body never ran"
    assert asgi_threads, "the ASGI app never ran"
    assert not (set(probe_store.threads) & asgi_threads), \
        (probe_store.threads, asgi_threads)


# ==========================================================================
# AC2 -- two ids over sse, against a real bound socket.
#
# Two outcomes were allowed to pass here: two distinct ids, or every sse call
# refusing with IdentityUnset and answering nothing. This ran as the FIRST
# outcome on 2026-09-05, so the assertions below are written for it. If a
# future SDK breaks sse propagation, this test fails loudly rather than
# quietly accepting the weaker outcome.
# ==========================================================================
@pytest.mark.timeout(120)
def test_two_keys_reach_two_tool_bodies_over_sse(probe_store):
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    app = build_http_app(probe_store, transport="sse", host="127.0.0.1",
                         token=SHARED, keyring=keyring)
    with bound_server(app) as hostport:
        for key in ("value-for-alpha", "value-for-bravo"):
            session = SseSession(hostport, key)
            try:
                session.handshake(key)
                assert session.post(_call_stats(), key) == 202
                reply = session.reply()
                assert '"id":2' in reply.replace(" ", ""), reply
                assert '"error"' not in reply, reply
            finally:
                session.close()

    assert probe_store.seen == [KEY_A, KEY_B], probe_store.seen
    assert len(set(probe_store.seen)) == 2
    assert None not in probe_store.seen


@pytest.mark.timeout(120)
def test_two_sse_calls_on_one_stream_are_attributed_per_message(probe_store):
    """Whose context does an sse tool body see, the stream's or the POST's?

    Measured: the POST's. One long-lived ``GET /sse`` opened with key A, then
    two tool calls POSTed to that session carrying key A and key B, produce the
    ids of the POSTs and not two copies of the stream's.

    This is the discriminating case, and it decides a real design question:
    an sse call can be attributed, charged and re-checked per message. Had the
    stream's context won, both rows would read ``key-alpha`` and sse identity
    would be fixed at connect time, unable to see a key revoked mid-stream.
    """
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    app = build_http_app(probe_store, transport="sse", host="127.0.0.1",
                         token=SHARED, keyring=keyring)
    with bound_server(app) as hostport:
        session = SseSession(hostport, "value-for-alpha")
        try:
            session.handshake("value-for-alpha")
            assert session.post(_call_stats(2), "value-for-alpha") == 202
            session.reply()
            assert session.post(_call_stats(3), "value-for-bravo") == 202
            session.reply()
        finally:
            session.close()

    assert probe_store.seen == [KEY_A, KEY_B], probe_store.seen


# ==========================================================================
# AC2b -- SUBSTITUTION, not absence. Two sse connections open at the same
# time, two keys, two tool bodies provably inside together.
#
# The frame refuses when the principal is MISSING. It has no second source of
# truth at that point, so it cannot tell a principal that belongs to this
# caller from one that belongs to another. That makes "does a body ever read
# the other connection's principal?" a question only measurement answers.
# These two tests are that measurement.
# ==========================================================================
@pytest.mark.timeout(180)
def test_two_concurrent_sse_connections_read_their_own_keys(probe_store):
    """Two sse streams open together, one tool body each, forced to overlap.

    ``test_two_keys_reach_two_tool_bodies_over_sse`` opens its two sessions one
    after the other -- created, used, closed, then the next -- so it cannot see
    a body reading the other connection's principal. Nothing is ever concurrent
    in it. This one holds both streams open and puts both bodies inside the
    barrier before either returns.

    The assertion that discriminates is the SET. Under substitution both bodies
    read one id, ``seen`` is ``[KEY_A, KEY_A]``, and that passes
    ``len(seen) == 2`` and passes ``None not in seen``. It fails the set.

    Measured 2026-09-05, mcp 2.1.0: the two ids differ. ``connect_sse`` calls
    ``create_context_streams`` per connection (mcp/server/sse.py:152) and
    ``last_context`` is an attribute on that per-connection receive stream
    (mcp/shared/_context_streams.py:64), so two connections share no holder for
    it. The result confirms that reading rather than discovering it.
    """
    probe_store.rendezvous = threading.Barrier(2)
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    app = build_http_app(probe_store, transport="sse", host="127.0.0.1",
                         token=SHARED, keyring=keyring, tool_concurrency=2)
    # Results come back to the main thread. An assertion raised inside a worker
    # thread is swallowed, which is how a serialised run passes a test that
    # asserts there.
    replies: dict[str, str] = {}
    failures: dict[str, BaseException] = {}

    def drive(key: str, session: SseSession) -> None:
        try:
            assert session.post(_call_stats(), key) == 202
            replies[key] = session.reply()
        except BaseException as exc:
            failures[key] = exc

    with bound_server(app) as hostport:
        sessions = {}
        try:
            for key in ("value-for-alpha", "value-for-bravo"):
                session = SseSession(hostport, key)
                session.handshake(key)
                sessions[key] = session
            threads = [threading.Thread(target=drive, args=(key, session))
                       for key, session in sessions.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
                assert not thread.is_alive(), "an sse call never finished"
        finally:
            for session in sessions.values():
                session.close()

    assert failures == {}, failures
    assert set(replies) == {"value-for-alpha", "value-for-bravo"}, list(replies)
    for key, reply in replies.items():
        # A broken barrier -- what a serialised run produces -- unwinds out of
        # the tool body and arrives as a JSON-RPC error, not as a bad status.
        assert '"error"' not in reply, (key, reply)
        assert '"nodes":2' in reply.replace(" ", ""), (key, reply)

    assert len(probe_store.seen) == 2, probe_store.seen
    assert set(probe_store.seen) == {KEY_A, KEY_B}, probe_store.seen


@pytest.mark.timeout(120)
def test_a_second_key_may_post_into_another_connections_sse_session(probe_store):
    """The gap the frame cannot see, measured rather than argued.

    Two sse streams are open, one per key. A tool call carrying key B is POSTed
    to key A's ``/messages/`` endpoint. Measured 2026-09-05:

    * the POST is accepted (202). Our gate authenticates each request on its own
      and binds nothing to the connection the session id names.
    * the tool body reads ``key-bravo``, the poster's id, not the stream
      owner's. So this is not one body reading another body's principal.
    * the ANSWER is written to key A's stream. Connection A receives the result
      of a call it did not make.

    The SDK has a guard for this (``mcp/server/sse.py:261``, a session is
    usable only by the credential that created it) and it is INERT here: it
    compares ``authorization_context(scope["user"])``, and
    ``BearerAuthMiddleware`` sets no ``scope["user"]``. This test pins today's
    behaviour so the fix is a visible change, and names what closes it: a
    session id carried alongside the principal and checked at the boundary.
    Tracked in planning/tickets/epic-4-mcp-network-auth/S4.1-auth-identity-gate.md.
    """
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    app = build_http_app(probe_store, transport="sse", host="127.0.0.1",
                         token=SHARED, keyring=keyring)
    with bound_server(app) as hostport:
        owner = SseSession(hostport, "value-for-alpha")
        intruder = SseSession(hostport, "value-for-bravo")
        try:
            owner.handshake("value-for-alpha")
            intruder.handshake("value-for-bravo")
            # Key B's credential, key A's session endpoint.
            assert owner.post(_call_stats(), "value-for-bravo") == 202
            # The answer arrives on the OWNER's stream, which is the disclosure.
            reply = owner.reply()
        finally:
            owner.close()
            intruder.close()

    assert '"id":2' in reply.replace(" ", ""), reply
    assert '"error"' not in reply, reply
    # The poster's id, not the stream owner's. Absence would be None here and
    # the frame would refuse; a present-but-wrong id is what it cannot see.
    assert probe_store.seen == [KEY_B], probe_store.seen


# ==========================================================================
# AC3 -- no cross-talk when two bodies are provably inside at the same time.
# ==========================================================================
@pytest.mark.timeout(120)
def test_overlapping_tool_bodies_read_their_own_keys(probe_store):
    """Driven against a bound socket, because two requests must be in flight at
    once and a single-portal test client cannot put them there. The overlap is
    FORCED with a barrier: a serialised run breaks the barrier on its timeout
    and fails, rather than passing with two ids that never coexisted.
    """
    probe_store.rendezvous = threading.Barrier(2)
    keyring = FakeKeyring({KEY_A: "value-for-alpha", KEY_B: "value-for-bravo"})
    app = build_http_app(probe_store, transport="streamable-http",
                         host="127.0.0.1", token=SHARED, keyring=keyring,
                         tool_concurrency=2)
    # Every result comes back to the main thread. An assertion raised inside a
    # worker thread is swallowed, so a test that asserted there would pass on a
    # serialised run: measured, before this was written that way round.
    answers: dict[str, tuple[int, bytes]] = {}
    failures: dict[str, BaseException] = {}

    def call(key: str) -> None:
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=60)
        try:
            for payload in (_INITIALIZE, _call_stats()):
                conn.request("POST", "/mcp", body=json.dumps(payload), headers={
                    "Host": hostport, **_JSON_HEADERS,
                    "Authorization": f"Bearer {key}"})
                response = conn.getresponse()
                body = response.read()
            answers[key] = (response.status, body)
        except BaseException as exc:
            failures[key] = exc
        finally:
            conn.close()

    with bound_server(app) as hostport:
        threads = [threading.Thread(target=call, args=(key,))
                   for key in ("value-for-alpha", "value-for-bravo")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
            assert not thread.is_alive(), "a request never finished"

    assert failures == {}, failures
    assert set(answers) == {"value-for-alpha", "value-for-bravo"}, list(answers)
    for key, (status, body) in answers.items():
        assert status == 200, (key, status)
        # A broken barrier -- which is what a SERIALISED run produces -- unwinds
        # out of the tool body and comes back as a tool ERROR inside a 200, not
        # as a bad status. These are the lines that fail when the two bodies
        # never met, so they are read off the parsed envelope rather than the
        # raw bytes: `isError` is present and false on every success.
        result = json.loads(body)["result"]
        assert result["isError"] is False, (key, result)
        assert result["structuredContent"]["nodes"] == 2, (key, result)

    assert len(probe_store.seen) == 2, probe_store.seen
    assert set(probe_store.seen) == {KEY_A, KEY_B}, probe_store.seen


# ==========================================================================
# AC4 -- the var is reset, asserted where a leak would show.
# ==========================================================================
def test_a_refused_request_between_two_keys_does_not_leak_the_first(two_key_app,
                                                                    probe_store):
    """Three requests through ONE app object: key A, one with no credential
    (refused), then key B. The third must read B's id, not A's.

    Reading the principal back in the test's own context would prove nothing:
    the ASGI app runs in the client's portal task, so that read is None whether
    the reset exists or not.

    Measured 2026-09-05, and worth writing down: this test does NOT detect a
    missing reset. Deleting the `finally` in the gate leaves it green, because
    the test client runs each request in a fresh task and a ContextVar set in
    one task is not visible in the next. What it does detect is a wrong id and
    a refusal that disturbs the request after it. The reset itself rests on
    `test_the_gate_sets_and_resets_once_per_admitted_request`, which does fail
    when the reset is deleted.
    """
    from starlette.testclient import TestClient

    with TestClient(two_key_app, base_url=LOOPBACK_BASE) as client:
        for key in ("value-for-alpha", "value-for-bravo"):
            auth = {**_JSON_HEADERS, "Authorization": f"Bearer {key}"}
            client.post("/mcp", json=_INITIALIZE, headers=auth)
            if key == "value-for-bravo":
                refused = client.post("/mcp", json=_call_stats(),
                                      headers=_JSON_HEADERS)
                assert refused.status_code == 401
            assert client.post("/mcp", json=_call_stats(),
                               headers=auth).status_code == 200

    assert probe_store.seen == [KEY_A, KEY_B], probe_store.seen


def test_the_gate_sets_and_resets_once_per_admitted_request(two_key_app,
                                                            monkeypatch):
    """set count == reset count == the number of requests that PASSED the gate.

    A refused request contributes 0 to both, and in this phase that is
    structural: the gate refuses before it calls through, so no set happens.
    """
    from starlette.testclient import TestClient

    counter = CountingVar(server_mod._PRINCIPAL)
    monkeypatch.setattr(server_mod, "_PRINCIPAL", counter)

    auth = {**_JSON_HEADERS, "Authorization": "Bearer value-for-alpha"}
    with TestClient(two_key_app, base_url=LOOPBACK_BASE) as client:
        assert client.post("/mcp", json=_INITIALIZE,
                           headers=auth).status_code == 200
        assert client.post("/mcp", json=_call_stats(),
                           headers=auth).status_code == 200
        assert client.post("/mcp", json=_call_stats(),
                           headers=_JSON_HEADERS).status_code == 401
        assert client.post("/mcp", json=_call_stats(), headers={
            **_JSON_HEADERS, "Authorization": "Bearer not-a-key"}).status_code == 401

    # Four requests, two admitted. The two refusals must move neither counter.
    assert counter.sets == 2, counter.sets
    assert counter.resets == 2, counter.resets


# ==========================================================================
# AC5 and AC9 -- the local path reads no ContextVar, and the switch is
# build-time. Two states, two outcomes, one fixture.
# ==========================================================================
def _ten_stdio_calls(server) -> list:
    async def run():
        results = []
        async with Client(server) as client:
            for _ in range(10):
                results.append(await client.call_tool("graph_stats", {}))
        return results

    return asyncio.run(run())


def test_stdio_never_reads_the_identity_contextvar(probe_store, monkeypatch):
    """``build_server`` with every new keyword-only parameter omitted is the
    stdio and in-process shape. Patch the one reader to raise: ten tool calls
    still complete, and the reader is called zero times."""
    calls = []

    def boom():
        calls.append(1)
        raise AssertionError("the local path read the identity ContextVar")

    monkeypatch.setattr(server_mod, "current_principal", boom)
    results = _ten_stdio_calls(build_server(probe_store))

    assert len(results) == 10
    assert all(not r.is_error for r in results), results[0]
    assert calls == [], f"current_principal was called {len(calls)} times"


def test_the_networked_build_of_the_same_server_reads_it_ten_times(probe_store,
                                                                   monkeypatch):
    """The other half of the pair, and the reason the first half means anything.

    A zero-call assertion alone is what a mock wired to nothing reports. The
    switch is proved by BEHAVIOUR differing between the two states, not by
    reading the source: a source-string assertion passes on any refactor that
    keeps the text and fails on any that does not, and measures neither.

    Named for what it checks. It counts reads; it does not check WHERE the read
    sits, so it stays green if the read moves out from behind the switch. The
    refusal that a moved read would break is
    ``test_a_lost_identity_refuses_and_says_why``, and that one does fail.
    """
    calls = []

    def boom():
        calls.append(1)
        raise AssertionError("read on a networked server, as designed")

    monkeypatch.setattr(server_mod, "current_principal", boom)
    results = _ten_stdio_calls(build_server(probe_store, networked=True))

    assert all(r.is_error for r in results), results[0]
    assert len(calls) == 10, len(calls)


# ==========================================================================
# AC7 -- fail closed on a lost identity, with the positive control beside it.
# ==========================================================================
def test_a_lost_identity_refuses_and_says_why(probe_store, monkeypatch):
    monkeypatch.setattr(server_mod, "current_principal", lambda: None)
    result = _ten_stdio_calls(build_server(probe_store, networked=True))[0]

    assert result.is_error
    text = result.content[0].text
    # The caller gets the reason, not a bare `Error executing tool graph_stats`.
    assert "could not identify the caller" in text, text
    assert "not a problem with your key" in text, text
    # Nothing was answered: the tool body never ran.
    assert probe_store.seen == [], probe_store.seen


def test_the_same_server_answers_a_real_principal(probe_store, monkeypatch):
    """The positive control. A refusal that fires on both states is a broken
    tool, not a working gate."""
    monkeypatch.setattr(server_mod, "current_principal",
                        lambda: Principal("key-positive-control"))
    result = _ten_stdio_calls(build_server(probe_store, networked=True))[0]

    assert not result.is_error, result.content[0].text
    # The bodies RAN. Ten calls, ten entries: the gate let every one through
    # rather than refusing on both states. (The recorded ids are None here
    # because the probe reads the real ContextVar, which nothing set on this
    # local build; what the gate saw is the patched reader.)
    assert len(probe_store.seen) == 10, probe_store.seen


def test_the_identity_fault_is_reported_once_per_process(probe_store,
                                                        monkeypatch, caplog):
    """Once per process, not once per request: an agent's retry loop emits
    faster than an operator reads it.

    Ten faulting calls, one warning line. The `suppressed` counter this test
    used to assert on was removed on 2026-09-05: a test assertion was its only
    reader, and this frame exists to keep writes-with-no-consumer out. Ten
    calls against one line is what proves the flag is doing its job, and it is
    what fails if the flag is deleted -- ten lines then arrive.
    """
    monkeypatch.setattr(server_mod, "current_principal", lambda: None)
    with caplog.at_level("WARNING", logger="contextlake.kb.server"):
        _ten_stdio_calls(build_server(probe_store, networked=True))

    faults = [r for r in caplog.records if "MCP identity fault" in r.getMessage()]
    assert len(faults) == 1, [r.getMessage() for r in faults]


# ==========================================================================
# What the frozen access-control anchor does NOT reach: `ask`s legs.
# ==========================================================================
def test_ask_dispatches_its_legs_below_the_guarded_wrapper(probe_store,
                                                           monkeypatch):
    """One `ask` call enters `guarded` once, not once per leg.

    `bounded_tool` registers `guarded` and returns the BARE `fn`, so `ask`
    calls its siblings by names that are unwrapped. Anything written at the
    access-control anchor therefore runs for `ask` and not for the tool `ask`
    dispatched to, and a caller granted `ask` reaches the rest through it.

    Counting the ONE read `guarded` makes per call is how that is measured
    from outside: the wrapper's identity read is the only thing in the frame
    today, so a leg that had crossed the wrapper would add a second read. If a
    later story wraps the legs, this test fails and sends the reader to the
    anchor comment, which is the point of writing it down.

    "who calls ingest" routes to CALLERS, which calls `find_callers`, so the
    leg is really dispatched: a green run with an empty answer would prove
    nothing.
    """
    calls = []

    def counted():
        calls.append(1)
        return Principal("key-ask")

    monkeypatch.setattr(server_mod, "current_principal", counted)

    async def run():
        async with Client(build_server(probe_store, networked=True)) as client:
            return await client.call_tool("ask", {"question": "who calls ingest"})

    result = asyncio.run(run())

    assert not result.is_error, result.content[0].text
    # The leg ran and answered. `a` (ForecastService) calls `b` (ingest).
    assert result.structured_content["route"] == "callers", result.structured_content
    assert [n["id"] for n in result.structured_content["nodes"]] == ["a"], \
        result.structured_content
    # One entry into `guarded`, for `ask` itself. The leg did not cross it.
    assert len(calls) == 1, len(calls)


# ==========================================================================
# AC8 -- the build-time refusal, with the combination named.
# ==========================================================================
@pytest.mark.parametrize("control", ["grant_source", "limiter", "usage"])
def test_a_network_only_control_is_refused_on_a_local_server(probe_store, control):
    """A new startup crash on a combination that has no call site today, so the
    combination is named here deliberately. Without this, a future helper that
    hits it reads as a regression rather than as the guard working."""
    with pytest.raises(ValueError) as caught:
        build_server(probe_store, **{control: object()})

    message = str(caught.value)
    assert control in message, message
    assert "networked" in message, message


def test_the_same_controls_are_accepted_on_a_networked_server(probe_store):
    """The positive control: the guard refuses the combination, not the
    parameter. A guard that refused both directions would pass the three
    assertions above while making the parameters unusable."""
    for control in ("grant_source", "limiter", "usage"):
        server = build_server(probe_store, networked=True, **{control: object()})
        assert server is not None


# ==========================================================================
# AC6 -- with every new keyword-only parameter omitted, today's object graph.
# ==========================================================================
def test_build_http_app_with_the_new_parameters_omitted_is_unchanged(probe_store):
    app = build_http_app(probe_store, transport="streamable-http",
                         host="127.0.0.1", token=SHARED)

    assert isinstance(app, server_mod.BearerAuthMiddleware)
    assert isinstance(app.app, server_mod._ToolLimiterLifespan)
    assert app._keyring is None

    # With no keyring the gate is the single shared token it has always been,
    # and it now files that traffic under the one reserved id.
    def scope(value: bytes) -> dict:
        return {"type": "http", "headers": [(b"authorization", value)]}

    assert app._principal(scope(f"Bearer {SHARED}".encode())) == \
        Principal(SHARED_TOKEN_KEY_ID)
    assert app._principal(scope(b"Bearer wrong")) is None
    assert app._principal(scope(f"Basic {SHARED}".encode())) is None
    assert app._principal({"type": "http", "headers": []}) is None


# ==========================================================================
# Local-first. The default transport must not change, and must not reach the
# networked builder at all.
# ==========================================================================
def test_stdio_run_server_never_reaches_build_http_app(probe_store, monkeypatch):
    reached = []

    def refuse(*args, **kwargs):
        reached.append(1)
        raise AssertionError("stdio built the networked app")

    async def noop(server, limit):
        return None

    monkeypatch.setattr(server_mod, "build_http_app", refuse)
    monkeypatch.setattr(server_mod, "_run_stdio", noop)
    server_mod.run_server(probe_store, transport="stdio")

    assert reached == []


_GOLDEN = Path(__file__).parent / "golden" / "stdio_tool_output.json"

# Every always-on tool, with arguments that exercise the seeded fixture. Counted
# 2026-09-05: 23 tools are registered unconditionally, and this list holds all
# 23. The two conditional ones (semantic_search, hybrid_search) need an embedder
# and a vector store and are NOT covered here; the count in the assertion says
# so rather than implying 25.
_ALL_TOOL_CALLS = [
    ("ask", {"question": "who calls ingest"}),
    ("blast_radius", {"node_id": "a"}),
    ("find_callees", {"node_id": "a"}),
    ("find_callers", {"node_id": "b"}),
    ("find_definition", {"name": "ingest"}),
    ("find_dependents", {"package": "requests"}),
    ("get_fleet_doc", {}),
    ("get_generated_doc", {"repo": "team/api"}),
    ("get_neighbors", {"node_id": "a"}),
    ("get_node", {"node_id": "a"}),
    ("get_readme", {"repo": "team/api"}),
    ("get_repo_brief", {"repo": "team/api"}),
    ("get_repo_links", {"repo": "team/api"}),
    ("get_wiki", {"repo": "team/api"}),
    ("graph_health", {}),
    ("graph_stats", {}),
    ("list_repos", {}),
    ("repo_dependencies", {"repo": "team/api"}),
    ("repo_event_flow", {"repo": "team/api"}),
    ("repo_flow", {"repo": "team/api"}),
    ("search_code", {"query": "ingest"}),
    ("shortest_path", {"src_id": "a", "dst_id": "b"}),
    ("who_knows", {"repo": "team/api"}),
]


def test_every_always_on_tool_returns_the_same_bytes_on_stdio(tmp_path):
    """Local-first property P4, on the 23 always-on tools.

    The golden file was captured from the build immediately before the identity
    gate landed. It is the only assertion that can catch the frame changing what
    a local user sees, and it cannot be re-derived once the old build is gone.
    """
    store = SqliteStore(tmp_path / "kb.sqlite")
    seed(store)
    try:
        server = build_server(store)

        async def run():
            out = {}
            async with Client(server) as client:
                for name, args in _ALL_TOOL_CALLS:
                    out[name] = (await client.call_tool(name, args)).structured_content
            return out

        produced = asyncio.run(run())
    finally:
        store.close()

    assert len(produced) == 23
    expected = json.loads(_GOLDEN.read_text())
    assert set(produced) == set(expected)
    for name in sorted(expected):
        assert produced[name] == expected[name], name
