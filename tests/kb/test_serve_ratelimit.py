"""The quota AT THE GATE: admission, the 429, and the charge inside the slot.

Three properties this file exists to hold, each with a test that fails when it
is broken and a break-test that was run to prove the test can fail:

1. **Admission is in the middleware, not in the tool wrapper.** A limiter in
   `guarded` passes every functional throttling test while refusing after the
   body has been parsed, after the request has queued for a concurrency slot,
   and after `ask` has fanned out to its eight bare legs -- and while missing
   `tools/list` and the `kb://stats` resource, which cross no wrapper at all.
   `test_a_throttled_request_never_reaches_the_app` is the one that can see it.
2. **Identity resolves first, so the map is keyed by ids this server minted.**
   `test_ten_thousand_forged_bearers_allocate_nothing` is the memory property.
3. **The charge is timed INSIDE the slot.** Outside it, a cheap tool queued
   behind an expensive one is billed for the wait, by a key that did not spend
   it. `test_a_slot_wait_is_not_billed_to_the_caller_that_waited` needs
   engineered contention or it cannot fail.

The end-to-end tests bind a REAL SOCKET on an ephemeral port rather than using
`starlette.testclient.TestClient`, which runs one portal and hangs on this
server's long-lived streams.
"""
from __future__ import annotations

import asyncio
import io
import json
import pathlib
import re
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr

import pytest

from contextlake.kb import keyfile, ratelimit
from contextlake.kb import keys as keys_mod
from contextlake.kb import server as server_mod
from contextlake.kb.model import Node
from contextlake.kb.server import (
    REFUSAL_CLASSES,
    KeyAuthMiddleware,
    build_http_app,
    build_server,
)
from contextlake.kb.store.sqlite_store import SqliteStore

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "quota", "version": "1"}},
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("team/api", [
        Node(id="a", repo="team/api", kind="function", name="ForecastService",
             file="svc.py", line_start=1, line_end=3),
    ])
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def bounded_logs():
    """Both refusal-line bounds are module state, and module state outlives a test."""
    server_mod.reset_refusal_log()
    yield
    server_mod.reset_refusal_log()


class Ring:
    """One real key file with one real live key, plus the real `Keyring`."""

    def __init__(self, path, policy=None):
        self.path = path
        self.records: list = []
        self.record, self.value = keys_mod.create(self.records, "alpha")
        if policy:
            self.record.policy.update(policy)
        keyfile.write_document(path, [r.to_dict() for r in self.records])
        self.keyring = keyfile.Keyring.load(path)


def _limits(**kwargs):
    return ratelimit.ServeDefaults(
        rate=ratelimit.parse_rate(kwargs.get("rate")),
        burst=ratelimit.burst_number(kwargs.get("burst")),
        cost_budget=ratelimit.parse_cost_budget(kwargs.get("cost_budget")))


class _Counting:
    """A downstream ASGI app that counts the calls the gate let through.

    NOT an assertion on `_tool_slots`: that is closure-local, and
    `Semaphore._value` is private. What matters is whether the request reached
    anything at all, and this is the only thing that can say so.
    """

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(app, *, key: str, headers=None):
    """One HTTP request through an ASGI app. Returns (status, headers, body)."""
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"authorization", b"Bearer " + key.encode("ascii")),
                         *(headers or ())]}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


# ---------------------------------------------------------------------------
# where admission runs
# ---------------------------------------------------------------------------


def test_a_throttled_request_never_reaches_the_app(tmp_path):
    """THE ONE THIS FILE RESTS ON: admission is above the whole SDK.

    A limiter written inside `guarded` refuses after the body is parsed, after
    the SDK has routed it, and after the request has queued for a concurrency
    slot. It also never sees `tools/list` or `kb://stats`, which cross no
    wrapper, or any of `ask`'s eight bare legs.

    Two assertions, and each rules out a different half:

    * the refusal is an HTTP **429**. A limiter inside `guarded` raises from
      inside a tool body, so the SDK wraps it as a JSON-RPC error inside a
      **200** and no proxy, client or agent can see a quota at all;
    * the request reaches the downstream app **zero** times. Counted on a stub,
      so it can tell "refused before anything ran" from "refused inside".

    BREAK-TEST RUN: moving admission into `guarded` (raising `GrantDenied` in
    place of the 429) makes this fail at `assert 200 == 429`, because a refusal
    below the gate cannot produce a status code.
    """
    ring = Ring(tmp_path / "keys.json")
    counting = _Counting()
    limiter = ratelimit.build_limiter(ring.keyring, _limits(rate="1/min",
                                                            burst="4"))
    gate = KeyAuthMiddleware(counting, None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(4):
            assert _drive(gate, key=ring.value)[0] == 200
        assert _drive(gate, key=ring.value)[0] == 429
    assert counting.calls == 4, (
        f"the throttled request reached the downstream app {counting.calls - 4} "
        "time(s), so it was refused below the gate")


def test_a_method_that_crosses_no_tool_wrapper_is_still_throttled(store,
                                                                  tmp_path):
    """`tools/list` and `kb://stats` cross no wrapper, so a limiter in
    `guarded` cannot see them at all.

    This is the half a counting stub cannot show: it drives the REAL app, over
    a real socket, and throttles a method that never reaches a tool body.
    `bounded_tool` registers `guarded` and `mcp.add_tool` is the only
    registration, so nothing in the catalogue path is wrapped.
    """
    import http.client

    from test_mcp_identity_propagates import bound_server

    ring = Ring(tmp_path / "keys.json")
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring,
                         limits=_limits(rate="1/hour", burst="4"))
    buf = io.StringIO()
    statuses = []
    with redirect_stderr(buf), bound_server(app) as hostport:
        host, port = hostport.split(":")
        for n in range(6):
            conn = http.client.HTTPConnection(host, int(port), timeout=30)
            try:
                headers = {"Host": hostport,
                           "Authorization": f"Bearer {ring.value}",
                           **_JSON_HEADERS}
                conn.request("POST", "/mcp", body=json.dumps(
                    {"jsonrpc": "2.0", "id": n + 1, "method": "tools/list",
                     "params": {}}), headers=headers)
                response = conn.getresponse()
                response.read()
                statuses.append(response.status)
            finally:
                conn.close()
    assert 429 in statuses, (
        f"tools/list was never throttled: {statuses}. It crosses no tool "
        "wrapper, so a limiter written inside `guarded` cannot reach it")


def test_an_unknown_key_gets_401_and_allocates_no_bucket(tmp_path):
    """401 BEFORE 429. A refused caller must never key the limiter's map.

    Admission ahead of identity would key a map on attacker-controlled input,
    which is the memory growth this whole design refuses.
    """
    ring = Ring(tmp_path / "keys.json")
    limiter = ratelimit.build_limiter(ring.keyring, _limits(rate="1/min",
                                                            burst="4"))
    gate = KeyAuthMiddleware(_Counting(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(20):
            key, _ = keys_mod.mint()
            assert _drive(gate, key=key)[0] == 401, "an unknown key got a 429"
    assert limiter._buckets == {}


def test_ten_thousand_forged_bearers_allocate_nothing(tmp_path):
    """The memory property, at the scale a flood actually reaches.

    BREAK-TEST RUN: keying the map on the presented bearer value instead of the
    resolved id makes this fail with 10,000 entries.
    """
    ring = Ring(tmp_path / "keys.json")
    limiter = ratelimit.build_limiter(ring.keyring, _limits(rate="60/min"))
    gate = KeyAuthMiddleware(_Counting(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(10_000):
            key, _ = keys_mod.mint()
            _drive(gate, key=key)
    assert len(limiter._buckets) == 0


def test_a_non_http_scope_is_never_admitted(tmp_path):
    """`lifespan` must pass straight through, empty bucket or not.

    Gating it leaves the SDK's session manager never started, which reads as a
    broken server rather than as a quota.
    """
    ring = Ring(tmp_path / "keys.json")
    limiter = ratelimit.build_limiter(ring.keyring, _limits(rate="1/min",
                                                            burst="4"))
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})

    gate = KeyAuthMiddleware(downstream, None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    # Drain the bucket first, so a gate that admitted lifespan scopes would have
    # refused this one.
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(6):
            _drive(gate, key=ring.value)

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(gate({"type": "lifespan"}, receive, send))
    assert reached[-1] == "lifespan", reached
    assert reached.count("lifespan") == 1


def test_an_unlimited_key_is_never_asked(tmp_path):
    """`--rate none` costs zero limiter calls; a limited key costs one each.

    Both halves. The first alone passes for a gate that never admits anybody.
    """
    ring = Ring(tmp_path / "keys.json", policy={"rate": "none"})
    calls: list = []

    class _Recording(ratelimit.Limiter):
        def admit(self, key_id):
            calls.append(key_id)
            return super().admit(key_id)

    limits = _limits(rate="60/min")
    limiter = _Recording(
        lambda key_id: ratelimit.resolve_limits(
            ring.keyring.policy_for(key_id), limits))
    gate = KeyAuthMiddleware(_Counting(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    for _ in range(20):
        assert _drive(gate, key=ring.value)[0] == 200
    # `admit` IS called (the gate does not know the key is unlimited), and it
    # allocates nothing, which is the property that bounds the map.
    assert len(calls) == 20
    assert limiter._buckets == {}, (
        "a key that opted out of every quota was still given two buckets")


def test_two_keys_do_not_share_a_quota(tmp_path):
    ring = Ring(tmp_path / "keys.json")
    second, second_value = keys_mod.create(ring.records, "bravo")
    keyfile.write_document(ring.path, [r.to_dict() for r in ring.records])
    keyring = keyfile.Keyring.load(ring.path)
    limiter = ratelimit.build_limiter(keyring, _limits(rate="1/min", burst="4"))
    gate = KeyAuthMiddleware(_Counting(), None, keyring=keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(4):
            assert _drive(gate, key=ring.value)[0] == 200
        assert _drive(gate, key=ring.value)[0] == 429
        assert _drive(gate, key=second_value)[0] == 200


# ---------------------------------------------------------------------------
# the 429 itself
# ---------------------------------------------------------------------------


def _throttled(tmp_path, *, defaults, drain=5):
    ring = Ring(tmp_path / "keys.json")
    limiter = ratelimit.build_limiter(ring.keyring, defaults)
    gate = KeyAuthMiddleware(_Counting(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(drain):
            result = _drive(gate, key=ring.value)
            if result[0] == 429:
                return result, buf.getvalue()
        result = _drive(gate, key=ring.value)
    return result, buf.getvalue()


def test_the_429_carries_the_three_fields_that_decide_whether_it_is_read(
        tmp_path):
    """content-type, content-length and Retry-After, each asserted alone.

    `content-type: application/json` EXACTLY is the field that decides whether
    the message reaches the model: the SDK client tests it before parsing
    anything, so `text/plain` shows the caller "Server returned an error
    response" and the limit is invisible. A body-only assertion passes with the
    header wrong, which is why it is its own criterion.
    """
    (status, headers, body), _ = _throttled(tmp_path,
                                            defaults=_limits(rate="1/min",
                                                             burst="4"))
    assert status == 429
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"content-length"] == str(len(body)).encode("ascii")
    assert re.fullmatch(rb"\d+", headers[b"retry-after"]), headers[b"retry-after"]
    assert int(headers[b"retry-after"]) >= 1


def test_retry_after_floors_at_one_and_never_renders_a_unit(tmp_path):
    """`int(0.2)` is 0, which tells a proxy to retry immediately.

    And `format_duration(120)` is "2m", which is the wrong function for a header
    that takes delta-seconds. Both directions, one test.
    """
    verdict = ratelimit.Admission(False, "requests", "1/sec", 0.2)
    _, headers, _ = _send(verdict)
    assert headers[b"retry-after"] == b"1"

    verdict = ratelimit.Admission(False, "cost", "30s/min", 119.2)
    _, headers, _ = _send(verdict)
    assert headers[b"retry-after"] == b"120", "a rendered unit reached the header"


def _send(verdict):
    sent: list = []

    async def send(message):
        sent.append(message)

    asyncio.run(server_mod._send_throttled(send, verdict))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def test_the_body_is_a_jsonrpc_error_with_a_null_id_and_no_unset_field():
    """The defect: a trailing `"data":null` from a dump without exclude_unset.

    Twelve extra bytes and a different content-length, on a body the client
    parses strictly.
    """
    from mcp.types import jsonrpc_message_adapter

    _, _, body = _send(ratelimit.Admission(False, "requests", "60/min", 3.0))
    assert b'"id":null' in body
    assert b'"data":null' not in body
    message = jsonrpc_message_adapter.validate_json(body, by_name=False)
    assert message.id is None
    assert message.error.code == -32000
    assert "60/min" in message.error.message


def test_each_bucket_gets_its_own_sentence():
    """An operator has to tell "too many calls" from "too much time".

    They are fixed by different flags, and one sentence covering both sends
    them to the wrong one.
    """
    _, _, requests = _send(ratelimit.Admission(False, "requests", "60/min", 12.0))
    _, _, cost = _send(ratelimit.Admission(False, "cost", "30s/min", 16.0))
    assert b"rate limit exceeded for this key: 60/min. retry in 12s" in requests
    assert b"compute budget exceeded for this key: 30s/min. retry in 16s" in cost


def test_the_429_carries_no_draft_ratelimit_headers():
    """`RateLimit` / `RateLimit-Policy` are an Internet-Draft.

    Emitting them pins contextlake to a spelling that is still moving.
    """
    _, headers, _ = _send(ratelimit.Admission(False, "requests", "60/min", 1.0))
    assert not [name for name in headers if b"ratelimit" in name.lower()]


def test_the_wire_carries_nothing_about_the_presented_key(tmp_path):
    """Scanned on the WIRE BYTES, not on the limiter's return value."""
    ring = Ring(tmp_path / "keys.json")
    limiter = ratelimit.build_limiter(ring.keyring, _limits(rate="1/min",
                                                            burst="4"))
    gate = KeyAuthMiddleware(_Counting(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(5):
            status, _, body = _drive(gate, key=ring.value)
    assert status == 429
    assert b"ctxlake_" not in body
    assert ring.value.encode("ascii") not in body
    assert ring.record.id.encode("ascii") not in body


def test_the_401_body_is_untouched_by_this_changeset():
    """The 429 is a new response, not a rewritten 401.

    Byte-identical against the module constant, which is NOT
    `{"error":"unauthorized"}` any more: it carries the no-OAuth-here detail a
    client needs.
    """
    sent: list = []

    async def send(message):
        sent.append(message)

    asyncio.run(server_mod._send_unauthorized(send))
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    assert body == server_mod._UNAUTHORIZED_BODY
    assert b"There is no OAuth flow here" in body


# ---------------------------------------------------------------------------
# the operator line
# ---------------------------------------------------------------------------


def test_a_throttle_is_not_one_of_the_seven_401_classes():
    """The defect: widening `REFUSAL_CLASSES` to fit a 429 into it.

    Those seven are byte-identical on the wire on purpose, and four analytics
    stories enumerate the tuple. A throttle gets its own vocabulary and its own
    bounded log instance.
    """
    assert len(REFUSAL_CLASSES) == 7
    assert "throttled" not in REFUSAL_CLASSES
    with pytest.raises(ValueError, match="not a refusal class"):
        server_mod._report_refusal("throttled")
    with pytest.raises(ValueError, match="not a throttle class"):
        server_mod._report_throttle("k_a", "elsewhere")


def test_the_throttle_line_goes_to_stderr_and_names_the_key(capsys):
    """stderr, following `_report_refusal`, which says so in its docstring.

    A stdout capture here would pass over a stream nothing was written to,
    which is the vacuous form this assertion has to avoid.
    """
    server_mod._report_throttle("k_abc123", "requests")
    captured = capsys.readouterr()
    assert "MCP rate limit: requests key=k_abc123" in captured.err, captured
    assert "k_abc123" not in captured.out


def test_the_throttle_line_is_bounded_the_way_the_refusal_line_is(capsys):
    """The defect: an unbounded print on the one path a flood exercises.

    A throttled caller is by definition sending a lot, so a line per refused
    request fills a `--log-file` disk.
    """
    for _ in range(100):
        server_mod._report_throttle("k_a", "requests")
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) <= server_mod.REFUSAL_LOG_CAP + 1, len(lines)


# ---------------------------------------------------------------------------
# derivation and validation
# ---------------------------------------------------------------------------


def test_build_http_app_derives_the_limiter_and_takes_no_parameter_for_it(
        store, tmp_path, monkeypatch):
    """The defect a `limiter=` parameter would have: a call site that forgets it.

    Two production call sites reach `build_http_app`, and one without the
    argument would be a socket serving unlimited traffic with every test still
    green. `build_limiter` is the one seam a test patches instead.
    """
    import inspect

    assert "limiter" not in inspect.signature(build_http_app).parameters

    built: list = []
    real = ratelimit.build_limiter
    monkeypatch.setattr(ratelimit, "build_limiter",
                        lambda *a, **k: (built.append(a), real(*a, **k))[1])
    ring = Ring(tmp_path / "keys.json")
    build_http_app(store, transport="streamable-http", host="127.0.0.1",
                   token=None, keyring=ring.keyring, limits=_limits(rate="60/min"))
    assert len(built) == 1


def test_a_key_file_with_an_unparseable_rate_is_refused_at_load(tmp_path):
    """A server that starts on that file is a key that reads as limited.

    `kb keys` still RENDERS such a record: an operator has to be able to see the
    value they are correcting.
    """
    ring = Ring(tmp_path / "keys.json")
    ring.record.policy["rate"] = "60"
    keyfile.write_document(ring.path, [r.to_dict() for r in ring.records])
    with pytest.raises(keyfile.KeyFileError, match="60"):
        keyfile.Keyring.load(ring.path)


def test_a_bad_rate_introduced_by_a_live_edit_keeps_the_previous_keyring(
        tmp_path):
    """The defect: a hand-edited typo taking down every live key at once.

    The previous file is the last state an operator successfully validated, so
    a rejected reload keeps serving it and warns once.
    """
    warnings: list = []
    ring = Ring(tmp_path / "keys.json")
    keyring = keyfile.Keyring.load(ring.path, warn=warnings.append)
    assert keyring.resolve(ring.value) is not None

    ring.record.policy["rate"] = "not-a-rate"
    keyfile.write_document(ring.path, [r.to_dict() for r in ring.records])
    keyring.reload_if_changed()
    assert warnings and "not-a-rate" in warnings[0], warnings
    # THE HALF THAT MATTERS: the key that was live before the bad edit still is.
    assert keyring.resolve(ring.value) is not None, (
        "a typo in one record locked out every key on a running server")


def test_a_typo_in_the_serve_table_is_warned_about(tmp_path, monkeypatch, caplog):
    """The defect: `default_rat = "60/min"` silently leaving every key unlimited.

    `[serve]` keys were never checked the way `[kb]` keys are, and this table
    now carries four of them.
    """
    from contextlake.kb import config as kb_config

    config = tmp_path / "kb.toml"
    config.write_text('[serve]\ndefault_rat = "60/min"\n')
    messages: list = []
    monkeypatch.setattr(kb_config, "log",
                        lambda message, **kw: messages.append(message))
    kb_config._warn_unknown_config({}, {"serve": {"default_rat": "60/min"}})
    assert any("default_rat" in m for m in messages), messages
    # The positive control: a known key produces no line, so the assertion above
    # is not passing on a warning that fires for everything.
    messages.clear()
    kb_config._warn_unknown_config({}, {"serve": {"default_rate": "60/min"}})
    assert not messages, messages


def test_a_config_found_by_walking_up_cannot_set_a_quota(tmp_path, monkeypatch):
    """A rate limit a repository checkout can rewrite is not a limit.

    Same provenance gate `[serve] keys_file` already goes through.
    """
    from contextlake.kb import config as kb_config

    local = tmp_path / ".contextlake.kb.toml"
    local.write_text('[serve]\ndefault_rate = "1/hour"\n')
    monkeypatch.setattr(kb_config, "find_ancestor_config", lambda name: str(local))
    monkeypatch.setattr(kb_config, "GLOBAL_CONFIG", str(tmp_path / "absent.toml"))
    warned: list = []
    table = keyfile.trusted_serve_table(None, warned.append)
    assert table == {}, table
    assert warned and "default_rate" in warned[0], warned


# ---------------------------------------------------------------------------
# the charge inside the slot
# ---------------------------------------------------------------------------


class _Recorder:
    """A limiter that records instead of limiting. Admits everything."""

    def __init__(self):
        self.charges: list[tuple[str, float]] = []

    def admit(self, key_id):
        return ratelimit.Admission(True)

    def charge(self, key_id, ms):
        self.charges.append((key_id, ms))


def _wire_call(app, key: str, tool: str, arguments=None, request_id=2):
    import http.client

    from test_mcp_identity_propagates import bound_server

    with bound_server(app) as hostport:
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            headers = {"Host": hostport, "Authorization": f"Bearer {key}",
                       **_JSON_HEADERS}
            conn.request("POST", "/mcp", body=json.dumps(_INITIALIZE),
                         headers=headers)
            conn.getresponse().read()
            conn.request("POST", "/mcp", body=json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                 "params": {"name": tool, "arguments": arguments or {}}}),
                headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


def test_every_wire_tool_call_is_charged_once(store, tmp_path, monkeypatch):
    """Ten calls, ten charges, and the key id on every one of them."""
    ring = Ring(tmp_path / "keys.json")
    recorder = _Recorder()
    monkeypatch.setattr(ratelimit, "build_limiter", lambda *a, **k: recorder)
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    import http.client

    from test_mcp_identity_propagates import bound_server

    with bound_server(app) as hostport:
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            headers = {"Host": hostport,
                       "Authorization": f"Bearer {ring.value}", **_JSON_HEADERS}
            conn.request("POST", "/mcp", body=json.dumps(_INITIALIZE),
                         headers=headers)
            conn.getresponse().read()
            for n in range(10):
                conn.request("POST", "/mcp", body=json.dumps(
                    {"jsonrpc": "2.0", "id": n + 2, "method": "tools/call",
                     "params": {"name": "graph_stats", "arguments": {}}}),
                    headers=headers)
                conn.getresponse().read()
        finally:
            conn.close()
    assert len(recorder.charges) == 10, recorder.charges
    assert {key_id for key_id, _ in recorder.charges} == {ring.record.id}


def test_stdio_reads_no_context_var_and_charges_nothing(store, monkeypatch):
    """The switch is decided at BUILD TIME, never by a ContextVar read.

    `current_principal` is patched to raise, so a charge gated on a ContextVar
    read cannot pass. `limiter=None` on this path means `_charging` is False and
    the timer never opens.
    """
    monkeypatch.setattr(server_mod, "current_principal",
                        lambda: (_ for _ in ()).throw(AssertionError("read")))
    server = build_server(store)
    # The REGISTERED wrapper, which is `guarded`. `get_tool(...).fn` is what
    # `test_serve_concurrency.py` already reaches for, and it is the only handle
    # on the wrapper from outside the closure.
    guarded = server._tool_manager.get_tool("graph_stats").fn
    for _ in range(10):
        guarded()


def test_a_tool_that_raises_is_still_charged(store, tmp_path, monkeypatch):
    """The defect: a key spending unlimited compute by failing.

    The charge is in a `finally` for exactly this. Without it, a tool that took
    fifty milliseconds and then raised is free.
    """
    ring = Ring(tmp_path / "keys.json")
    recorder = _Recorder()
    monkeypatch.setattr(ratelimit, "build_limiter", lambda *a, **k: recorder)

    def _boom(self):
        time.sleep(0.05)
        raise RuntimeError("from inside the body")

    monkeypatch.setattr(SqliteStore, "stats", _boom)
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring)
    _wire_call(app, ring.value, "graph_stats")
    assert recorder.charges, "a tool that raised was never charged"
    assert recorder.charges[0][1] >= 45.0, recorder.charges


def test_a_slot_wait_is_not_billed_to_the_caller_that_waited(store, tmp_path,
                                                             monkeypatch):
    """The defect the ANCHOR comment exists to prevent: cross-key billing.

    `tool_concurrency=1` and a patched store method, so thread B genuinely waits
    for thread A's slot. Timed outside the `with`, B's charge is about 650 ms
    against the roughly 100 ms it actually spent.

    The STORE METHOD is patched, not `guarded`: `guarded` is closure-local, and
    a hand-written stub wrapper is not the code under test.
    """
    ring = Ring(tmp_path / "keys.json")
    recorder = _Recorder()
    monkeypatch.setattr(ratelimit, "build_limiter", lambda *a, **k: recorder)

    real_stats = SqliteStore.stats
    sleeps = {"n": 0}

    def _slow(self):
        # First caller in is the slow one; the second is the cheap tool that
        # queues behind it.
        sleeps["n"] += 1
        time.sleep(0.60 if sleeps["n"] == 1 else 0.10)
        return real_stats(self)

    monkeypatch.setattr(SqliteStore, "stats", _slow)
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, tool_concurrency=1)

    import http.client

    from test_mcp_identity_propagates import bound_server

    def _call(hostport, request_id):
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=60)
        try:
            headers = {"Host": hostport,
                       "Authorization": f"Bearer {ring.value}", **_JSON_HEADERS}
            conn.request("POST", "/mcp", body=json.dumps(_INITIALIZE),
                         headers=headers)
            conn.getresponse().read()
            conn.request("POST", "/mcp", body=json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                 "params": {"name": "graph_stats", "arguments": {}}}),
                headers=headers)
            conn.getresponse().read()
        finally:
            conn.close()

    with bound_server(app) as hostport:
        first = threading.Thread(target=_call, args=(hostport, 2))
        first.start()
        time.sleep(0.05)
        second = threading.Thread(target=_call, args=(hostport, 3))
        second.start()
        first.join(timeout=60)
        second.join(timeout=60)

    charges = sorted(ms for _, ms in recorder.charges)
    assert len(charges) == 2, recorder.charges
    cheap = charges[0]
    assert 90.0 <= cheap <= 400.0, (
        f"the cheap tool was charged {cheap:.0f} ms. It spent about 100 ms and "
        f"waited about 550 ms for the other caller's slot; timed outside the "
        f"`with` it comes out near 650 ms. Charges: {charges}")


def test_bounded_tool_still_returns_the_bare_function(store):
    """The defect: wrapping the return value, which deadlocks `ask` at limit 1.

    `bounded_tool` registers `guarded` and returns `fn`, so `ask` reaches its
    siblings below the wrapper and cannot queue against a slot it already holds.
    """
    server = build_server(store)
    tool = server._tool_manager.get_tool("ask")
    assert tool is not None
    source = pathlib.Path(server_mod.__file__).read_text(encoding="utf-8")
    assert "        mcp.add_tool(guarded)\n        return fn\n" in source


def test_the_charge_and_the_row_share_one_elapsed_value():
    """The defect: a second timer for `ms`, measuring a different window.

    The charge and the usage row must be two readers of ONE measurement. Two
    timers put two numbers on one call, so a key billed 40 ms would be reported
    at 38 ms and nobody could say which was the call.

    `_timed` is what makes that structural: the gate is `_charging or
    _recording`, so `t0` is taken once and `elapsed` is computed once.

    Read off the FILE, not through `inspect.getsource(guarded)`: that is
    closure-local and `functools.wraps` copies `__wrapped__`, so `getsource` can
    hand back the undecorated body and pass whatever the code does.
    """
    source = pathlib.Path(server_mod.__file__).read_text(encoding="utf-8")
    code = [line for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert len([line for line in code if "t0 = _now()" in line]) == 1
    assert len([line for line in code if "elapsed = (_now() - t0)" in line]) == 1
    assert len([line for line in code if "ms = round(elapsed)" in line]) == 1
    assert source.count("outcome, ms, principal = ") == 1
    # The charge still reads `_charging`, not `_timed`: a server that records
    # and does not limit measures the call and bills nobody.
    assert "                            if _charging:" + chr(10) in source


def test_stdio_never_loads_the_rate_limiter(tmp_path):
    """Local-first property P1, in a SUBPROCESS.

    In-process this is decided by whatever the rest of the session imported: the
    tests above patch `ratelimit.build_limiter`, which loads the module. A fresh
    interpreter is the only place the question can be asked.
    """
    code = (
        "import sys;"
        "from contextlake.kb.server import build_server;"
        "from contextlake.kb.store.sqlite_store import SqliteStore;"
        f"s = SqliteStore(r'{tmp_path / 'kb.sqlite'}');"
        "build_server(s); s.close();"
        "print('rl' if 'contextlake.kb.ratelimit' in sys.modules else '-')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "-", (
        "building an stdio server loaded the rate limiter; it must stay behind "
        "build_http_app")
