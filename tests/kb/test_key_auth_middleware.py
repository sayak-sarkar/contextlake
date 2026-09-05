"""Multi-key auth on the network transports: the gate, the seven classes, the
one 401, and revocation with no restart.

Two things this file has to hold at once, and they pull in opposite directions.
The OPERATOR must be able to tell seven refusal classes apart, because each one
needs a different thing done about it. The unauthenticated CALLER must not be
able to tell any two of them apart, because a difference on the wire is a
working oracle over the key space and over the operator's staff changes. So the
parity test and the class test are both here, and neither is allowed to soften
the other.

EVERY KEY IN THIS FILE IS REAL AND EVERY ONE IS MINTED AT RUN TIME. Not one is
a literal, not one is written to a tracked path, and each lives only for the
test that drew it. They have to be real keys: the gate classifies a presented
value against `keys.check_format` before it looks anything up, so a readable
placeholder would be refused as `unknown` and the tests would be measuring the
refusal path while claiming to measure the admission path.

NOTHING IS SHIMMED. `contextlake.kb.keys` and `contextlake.kb.keyfile` are
used directly: real minting, the real CRC, the real digest, the real atomic
writer, and the real `Keyring` with its real `(st_ino, st_size, st_mtime_ns)`
freshness triple over a real file in `tmp_path`. Revocation here is a real
rewrite of that file, not a fake flipping a flag, which is the only form of the
test that can fail when the per-request reload call is deleted.
"""

from __future__ import annotations

import io
import json
import secrets
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone

import pytest

from contextlake.kb import keyfile
from contextlake.kb import keys as keys_mod
from contextlake.kb import server as server_mod
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import (
    REFUSAL_CLASSES,
    SHARED_TOKEN_KEY_ID,
    KeyAuthMiddleware,
    Principal,
    build_http_app,
)
from contextlake.kb.store.sqlite_store import SqliteStore

SHARED = "fake-shared-token-for-tests"  # noqa: S105 - synthetic, not a secret

LOOPBACK_BASE = "http://127.0.0.1:8765"
_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "key-auth", "version": "1"}},
}


def _call_stats(request_id: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "graph_stats", "arguments": {}}}


def write_keys(path, records) -> None:
    """Through the real atomic writer, so a rewrite lands a new inode."""
    keyfile.write_document(path, [record.to_dict() for record in records])


def make_key(records, name: str, *, expires: str | None = None):
    """One real key. Returns ``(record, plaintext)``; the plaintext is not stored."""
    return keys_mod.create(records, name, expires=expires)


def bad_checksum_value() -> str:
    """A real key with its checksum tail broken, and nothing else changed.

    Same 57 characters, same base62 alphabet, so it can only fail on the CRC.
    Built by rotating one tail character rather than by writing a literal: a
    literal would have to be a plausible key, which is the one thing no file in
    this repository may contain.
    """
    key, _ = keys_mod.mint()
    tail = key[-1]
    swap = keys_mod.ALPHABET[(keys_mod.ALPHABET.index(tail) + 1)
                             % len(keys_mod.ALPHABET)]
    broken = key[:-1] + swap
    assert len(broken) == keys_mod.KEY_LEN
    assert not keys_mod.check_format(broken)
    return broken


def malformed_value() -> str:
    """Our prefix, wrong length. Fails before the checksum is ever computed."""
    value = keys_mod.KEY_PREFIX + "short"
    assert not keys_mod.check_format(value)
    return value


def unissued_value() -> str:
    """Well-formed, valid CRC, in no key file. Free for an attacker to mint:
    the format is published, so this is the one refusal class that reaches the
    filesystem, and that is stated rather than claimed away."""
    key, _ = keys_mod.mint()
    assert keys_mod.check_format(key)
    return key


# --------------------------------------------------------------------------
# The keyring under test is the REAL one, over a real file in tmp_path.
# --------------------------------------------------------------------------
class Ring:
    """One real key file plus the real `Keyring` reading it.

    Holds the plaintexts so a test can present them. Nothing here writes
    outside ``tmp_path``.
    """

    def __init__(self, path) -> None:
        self.path = path
        self.records: list = []
        past = datetime.now(timezone.utc) - timedelta(days=1)
        self.live_a, self.value_a = make_key(self.records, "alpha")
        self.live_b, self.value_b = make_key(self.records, "bravo")
        self.revoked, self.value_revoked = make_key(self.records, "withdrawn")
        keys_mod.revoke(self.records, self.revoked, reason="left the team")
        self.expired, self.value_expired = keys_mod.create(
            self.records, "lapsed", expires="1d", now=past)
        assert self.expired.state() == "expired"
        write_keys(path, self.records)
        self.keyring = keyfile.Keyring.load(path)

    def revoke_alpha(self) -> None:
        """Rewrite the file the way `kb keys revoke` will: real writer, new inode."""
        keys_mod.revoke(self.records, self.live_a, reason="rotated out")
        write_keys(self.path, self.records)


@pytest.fixture
def ring(tmp_path):
    return Ring(tmp_path / "mcp-keys.json")


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


class IdStore(SqliteStore):
    """Records the key id each tool body saw, from inside the body."""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.seen: list[str | None] = []

    def stats(self):
        principal = server_mod.current_principal()
        self.seen.append(None if principal is None else principal.key_id)
        return super().stats()


@pytest.fixture
def store(tmp_path):
    s = IdStore(tmp_path / "kb.sqlite")
    seed(s)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def bounded_refusal_log():
    """The refusal-line bound is module state, and module state outlives a test.

    Without this, whichever test floods first eats into the per-class window
    every later test in the process reads, and the exact line counts below
    start depending on the order pytest happened to run in. Same seam as
    `keyfile.reset_counters`, which this file already uses.
    """
    server_mod.reset_refusal_log()
    yield
    server_mod.reset_refusal_log()


def scope(value: bytes | None) -> dict:
    headers = [] if value is None else [(b"authorization", value)]
    return {"type": "http", "headers": headers}


def gate(*, token=SHARED, keyring=None) -> KeyAuthMiddleware:
    """The middleware alone, with a sentinel app it must not reach."""
    async def refuse(*_a, **_k):  # pragma: no cover - reached only on a bug
        raise AssertionError("a refused request must not reach the app")

    return KeyAuthMiddleware(refuse, token, keyring=keyring, keys=keys_mod)


def bearer(value: str) -> bytes:
    return b"Bearer " + value.encode("latin-1")


# ==========================================================================
# The one that matters most. Two keys, one REAL BOUND SOCKET, two ids read
# inside two tool bodies, and a third request with a revoked key refused --
# on the SAME app object, with no restart and no signal between them.
# ==========================================================================
def test_two_keys_read_as_two_principals_and_a_revoked_one_is_refused(store, ring):
    import http.client

    # Lazy and by bare name, the house convention (see
    # test_wiki_jump_from_a_graph_node.py:37). Lazy because the identity file
    # imports from THIS module at import time, and a module-level import here
    # would close that cycle.
    from test_mcp_identity_propagates import bound_server

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)

    def call(hostport: str, key: str, request_id: int) -> int:
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            headers = {"Host": hostport, "Authorization": f"Bearer {key}",
                       **_JSON_HEADERS}
            conn.request("POST", "/mcp", body=json.dumps(_INITIALIZE),
                         headers=headers)
            conn.getresponse().read()
            conn.request("POST", "/mcp", body=json.dumps(_call_stats(request_id)),
                         headers=headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    buf = io.StringIO()
    with redirect_stderr(buf), bound_server(app) as hostport:
        assert call(hostport, ring.value_a, 2) == 200
        assert call(hostport, ring.value_b, 3) == 200
        # The key file is rewritten through the real writer. No restart, no new
        # app object, no signal: the next request pays for the change.
        ring.revoke_alpha()
        assert call(hostport, ring.value_a, 4) == 401

    # Two DIFFERENT ids, not one value arriving twice. A hard-coded constant
    # passes the weaker form and proves nothing.
    assert store.seen == [ring.live_a.id, ring.live_b.id], store.seen
    # The refused third call reached no tool body at all.
    assert len(store.seen) == 2, store.seen
    assert "revoked" in buf.getvalue()


def test_a_revoked_key_is_refused_on_the_next_request(store, ring):
    """The same revocation on the test client, so the failure is readable.

    The socket test above is the wire proof; this one is the one that names the
    defect when the per-request `reload_if_changed()` call is deleted.
    """
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    auth = {**_JSON_HEADERS, "Authorization": f"Bearer {ring.value_a}"}

    with redirect_stderr(io.StringIO()), TestClient(app, base_url=LOOPBACK_BASE) as client:
        before = client.post("/mcp", json=_INITIALIZE, headers=auth)
        ring.revoke_alpha()
        after = client.post("/mcp", json=_INITIALIZE, headers=auth)
        # The other key is untouched by the revocation. Without this, a gate
        # that started refusing EVERYTHING after the rewrite would pass.
        other = client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS, "Authorization": f"Bearer {ring.value_b}"})

    assert before.status_code == 200
    assert after.status_code == 401
    assert other.status_code == 200


def test_a_revoked_key_stops_working_on_a_live_sse_stream(store, ring):
    """A `GET /sse` authorised at connect does NOT stay authorised forever.

    The stream itself is not torn down -- the gate runs once per HTTP request,
    and the long-lived GET is one request -- but every `POST /messages/` on that
    stream is a separate request through the same gate, so revocation lands on
    the next call rather than at the next reconnect. That is the strongest true
    form of the claim, and the stream staying open is the documented exception
    beside it.
    """
    from test_mcp_identity_propagates import SseSession, bound_server

    app = build_http_app(store, transport="sse", host="127.0.0.1",
                         token=None, keyring=ring.keyring)

    with redirect_stderr(io.StringIO()), bound_server(app) as hostport:
        session = SseSession(hostport, ring.value_a)
        try:
            session.handshake(ring.value_a)
            assert session.post(_call_stats(2), ring.value_a) == 202
            session.reply()
            ring.revoke_alpha()
            # Same open stream, same session id, next message.
            assert session.post(_call_stats(3), ring.value_a) == 401
        finally:
            session.close()

    assert store.seen == [ring.live_a.id], store.seen


def test_the_gate_stats_the_key_file_on_every_authenticated_request(store, ring):
    """`reload_if_changed` runs per request, not once at startup.

    A keyring loaded once and never re-stated makes `kb keys revoke` a command
    that reports success and changes nothing until somebody restarts.
    """
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    auth = {**_JSON_HEADERS, "Authorization": f"Bearer {ring.value_a}"}

    trace: list = []
    keyfile.reset_counters(trace)
    try:
        with TestClient(app, base_url=LOOPBACK_BASE) as client:
            for _ in range(3):
                assert client.post("/mcp", json=_INITIALIZE,
                                   headers=auth).status_code == 200
        counts = keyfile.counters()
    finally:
        keyfile.reset_counters()

    assert counts["stat"] == 3, counts
    # Nothing moved, so the file is never re-read: one stat is the whole cost.
    assert counts["read"] == 0, counts
    assert {path for _op, path in trace} == {str(ring.path)}, trace


# ==========================================================================
# The order on the request path: format check, then stat, then resolve.
# Asserted by counting the keystore's OWN file accessor, because the counts
# are the only thing separating a correct order from a plausible one.
# ==========================================================================
@pytest.mark.parametrize("shape, stats, refusal", [
    pytest.param("none", 0, "no_header", id="no-header"),
    pytest.param("basic", 0, "wrong_scheme", id="wrong-scheme"),
    pytest.param("bare", 0, "wrong_scheme", id="no-scheme-at-all"),
    pytest.param("malformed", 0, "malformed", id="malformed"),
    pytest.param("bad_checksum", 0, "bad_checksum", id="bad-checksum"),
    pytest.param("notakey", 0, "unknown", id="not-our-shape-at-all"),
    # The shape an attacker mints offline for free: the format is published, so
    # a valid checksum costs nothing to produce, and admission is not checked
    # before identity resolves. It reaches the filesystem, exactly once.
    pytest.param("unissued", 1, "unknown", id="well-formed-unknown"),
    pytest.param("revoked", 1, "revoked", id="revoked"),
    pytest.param("expired", 1, "expired", id="expired"),
])
def test_only_a_well_formed_value_reaches_the_filesystem(ring, shape, stats,
                                                         refusal):
    values = {
        "none": None,
        "basic": b"Basic ZmFrZTpmYWtl",
        "bare": SHARED.encode(),
        "malformed": bearer(malformed_value()),
        "bad_checksum": bearer(bad_checksum_value()),
        "notakey": b"Bearer notakey",
        "unissued": bearer(unissued_value()),
        "revoked": bearer(ring.value_revoked),
        "expired": bearer(ring.value_expired),
    }
    app = gate(token=None, keyring=ring.keyring)

    keyfile.reset_counters()
    try:
        principal, got, _key_id = app._authenticate(scope(values[shape]))
        counts = keyfile.counters()
    finally:
        keyfile.reset_counters()

    assert principal is None
    assert got == refusal
    assert counts["stat"] == stats, (refusal, counts)


def test_the_stat_counter_sees_an_admitted_request(ring):
    """The positive control for every zero above.

    A counter wired to nothing reads 0 for all six zero parameters and every
    one of them passes.
    """
    app = gate(token=None, keyring=ring.keyring)

    keyfile.reset_counters()
    try:
        principal, refusal, _ = app._authenticate(scope(bearer(ring.value_a)))
        counts = keyfile.counters()
    finally:
        keyfile.reset_counters()

    assert principal == Principal(ring.live_a.id)
    assert refusal is None
    assert counts["stat"] == 1, counts


def test_the_classifier_reads_no_file_in_either_direction():
    """Both halves in one test: the format check is file-free for a value it
    accepts as well as for one it rejects."""
    keyfile.reset_counters()
    try:
        assert server_mod._classify_presented(bearer(unissued_value())[7:],
                                              keys_mod) == "ok"
        assert server_mod._classify_presented(malformed_value().encode(),
                                              keys_mod) == "malformed"
        assert server_mod._classify_presented(bad_checksum_value().encode(),
                                              keys_mod) == "bad_checksum"
        assert server_mod._classify_presented(b"notakey", keys_mod) is None
        counts = keyfile.counters()
    finally:
        keyfile.reset_counters()

    assert counts == {"stat": 0, "read": 0}, counts


# ==========================================================================
# The seven classes: different on the operator's terminal, identical on the
# wire. Both halves, and a positive control inside each.
# ==========================================================================
def _drive_configuration_a(store, ring) -> tuple[list, str]:
    """Every one of the seven classes plus one ADMITTED request, one app.

    Configuration A: a key file holding a live key, a revoked one and an
    expired one, with CONTEXTLAKE_MCP_TOKEN unset. The shared-token branch is
    suppressed, so all seven are reachable from this one app object.
    """
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    cases = [
        None,
        "Basic ZmFrZTpmYWtl",
        SHARED,                                   # a bare value, no scheme
        f"Bearer {malformed_value()}",
        f"Bearer {bad_checksum_value()}",
        f"Bearer {unissued_value()}",
        "Bearer notakey",                         # the widened `unknown`
        f"Bearer {ring.value_revoked}",
        f"Bearer {ring.value_expired}",
    ]
    refused, buf = [], io.StringIO()
    with redirect_stderr(buf), TestClient(app, base_url=LOOPBACK_BASE) as client:
        for value in cases:
            headers = dict(_JSON_HEADERS)
            if value is not None:
                headers["Authorization"] = value
            refused.append(client.post("/mcp", json=_INITIALIZE, headers=headers))
        # The positive control, in the SAME test and through the same app: a
        # gate that refused everything would satisfy every assertion above it.
        admitted = client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS, "Authorization": f"Bearer {ring.value_a}"})
    assert admitted.status_code == 200, admitted.text
    return refused, buf.getvalue()


def _drive_configuration_b(store) -> list:
    """Configuration B: no key file, one shared token. Three of the seven."""
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=SHARED)
    refused = []
    with redirect_stderr(io.StringIO()), TestClient(app, base_url=LOOPBACK_BASE) as client:
        for value in (None, "Basic ZmFrZTpmYWtl", "Bearer fake-wrong-token"):
            headers = dict(_JSON_HEADERS)
            if value is not None:
                headers["Authorization"] = value
            refused.append(client.post("/mcp", json=_INITIALIZE, headers=headers))
        admitted = client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS, "Authorization": f"Bearer {SHARED}"})
    assert admitted.status_code == 200, admitted.text
    return refused


def test_every_refusal_class_returns_one_body(store, ring, tmp_path):
    """Counted over BOTH configurations.

    A body that differs between them still leaks, and configuration B is the
    one an operator runs today.
    """
    a, _lines = _drive_configuration_a(store, ring)
    second = IdStore(tmp_path / "b.sqlite")
    seed(second)
    try:
        b = _drive_configuration_b(second)
    finally:
        second.close()

    responses = a + b
    assert len(responses) == 12
    assert {r.status_code for r in responses} == {401}
    assert {r.headers["www-authenticate"] for r in responses} == {"Bearer"}
    assert len({r.content for r in responses}) == 1

    # THE WHOLE RESPONSE, not three fields of it. The three assertions above
    # read the status, one named header and the body, and are blind to every
    # other header, so a header that differs by refusal class -- an
    # `x-auth-reason`, a `retry-after` on the expired class, a `content-length`
    # that tracked a per-class body -- would ship with this suite green.
    #
    # `headers.raw` is the ordered list of byte pairs the ASGI app sent, so one
    # comparison covers a new header, a dropped one, a reordering and a value
    # that varies.
    shapes = {(r.status_code, tuple(r.headers.raw), r.content)
              for r in responses}
    assert len(shapes) == 1, sorted(tuple(sorted(s[1])) for s in shapes)

    # The header NAMES are pinned too, so an addition made to every class at
    # once still has to be typed here and read by somebody. Nothing else is in
    # the set: there is no server in this stack to stamp a `date`, which is
    # asserted rather than assumed -- if one ever appears, exclude it BY NAME
    # and say why, never with a filter that would also swallow a
    # class-distinguishing header.
    (_status, headers, _body), = shapes
    assert [name for name, _value in headers] == [
        b"content-type", b"content-length", b"www-authenticate"], headers


def test_the_401_body_says_what_to_send_and_that_there_is_no_oauth(store, ring):
    """Pinned as literal bytes, not as the constant the code reads.

    `r.content == server_mod._UNAUTHORIZED_BODY` passes for any body, today's
    `{"error":"unauthorized"}` included, which is the string that sends a
    client into an OAuth flow this server will never have.
    """
    a, _ = _drive_configuration_a(store, ring)
    body = a[0].content

    assert body == (
        b'{"error":"unauthorized","detail":"This server requires an API key. '
        b'Send: Authorization: Bearer <key>. There is no OAuth flow here. Ask '
        b'the operator for a key (contextlake kb keys create)."}')
    assert b"Authorization: Bearer" in body
    assert b"contextlake kb keys create" in body
    assert b"no OAuth flow" in body
    # A content-length that disagrees with the body hangs the client.
    assert int(a[0].headers["content-length"]) == len(body)
    # The bare challenge: no realm, and no RFC 9728 resource_metadata, which
    # would point a client at metadata this server does not serve.
    assert a[0].headers["www-authenticate"] == "Bearer"


def test_the_operator_gets_one_line_per_refusal_naming_its_class(store, ring):
    """DEPENDS ON `bounded_refusal_log`, which resets the per-class window.

    The operator line is capped at `REFUSAL_LOG_CAP` per class per window, and
    that window is module state. This file drives `unknown` about a dozen times
    across its tests, so today the count stays under the cap even with the
    fixture removed -- measured, not assumed. Add a few more `unknown` refusals
    to this file and it would not, and the failure would land HERE, on a count
    of 10 instead of 9, in a test that has nothing to do with the cap. The
    fixture is what keeps that from ever being the way somebody finds out.
    """
    _refused, lines = _drive_configuration_a(store, ring)
    emitted = [line for line in lines.splitlines() if "MCP auth refused" in line]

    # Nine refusals drove seven classes, and the count PER class is pinned, not
    # just its presence. `unknown` has to read 2: one well-formed value the
    # server never issued, and one value that is not `ctxlake_`-shaped at all.
    # Narrowing `unknown` back to the well-formed-only definition drops the
    # second to 0, which a ">= 1" assertion cannot see.
    expected = {"no_header": 1, "wrong_scheme": 2, "malformed": 1,
                "bad_checksum": 1, "unknown": 2, "revoked": 1, "expired": 1}
    assert set(expected) == set(REFUSAL_CLASSES)
    assert sum(expected.values()) == 9
    counted = {name: sum(f"refused: {name}" in line for line in emitted)
               for name in REFUSAL_CLASSES}
    assert counted == expected, emitted
    assert len(emitted) == 9, emitted
    assert len(REFUSAL_CLASSES) == 7
    assert len(set(REFUSAL_CLASSES)) == 7


def test_no_operator_line_carries_any_part_of_the_presented_value(store, ring):
    """The marker is 20 characters and is not `ctxlake_`-shaped, so it cannot
    collide with a checksum tail or with a key id."""
    from starlette.testclient import TestClient

    marker = "MARKERQQQQZZZZ999900"
    # BOTH values that end in `unknown`, because they reach it down DIFFERENT
    # branches. `notakey<marker>` is not `ctxlake_`-shaped and falls through the
    # shared-token branch; the second is well-formed, valid CRC, and resolved to
    # no record. A test that drives only one leaves the other branch free to
    # print the presented value.
    unissued = unissued_value()
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    buf = io.StringIO()
    with redirect_stderr(buf), TestClient(app, base_url=LOOPBACK_BASE) as client:
        for value in (f"Bearer notakey{marker}", f"Bearer {unissued}"):
            assert client.post("/mcp", json=_INITIALIZE, headers={
                **_JSON_HEADERS, "Authorization": value}).status_code == 401
        # Positive control, in the SAME capture: a revoked key DOES put a key
        # id on the line, so a scanner reading nothing cannot pass by seeing
        # nothing.
        assert client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS,
            "Authorization": f"Bearer {ring.value_revoked}"}).status_code == 401

    lines = buf.getvalue()
    assert marker not in lines
    for start in range(len(marker) - 3):
        assert marker[start:start + 4] not in lines, marker[start:start + 4]
    assert unissued not in lines
    for start in range(len(unissued) - 3):
        assert unissued[start:start + 4] not in lines, unissued[start:start + 4]
    # A key id is not a secret: a digest this process holds matched a record
    # this process owns, so the id is the server's own name for it.
    assert ring.revoked.id in lines
    assert "revoked" in lines


def test_no_operator_line_ever_matches_the_key_format(store, ring):
    """0 lines carry a `ctxlake_` value, on any class, including the two that
    resolved to a real record."""
    import re

    _refused, lines = _drive_configuration_a(store, ring)
    pattern = re.compile(r"ctxlake_[A-Za-z0-9]{49}")

    assert pattern.findall(lines) == []
    for value in (ring.value_a, ring.value_revoked, ring.value_expired):
        assert value not in lines
        # No four-character run of any presented key either.
        for start in range(0, len(value) - 3, 7):
            assert value[start:start + 4] not in lines, value[start:start + 4]
    # The capture is not empty, so the scan above is not vacuous.
    assert lines.count("MCP auth refused") == 9


def test_a_refusal_never_names_a_key_id_the_server_never_issued(ring):
    """`unknown`, `malformed` and `bad_checksum` reach the reporter with no id.

    An id on one of those lines could only have come from the presented value.
    """
    app = gate(token=None, keyring=ring.keyring)

    for value, cls in ((b"Bearer notakey", "unknown"),
                       (bearer(unissued_value()), "unknown"),
                       (bearer(malformed_value()), "malformed"),
                       (bearer(bad_checksum_value()), "bad_checksum")):
        _p, got, key_id = app._authenticate(scope(value))
        assert (got, key_id) == (cls, None)

    for value, cls, expected in ((ring.value_revoked, "revoked", ring.revoked.id),
                                 (ring.value_expired, "expired", ring.expired.id)):
        _p, got, key_id = app._authenticate(scope(bearer(value)))
        assert (got, key_id) == (cls, expected)


def test_a_non_ascii_credential_is_a_401_and_never_a_500(ring):
    """`Bearer tökén`, pre-encoded, is what a hostile client can put on the
    wire. `hmac.compare_digest` raises TypeError on a str carrying non-ASCII,
    and a strict decode inside the format check would put the same 500 back one
    layer up, so this runs against BOTH configurations."""
    hostile = "Bearer tökén".encode("latin-1")

    with_keys = gate(token=None, keyring=ring.keyring)
    assert with_keys._authenticate(scope(hostile)) == (None, "unknown", None)

    shared_only = gate(token=SHARED)
    assert shared_only._authenticate(scope(hostile)) == (None, "unknown", None)


# ==========================================================================
# The three-row token/keyring matrix. A boolean loses the middle row.
# ==========================================================================
def test_a_minted_shape_token_is_refused_once_keys_exist(store, ring):
    """Suppression asserted on the RESPONSE, not on a `token=None` argument.

    `token or resolve_token()[0]` in run_server used to hand the gate a freshly
    minted value on exactly this call, so the property "there is no shared
    token" was false while every configuration-shaped test said it was true.
    """
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    assert app._token is None

    minted = secrets.token_urlsafe(32)
    assert len(minted) == 43
    with redirect_stderr(io.StringIO()), TestClient(app, base_url=LOOPBACK_BASE) as client:
        refused = client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS, "Authorization": f"Bearer {minted}"})
        # Positive control: the real keys still work on this same app object.
        ok = client.post("/mcp", json=_INITIALIZE, headers={
            **_JSON_HEADERS, "Authorization": f"Bearer {ring.value_a}"})

    assert refused.status_code == 401
    assert ok.status_code == 200


def test_an_operator_set_shared_token_stays_live_beside_the_keys(store, ring):
    """Row three: both branches exist, and the shared value is filed under the
    one reserved id so a usage row can tell it from a real key."""
    from starlette.testclient import TestClient

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=SHARED, keyring=ring.keyring)

    with TestClient(app, base_url=LOOPBACK_BASE) as client:
        for key in (SHARED, ring.value_b):
            auth = {**_JSON_HEADERS, "Authorization": f"Bearer {key}"}
            assert client.post("/mcp", json=_INITIALIZE,
                               headers=auth).status_code == 200
            assert client.post("/mcp", json=_call_stats(),
                               headers=auth).status_code == 200

    assert store.seen == [SHARED_TOKEN_KEY_ID, ring.live_b.id], store.seen


def test_every_admitted_request_carries_a_principal_in_all_three_rows(ring,
                                                                      tmp_path):
    """Row by row, read from INSIDE a tool body.

    This is the precondition the tool wrapper's fail-closed refusal rests on. A
    branch that admits without setting a principal turns the shipped default
    into a server that refuses every tool call.
    """
    from starlette.testclient import TestClient

    rows = [
        ("shared only", {"token": SHARED}, SHARED, SHARED_TOKEN_KEY_ID),
        ("keys only", {"token": None, "keyring": ring.keyring}, ring.value_a,
         ring.live_a.id),
        ("both", {"token": SHARED, "keyring": ring.keyring}, SHARED,
         SHARED_TOKEN_KEY_ID),
    ]
    for index, (label, kwargs, key, expected) in enumerate(rows):
        s = IdStore(tmp_path / f"row{index}.sqlite")
        seed(s)
        try:
            app = build_http_app(s, transport="streamable-http",
                                 host="127.0.0.1", **kwargs)
            auth = {**_JSON_HEADERS, "Authorization": f"Bearer {key}"}
            with TestClient(app, base_url=LOOPBACK_BASE) as client:
                assert client.post("/mcp", json=_INITIALIZE,
                                   headers=auth).status_code == 200
                assert client.post("/mcp", json=_call_stats(),
                                   headers=auth).status_code == 200
            assert s.seen == [expected], (label, s.seen)
            assert None not in s.seen, label
        finally:
            s.close()


def test_a_socket_with_no_credential_at_all_is_refused_at_build_time(store):
    """No token and no keyring is not a configuration, it is an open socket."""
    with pytest.raises(ValueError, match="token, a keyring, or both"):
        build_http_app(store, transport="streamable-http", host="127.0.0.1",
                       token=None)


class _FakeUvicorn:
    """`uvicorn.run` must not bind anything in these tests."""

    def run(self, *args, **kwargs) -> None:
        pass


def test_run_server_does_not_re_mint_a_suppressed_token(store, ring, monkeypatch):
    """The fallback that made suppression unexpressible, asserted at the seam.

    `run_server(token=None, keyring=...)` must reach `build_http_app` with
    `token=None`, not with a fresh mint.
    """
    import sys

    seen: dict = {}

    def capture(*args, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(server_mod, "build_http_app", capture)
    monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn())

    server_mod.run_server(store, transport="streamable-http", token=None,
                          keyring=ring.keyring)
    assert seen["token"] is None
    assert seen["keyring"] is ring.keyring

    # The other direction, so this is not a gate that refuses everything: with
    # no keyring, a forgotten token is still minted rather than left off.
    monkeypatch.delenv(server_mod.TOKEN_ENV, raising=False)
    server_mod.run_server(store, transport="streamable-http", token=None)
    assert isinstance(seen["token"], str) and len(seen["token"]) >= 32
    assert seen["keyring"] is None


# ==========================================================================
# The properties that exist for a recorded reason and must survive.
# ==========================================================================
def test_a_non_http_scope_passes_through_untouched():
    """Gating `lifespan` leaves the SDK session manager never started, so the
    server fails as "hangs" rather than as "unauthorised"."""
    import anyio

    seen: list[str] = []
    sent: list[dict] = []

    async def inner(scope_, receive, send):
        seen.append(scope_["type"])

    async def send(message):  # pragma: no cover - reached only on a bug
        sent.append(message)

    app = KeyAuthMiddleware(inner, SHARED)

    async def drive():
        await app({"type": "lifespan"}, None, send)
        await app({"type": "websocket", "headers": []}, None, send)

    anyio.run(drive)

    assert seen == ["lifespan", "websocket"]
    assert sent == []


def test_the_lifespan_scope_reaches_the_inner_app_through_the_real_stack(store,
                                                                        ring):
    from starlette.testclient import TestClient

    seen: list[str] = []
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=SHARED, keyring=ring.keyring)
    inner = app.app.app

    async def recording(scope_, receive, send):
        seen.append(scope_["type"])
        await inner(scope_, receive, send)

    app.app.app = recording
    with redirect_stderr(io.StringIO()), TestClient(app, base_url=LOOPBACK_BASE) as client:
        assert client.post("/mcp", json=_INITIALIZE,
                           headers=dict(_JSON_HEADERS)).status_code == 401

    assert seen.count("lifespan") >= 1, seen
    # The 401 above never reached the inner app, which is the other half.
    assert seen.count("http") == 0, seen


def test_the_real_keyring_satisfies_the_shape_the_gate_calls():
    """The two modules cannot drift apart without this failing.

    An attribute check, not a construction: it holds whatever state the
    keystore area's own work is in.
    """
    for name in KeyAuthMiddleware.KEYRING_METHODS:
        assert callable(getattr(keyfile.Keyring, name, None)), name


def test_the_keyring_shape_is_checked_at_build_time(store):
    """A keyring missing a method must crash the build, not every request.

    Per request it would be an AttributeError inside the gate, which is a 500
    on the auth path on every authenticated call.
    """
    class Partial:
        def resolve(self, presented):  # pragma: no cover - never called
            return None

    with pytest.raises(TypeError) as caught:
        build_http_app(store, transport="streamable-http", host="127.0.0.1",
                       token=None, keyring=Partial())

    assert "reload_if_changed" in str(caught.value)
    assert "resolve(presented)" in str(caught.value)


def test_the_refusal_reporter_refuses_a_class_outside_the_vocabulary():
    """A free-text reason string is how a class name later reaches a log line
    as attacker-controlled text."""
    with pytest.raises(ValueError, match="not a refusal class"):
        server_mod._report_refusal("whatever-the-caller-sent")


# ==========================================================================
# The refusal CLOCK. Identical bytes are worth nothing while the time
# differs: a found record used to parse a date and read the clock, and an
# unknown key returned before either. Both halves are guarded separately,
# because either one can be reverted on its own and leave the other green.
# ==========================================================================
def _work_done(app, value, monkeypatch_target, clock_reads):
    """Run one authentication and return what it cost, in counted calls."""
    parses: list = []
    decoys: list = []
    real_parse = keys_mod._parse_ts
    real_decoy = keys_mod.decoy_state

    def counting_parse(text):
        parses.append(text)
        return real_parse(text)

    def counting_decoy(now=None):
        decoys.append(1)
        return real_decoy(now)

    monkeypatch_target.setattr(keys_mod, "_parse_ts", counting_parse)
    monkeypatch_target.setattr(keys_mod, "decoy_state", counting_decoy)
    monkeypatch_target.setattr(keys_mod, "_now", clock_reads.reader)
    clock_reads.calls.clear()
    result = app._authenticate(scope(value))
    return {"result": result, "parses": len(parses), "decoys": len(decoys),
            "clock": len(clock_reads.calls)}


class _CountingClock:
    """One counter for BOTH clocks on the auth path.

    The keyring reads its own wall clock after a dict hit; the decoy reads
    `keys._now`. Counting them into one list is what makes "one clock read
    either way" a single assertion instead of two that can disagree.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def reader(self):
        self.calls.append(1)
        return datetime.now(timezone.utc)


def test_a_known_and_an_unknown_key_cost_the_same_work(tmp_path, monkeypatch):
    """The timing side channel, counted rather than timed.

    A timing assertion on a loaded machine proves nothing in either direction.
    These are the two things the two paths used to differ by: one
    `datetime.strptime` and one clock read. The measurement lives in the
    `contextlake.kb.keys` module docstring; this is the gate that keeps it true.
    """
    clock = _CountingClock()
    ring = Ring(tmp_path / "mcp-keys.json")
    keyring = keyfile.Keyring.load(ring.path, now=clock.reader)
    app = gate(token=None, keyring=keyring)

    # Warm-up, before any counter is read: it settles the freshness stamp, so
    # the counted calls below are the request path and not a reload.
    assert app._authenticate(scope(bearer(ring.value_a)))[0] is not None

    known = _work_done(app, bearer(ring.value_a), monkeypatch, clock)
    unknown = _work_done(app, bearer(unissued_value()), monkeypatch, clock)

    assert known["result"] == (Principal(ring.live_a.id), None, None)
    assert unknown["result"] == (None, "unknown", None)

    # Guard A -- the parse. Reverting the cached deadline in `KeyRecord` makes
    # the known path 1 and leaves the unknown path 0.
    assert known["parses"] == unknown["parses"] == 0, (known, unknown)

    # Guard B -- the clock and the decoy. Deleting the `decoy_state()` call
    # from the gate makes the unknown path 0 on both counters, and Guard A
    # cannot see that: after the parse fix both paths read 0 parses either way.
    assert known["clock"] == unknown["clock"] == 1, (known, unknown)
    assert unknown["decoys"] == 1, unknown
    assert known["decoys"] == 0, known

    # A revoked and an expired key are on the same path as a live one, so the
    # answer cannot be read off the work either.
    for value, refusal in ((ring.value_revoked, "revoked"),
                           (ring.value_expired, "expired")):
        seen = _work_done(app, bearer(value), monkeypatch, clock)
        assert seen["result"][1] == refusal
        assert seen["parses"] == 0, (refusal, seen)
        assert seen["clock"] == 1, (refusal, seen)


def test_the_work_counters_are_not_vacuous(tmp_path, monkeypatch):
    """The positive control for every zero above.

    Counters wired to nothing read 0 everywhere and every assertion passes.
    Building a record parses, and resolving a known key reads a clock.
    """
    clock = _CountingClock()
    ring = Ring(tmp_path / "mcp-keys.json")

    parses: list = []
    real_parse = keys_mod._parse_ts
    monkeypatch.setattr(keys_mod, "_parse_ts",
                        lambda text: (parses.append(text), real_parse(text))[1])

    # Loading the key file builds the records, and that is where the parses are.
    keyring = keyfile.Keyring.load(ring.path, now=clock.reader)
    assert len(parses) >= len(ring.records), (len(parses), len(ring.records))

    app = gate(token=None, keyring=keyring)
    clock.calls.clear()
    assert app._authenticate(scope(bearer(ring.value_a)))[0] is not None
    assert len(clock.calls) == 1, clock.calls


# ==========================================================================
# The refusal is owed to the caller before it is owed to the operator.
# ==========================================================================
def test_the_401_goes_out_before_the_reporter_can_raise(ring, monkeypatch):
    """A vocabulary violation must not cost the caller its response.

    The reporter raises on a class outside `REFUSAL_CLASSES`. With the report
    ahead of the send, that raise unwound the request with NOTHING written: the
    caller got a dropped connection instead of a 401, and a client that retries
    a dropped connection retries forever.

    The out-of-vocabulary class is forced by narrowing the vocabulary, so the
    real gate drives the real reporter down its real branch. Patching
    `_authenticate` instead would test the patch.
    """
    import anyio

    monkeypatch.setattr(server_mod, "REFUSAL_CLASSES", ("no_header",))
    sent: list = []

    async def send(message):
        sent.append(message)

    app = gate(token=None, keyring=ring.keyring)

    async def drive():
        await app(scope(b"Bearer notakey"), None, send)

    with redirect_stderr(io.StringIO()), pytest.raises(ValueError,
                                                       match="not a refusal class"):
        anyio.run(drive)

    assert [m["type"] for m in sent] == ["http.response.start",
                                         "http.response.body"], sent
    assert sent[0]["status"] == 401
    assert sent[1]["body"] == server_mod._UNAUTHORIZED_BODY
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]


def test_a_refusal_in_the_vocabulary_still_reports_after_the_401(ring):
    """The other direction: the reorder did not drop the operator line."""
    import anyio

    server_mod.reset_refusal_log()
    sent: list = []

    async def send(message):
        sent.append(message)

    app = gate(token=None, keyring=ring.keyring)
    buf = io.StringIO()

    async def drive():
        await app(scope(b"Bearer notakey"), None, send)

    with redirect_stderr(buf):
        anyio.run(drive)

    assert [m["type"] for m in sent] == ["http.response.start",
                                         "http.response.body"]
    assert "MCP auth refused: unknown" in buf.getvalue()


# ==========================================================================
# The operator line is BOUNDED. It sits on the path this module's own
# comments say a flood is designed to exercise.
# ==========================================================================
def test_the_refusal_line_is_capped_per_class_and_says_what_it_dropped():
    out = io.StringIO()
    clock = [1000.0]
    log = server_mod._RefusalLog(cap=3, window=60.0, clock=lambda: clock[0],
                                 stream=out)

    for _ in range(3 + 5):
        log.report("unknown")
    # A flood of one class does not silence another, which is the whole reason
    # the seven classes are kept apart.
    log.report("revoked", key_id="k_abc123")

    first = out.getvalue().splitlines()
    assert [line for line in first if "unknown" in line] == [
        "  MCP auth refused: unknown",
        "  MCP auth refused: unknown",
        "  MCP auth refused: unknown",
        "  MCP auth refused: unknown -- further lines suppressed for 60s",
    ], first
    assert "  MCP auth refused: revoked key=k_abc123" in first

    # THE READER OF THE SUPPRESSED COUNTER. Without this the counter is a
    # number nothing ever prints, which passes every test and tells nobody.
    clock[0] += 61.0
    log.report("unknown")
    assert out.getvalue().splitlines()[len(first):] == [
        "  MCP auth refused: unknown x5 more, suppressed in the last 60s",
        "  MCP auth refused: unknown",
    ]


def test_a_flood_through_the_real_gate_does_not_write_a_line_per_request(ring):
    """The bound reached through `_report_refusal`, not through a hand-built log.

    A cap wired only into a class nothing calls is a cap on nothing.
    """
    import anyio

    server_mod.reset_refusal_log()
    app = gate(token=None, keyring=ring.keyring)
    buf = io.StringIO()

    async def send(_message):
        pass

    async def drive():
        for _ in range(200):
            await app(scope(b"Bearer notakey"), None, send)

    with redirect_stderr(buf):
        anyio.run(drive)

    lines = [line for line in buf.getvalue().splitlines()
             if "MCP auth refused" in line]
    # The cap, plus the one line that says suppression started. 200 refusals
    # wrote 21 lines, not 200.
    assert len(lines) == server_mod.REFUSAL_LOG_CAP + 1, len(lines)
    assert lines[-1].endswith("further lines suppressed for 60s"), lines[-1]
    server_mod.reset_refusal_log()


def test_the_shipped_bound_is_wide_enough_for_a_person_debugging_a_client():
    """The cap is a flood bound, not a debugging bound.

    A misconfigured client retries a handful of times and its operator has to
    see every one of them. Narrowing this to single digits would hide the
    traffic the line exists for.
    """
    assert server_mod.REFUSAL_LOG_CAP >= 20
    assert server_mod.REFUSAL_LOG_WINDOW <= 300


# ==========================================================================
# A shared token shaped like a key contextlake mints.
# ==========================================================================
def test_a_key_shaped_shared_token_is_refused_at_build_time(store, ring):
    """It could never authenticate, and the banner says it is live.

    The gate classifies by shape first, so a `ctxlake_`-shaped value goes to
    the key file, resolves to no record and is refused as `unknown`. It never
    reaches the shared-token comparison. Meanwhile the startup banner tells the
    operator the variable "bypasses every per-key limit and scope".
    """
    key, _digest = keys_mod.mint()

    with pytest.raises(ValueError) as caught:
        build_http_app(store, transport="streamable-http", host="127.0.0.1",
                       token=key, keyring=ring.keyring)
    message = str(caught.value)
    assert server_mod.TOKEN_ENV in message
    assert keys_mod.KEY_PREFIX in message
    assert "revoke" in message
    # The message never echoes the value it refused.
    assert key not in message
    assert key[len(keys_mod.KEY_PREFIX):len(keys_mod.KEY_PREFIX) + 8] not in message

    # The same refusal with NO key file, so the variable means one thing in
    # every configuration. Without this, creating a first key would silently
    # stop an operator's pinned token working.
    with pytest.raises(ValueError, match=server_mod.TOKEN_ENV):
        build_http_app(store, transport="streamable-http", host="127.0.0.1",
                       token=key)

    # The other direction: a token contextlake did not mint is untouched.
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=SHARED, keyring=ring.keyring)
    assert app._token == SHARED.encode("utf-8")


def test_resolve_token_refuses_a_key_shaped_environment_value(monkeypatch):
    """`cmds/serve.py` calls this before it ever reaches `build_http_app`."""
    key, _digest = keys_mod.mint()
    monkeypatch.setenv(server_mod.TOKEN_ENV, key)
    with pytest.raises(ValueError, match="can never authenticate"):
        server_mod.resolve_token()

    # A value this tool did not mint still comes back as the pinned token, and
    # an unset variable still mints. Neither is broken by the guard.
    monkeypatch.setenv(server_mod.TOKEN_ENV, SHARED)
    assert server_mod.resolve_token() == (SHARED, True)
    monkeypatch.delenv(server_mod.TOKEN_ENV)
    minted, from_env = server_mod.resolve_token()
    assert from_env is False
    assert len(minted) >= 32
    assert not minted.startswith(keys_mod.KEY_PREFIX)
