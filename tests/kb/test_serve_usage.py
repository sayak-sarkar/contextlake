"""Usage rows at the wrapper anchor, at the gate, and on lifespan shutdown.

Three properties this file exists to hold:

1. **The row is written in the outer `finally`, not beside the `return`.** That
   is what records a call refused before the tool body ran and a call that
   raised. `test_a_raising_tool_writes_an_error_row` and
   `test_a_lost_principal_writes_an_identity_unset_row` are the two that can
   see it moved.
2. **The gate writes the rows for traffic that never reached a tool**, after
   the response has gone out. A refused request crosses no wrapper, so nothing
   else can.
3. **stdio pays nothing.** `test_stdio_reads_no_context_var_and_builds_no_recorder`
   patches both the ContextVar read and the recorder to RAISE, so a build that
   reaches either fails rather than being counted at zero by a mock wired to
   nothing.

The end-to-end tests bind a REAL SOCKET, following `test_serve_ratelimit.py`:
`starlette.testclient.TestClient` runs one portal and hangs on this server's
long-lived streams.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from contextlib import redirect_stderr

import pytest

from contextlake.kb import keyfile, usage
from contextlake.kb import keys as keys_mod
from contextlake.kb import server as server_mod
from contextlake.kb.model import Node
from contextlake.kb.server import KeyAuthMiddleware, build_http_app, build_server
from contextlake.kb.store.sqlite_store import SqliteStore

SHARED = "shared-token-for-a-test"  # noqa: S105 - a test value, not a credential


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
    server_mod._reset_identity_fault_log()
    yield
    server_mod.reset_refusal_log()
    server_mod._reset_identity_fault_log()


@pytest.fixture
def recorder(tmp_path):
    return usage.Recorder(tmp_path / "usage" / usage.FILENAME,
                          stream=io.StringIO())


class Ring:
    """One real key file with one real live key, plus the real `Keyring`."""

    def __init__(self, path, names=("alpha",)):
        self.path = path
        self.records: list = []
        self.values = []
        for name in names:
            _record, value = keys_mod.create(self.records, name)
            self.values.append(value)
        keyfile.write_document(path, [r.to_dict() for r in self.records])
        self.keyring = keyfile.Keyring.load(path)

    @property
    def value(self):
        return self.values[0]


def _drive(app, *, authorization: bytes | None = None):
    """One HTTP request through an ASGI app. Returns (status, body)."""
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [] if authorization is None else [(b"authorization", authorization)]
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    return start["status"], body


def _rows(rec):
    rec.flush()
    return usage.read_rows(rec.path)


# ---------------------------------------------------------------------------
# the anchor: which outcome, and where the row is written
# ---------------------------------------------------------------------------


def test_a_wire_tool_call_writes_one_ok_row(store, tmp_path, recorder, monkeypatch):
    """No row at all, or one per HTTP request instead of one per tool call."""
    ring = Ring(tmp_path / "keys.json")
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, usage=recorder)
    _wire_call(app, ring.value, "graph_stats")

    every = _rows(recorder)
    assert [r["outcome"] for r in every] == ["ok"]
    assert every[0]["tool"] == "graph_stats"
    assert every[0]["key"] == ring.records[0].id
    assert every[0]["n"] == 1
    # ONE row for TWO HTTP requests. `_wire_call` sends `initialize` and then
    # `tools/call`, and the handshake crosses no wrapper, so `ok` counts tool
    # calls and never requests. Asserting over EVERY row is what sees that: a
    # filter on `outcome == "ok"` would hide a second row of any other kind.
    assert len(every) == 1


def test_a_raising_tool_writes_an_error_row(store, tmp_path, recorder, monkeypatch):
    """The defect: the row written beside the `return` instead of in the outer
    `finally`. A tool that raises then produces NO row, and the one number an
    operator uses to find a broken client is missing exactly when it matters.
    """
    ring = Ring(tmp_path / "keys.json")

    def _boom(self):
        raise RuntimeError("from inside the body")

    monkeypatch.setattr(SqliteStore, "stats", _boom)
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, usage=recorder)
    _wire_call(app, ring.value, "graph_stats")

    rows = [r for r in _rows(recorder) if r["tool"] == "graph_stats"]
    assert [r["outcome"] for r in rows] == ["error"]
    # It reached the body, so it took a slot and spent real time.
    assert isinstance(rows[0]["ms"], int)


def test_a_token_only_server_times_its_calls_and_files_them_under_shared_token(
        store, recorder):
    """THE CASE `_timed` EXISTS FOR, and the only one that can see it.

    `build_limiter` returns None when there is no keyring and no `[serve]`
    default, so `_charging` is False on a token-only server. With the timer
    gated on `_charging` alone the row carries no duration and P50/P95 read `-`
    forever on the most exposed configuration contextlake ships. Every keyring
    server derives a limiter, so `_timed` collapsed to `_charging` passes every
    other test in this file.

    It also pins the second half: a shared token has no key record, and its
    traffic is filed under the one reserved id rather than dropped.
    """
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=SHARED, usage=recorder)
    _wire_call(app, SHARED, "graph_stats")

    rows = [r for r in _rows(recorder) if r["outcome"] == "ok"]
    assert len(rows) == 1, rows
    assert isinstance(rows[0]["ms"], int), (
        "a token-only server recorded a call with no duration, so the timer is "
        "gated on the charge rather than on the measurement")
    assert rows[0]["key"] == server_mod.SHARED_TOKEN_KEY_ID


def test_a_denied_call_writes_a_denied_row_with_no_duration(store, tmp_path,
                                                            recorder):
    """A refusal above the slot carries no `ms`, and no code decides that: the
    timer opens inside the `with`, below the grant check.

    `ms: 0` here instead of null would drag every latency figure down.
    """
    ring = Ring(tmp_path / "keys.json")
    ring.records[0].policy["tools"] = "none"
    keyfile.write_document(ring.path, [r.to_dict() for r in ring.records])
    keyring = keyfile.Keyring.load(ring.path)

    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=keyring, usage=recorder)
    _wire_call(app, ring.value, "graph_stats")

    rows = [r for r in _rows(recorder) if r["tool"] == "graph_stats"]
    assert [r["outcome"] for r in rows] == ["denied"]
    assert rows[0]["ms"] is None


def test_a_lost_principal_writes_an_identity_unset_row(store, tmp_path,
                                                       recorder, monkeypatch):
    """The fail-closed fault recorded as nothing at all.

    `identity_unset` dropped from the vocabulary would make every one of these
    calls invisible: the server refuses every tool and the file says the server
    is idle.
    """
    ring = Ring(tmp_path / "keys.json")
    monkeypatch.setattr(server_mod, "current_principal", lambda: None)
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, usage=recorder)
    buf = io.StringIO()
    with redirect_stderr(buf):
        _wire_call(app, ring.value, "graph_stats")

    rows = [r for r in _rows(recorder) if r["outcome"] == "identity_unset"]
    assert len(rows) == 1
    assert rows[0]["key"] is None and rows[0]["ms"] is None


def test_two_keys_produce_two_different_key_ids(store, tmp_path, recorder):
    """A hard-coded or shared id. Inequality, not "two rows": two rows carrying
    one id passes a count assertion and attributes both callers to one key."""
    ring = Ring(tmp_path / "keys.json", names=("alpha", "beta"))
    for value in ring.values:
        # A FRESH app per call: `StreamableHTTPSessionManager.run()` refuses a
        # second bind on one instance. The recorder is the same object, which
        # is what makes the two rows comparable.
        app = build_http_app(store, transport="streamable-http",
                             host="127.0.0.1", token=None,
                             keyring=ring.keyring, usage=recorder)
        _wire_call(app, value, "graph_stats")

    ids = {r["key"] for r in _rows(recorder) if r["outcome"] == "ok"}
    assert len(ids) == 2
    assert ids == {ring.records[0].id, ring.records[1].id}


def test_a_broken_recorder_does_not_change_the_tool_output(store, tmp_path):
    """The blast radius of a raise inside the wrapper's outer `finally`.

    THE NEVER-RAISE CONTRACT LIVES IN `Recorder.record`, not in a guard at the
    anchor, mirroring the charge above it ("the never-raise contract lives
    inside `Limiter.charge`"). So the real object is used here, broken two
    ways at once: an unwritable path and a clock that raises. A hand-written
    stub that raises would be testing a guard this design deliberately does
    not have; `test_record_never_raises` is where the contract itself is
    pinned.

    BODIES are compared, never status codes: a raising tool already returns
    HTTP 200 with an `is_error` result, so a status assertion passes over the
    exact defect.
    """
    ring = Ring(tmp_path / "keys.json")

    def _clock():
        raise RuntimeError("clock")

    def _bodies(**kwargs):
        # A FRESH app per call: `StreamableHTTPSessionManager.run()` refuses a
        # second bind on one instance.
        out = []
        for _ in range(5):
            app = build_http_app(store, transport="streamable-http",
                                 host="127.0.0.1", token=None,
                                 keyring=ring.keyring, **kwargs)
            out.append(_wire_call(app, ring.value, "graph_stats")[1])
        return out

    broken = usage.Recorder("/proc/definitely/not/writable/usage.jsonl",
                            wall=_clock, stream=io.StringIO())
    assert _bodies() == _bodies(usage=broken)
    assert usage.read_rows(broken.path) == []


def test_stdio_reads_no_context_var_and_builds_no_recorder(store, monkeypatch):
    """The local path paying for the network path.

    BOTH seams raise: the ContextVar read and the recorder's own constructor.
    A zero-call assertion is what a mock wired to nothing reports, and this
    fails instead.
    """
    monkeypatch.setattr(server_mod, "current_principal",
                        lambda: (_ for _ in ()).throw(AssertionError("read")))
    monkeypatch.setattr(usage, "Recorder",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("built")))
    server = build_server(store)
    guarded = server._tool_manager.get_tool("graph_stats").fn
    for _ in range(10):
        guarded()


def test_build_server_refuses_a_recorder_on_a_local_transport(store):
    """`usage` is a network-only control, and the refusal is what lets the row
    read `principal.key_id` with no None test."""
    with pytest.raises(ValueError, match="usage"):
        build_server(store, networked=False, usage=object())


# ---------------------------------------------------------------------------
# the gate's rows
# ---------------------------------------------------------------------------


def test_a_refusal_row_is_written_after_the_401(tmp_path):
    """The dropped connection, in ORDER.

    `_report_refusal` already sits after the response because a raise on the
    way IN unwound the request with no response at all: the caller got a closed
    socket instead of a 401, and a client that retries a dropped connection
    retries forever. The usage row inherits that rule, and the only way to see
    it is the sequence, not the contents.

    One log receives both the ASGI sends and the record call, so a row written
    first reads as `["record", "start", "body"]`.
    """
    ring = Ring(tmp_path / "keys.json")
    order: list = []

    class _Watching:
        def record(self, **kwargs):
            order.append("record")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        order.append(message["type"].rsplit(".", 1)[-1])

    gate = KeyAuthMiddleware(_ok_app(), None, keyring=ring.keyring,
                             keys=keys_mod, usage=_Watching())
    buf = io.StringIO()
    with redirect_stderr(buf):
        asyncio.run(gate({"type": "http", "method": "POST", "path": "/mcp",
                          "headers": []}, receive, send))
    assert order == ["start", "body", "record"], order


def test_refusal_rows_are_counted_not_kept_per_request(tmp_path, recorder):
    """500 rows for one scanner. At a thousand requests a second an
    unauthenticated flood fills a 20,000-row file in twenty seconds and evicts
    every real row."""
    ring = Ring(tmp_path / "keys.json")
    gate = KeyAuthMiddleware(_ok_app(), None, keyring=ring.keyring,
                             keys=keys_mod, usage=recorder)
    buf = io.StringIO()
    with redirect_stderr(buf):
        for _ in range(500):
            assert _drive(gate, authorization=None)[0] == 401

    rows = _rows(recorder)
    assert len(rows) == 1, rows
    assert rows[0] == {"ts": rows[0]["ts"], "key": None, "tool": None,
                       "outcome": "no_header", "ms": None, "n": 500}


def test_a_refusal_row_holds_nothing_about_the_presented_value(tmp_path, recorder):
    """A prefix, a length or a digest of a presented credential in a file that
    outlives the process.

    The positive control pushes a LEGAL row carrying the needle through the
    same writer, so a zero above is the scan working rather than the file being
    empty.
    """
    ring = Ring(tmp_path / "keys.json")
    presented = "ctxlake_" + "z" * 49
    gate = KeyAuthMiddleware(_ok_app(), None, keyring=ring.keyring,
                             keys=keys_mod, usage=recorder)
    buf = io.StringIO()
    with redirect_stderr(buf):
        _drive(gate, authorization=b"Bearer " + presented.encode("ascii"))

    text = "".join(json.dumps(row) for row in _rows(recorder))
    for needle in (presented, presented[:12], presented[8:20], str(len(presented))):
        assert needle not in text, needle

    recorder.record(tool=presented, outcome="ok", key="k_1", ms=1)
    assert presented in "".join(json.dumps(r) for r in _rows(recorder))


def test_a_throttled_request_writes_a_throttled_row(tmp_path, recorder):
    """A quota refusal invisible in the record. THR is the column an operator
    reads before raising a limit, and admission happens above every wrapper, so
    the gate is the only place this row can come from."""
    from contextlake.kb import ratelimit

    ring = Ring(tmp_path / "keys.json")
    limits = ratelimit.ServeDefaults(rate=ratelimit.parse_rate("1/min"),
                                     burst=ratelimit.burst_number("4"))
    limiter = ratelimit.build_limiter(ring.keyring, limits)
    gate = KeyAuthMiddleware(_ok_app(), None, keyring=ring.keyring,
                             keys=keys_mod, limiter=limiter, usage=recorder)
    buf = io.StringIO()
    key = ("Bearer " + ring.value).encode("ascii")
    with redirect_stderr(buf):
        for _ in range(4):
            assert _drive(gate, authorization=key)[0] == 200
        for _ in range(3):
            assert _drive(gate, authorization=key)[0] == 429

    throttled = [r for r in _rows(recorder) if r["outcome"] == "throttled"]
    assert len(throttled) == 1
    assert throttled[0]["n"] == 3
    # The key IS known here: the gate resolved an identity and then refused it,
    # which is what separates a throttle from the seven 401 classes.
    assert throttled[0]["key"] == ring.records[0].id


# ---------------------------------------------------------------------------
# the lifespan
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_flushes_the_buffer(store, tmp_path, recorder):
    """The lost last minutes. `cmd_serve`'s `finally` ends in `os._exit(0)`,
    which skips every `atexit` hook, so a shutdown hook is the only place this
    can happen."""
    ring = Ring(tmp_path / "keys.json")
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, usage=recorder)
    recorder.record(tool="graph_stats", outcome="ok", key="k_1", ms=4)
    assert usage.read_rows(recorder.path) == [], "it wrote before the flush"

    _drive_lifespan(app)
    assert len(usage.read_rows(recorder.path)) == 1


def test_lifespan_startup_still_applies_the_tool_limiter(store, tmp_path,
                                                         recorder, monkeypatch):
    """The shutdown branch breaking the startup one. They live in one class now
    and the rename is what makes that visible, so both are asserted."""
    applied = []
    monkeypatch.setattr(server_mod, "apply_tool_limiter", applied.append)
    ring = Ring(tmp_path / "keys.json")
    app = build_http_app(store, transport="streamable-http", host="127.0.0.1",
                         token=None, keyring=ring.keyring, usage=recorder,
                         tool_concurrency=3)
    _drive_lifespan(app)
    assert applied == [3]


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


def _serve_args(tmp_path, extra=""):
    from types import SimpleNamespace

    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n\n'
                   f'[embeddings]\nenabled = false\n{extra}')
    return SimpleNamespace(config=str(cfg), transport="http", host=None, port=None)


def _run_serve(args, monkeypatch, keys_file):
    """`cmd_serve` up to the socket, with `run_server` captured.

    `keys_file=None` means "absent at a path NOBODY NAMED", which is the only
    absent state that may mint a shared token. Reaching it with a named path
    that is not there is a refusal, not a first start.
    """
    from contextlake.kb import config as kb_config
    from contextlake.kb.cmds import serve as serve_cmd

    captured = {}

    def _fake_run_server(store, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server_mod, "run_server", _fake_run_server)
    monkeypatch.delenv("CONTEXTLAKE_MCP_TOKEN", raising=False)
    if keys_file is None:
        # Both tiers above the default are cut, or this reads the developer's
        # own config and environment and means something else on another
        # machine.
        monkeypatch.setattr(kb_config, "GLOBAL_CONFIG",
                            str(args.config) + ".absent")
        monkeypatch.setattr(keyfile, "default_keys_file",
                            lambda: keyfile.Path(str(args.config) + ".keys"))
        monkeypatch.delenv(keyfile.KEYS_FILE_ENV, raising=False)
    else:
        monkeypatch.setenv(keyfile.KEYS_FILE_ENV, str(keys_file))
    return serve_cmd.cmd_serve(args), captured


@pytest.mark.parametrize("how", ["stdio", "--no-usage", "[serve] usage = false"])
def test_no_recorder_is_built_when_recording_is_off(tmp_path, monkeypatch, how,
                                                    capsys):
    """Three off-states. The file accessor is counted as well as the file
    checked, so a build that constructs a recorder and never writes still
    fails."""
    built = []
    monkeypatch.setattr(usage, "Recorder",
                        lambda *a, **k: built.append(a) or object())
    keys_path, _ = _key_file(tmp_path)
    args = _serve_args(tmp_path, "" if how != "[serve] usage = false"
                       else "\n[serve]\nusage = false\n")
    if how == "stdio":
        args.transport = "stdio"
    if how == "--no-usage":
        args.no_usage = True

    rc, captured = _run_serve(args, monkeypatch, keys_path)
    capsys.readouterr()
    assert rc == 0
    assert built == []
    assert captured.get("usage") is None
    assert not os.path.exists(usage.usage_path(tmp_path / "kb"))


def test_a_network_start_with_no_key_file_still_records(tmp_path, monkeypatch,
                                                        capsys):
    """THE POSITIVE CONTROL for the three off-states above. Without it they all
    pass on a build that never records anything.

    A token-only server files its traffic under the reserved shared-token id,
    and that is traffic an operator needs to see, so the switch is the
    TRANSPORT and not whether a key file exists.
    """
    args = _serve_args(tmp_path)
    rc, captured = _run_serve(args, monkeypatch, None)
    err = capsys.readouterr().err
    assert rc == 0
    assert isinstance(captured.get("usage"), usage.Recorder)
    assert "Recording usage to" in err


def test_the_first_run_banner_prints_once(tmp_path, monkeypatch, capsys):
    """A line on every start is a line an operator stops reading. BOTH counts
    are asserted, because a banner that never prints also passes a `<= 1`."""
    keys_path, _ = _key_file(tmp_path)
    args = _serve_args(tmp_path)
    _run_serve(args, monkeypatch, keys_path)
    first = capsys.readouterr().err
    assert first.count("Recording usage to") == 1
    assert "--no-usage" in first

    # The file the banner announced now exists, so the next start says nothing.
    path = usage.usage_path(tmp_path / "kb")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8"):
        pass
    _run_serve(_serve_args(tmp_path), monkeypatch, keys_path)
    assert capsys.readouterr().err.count("Recording usage to") == 0


def test_serve_usage_keys_are_read_from_a_privileged_config_only(tmp_path,
                                                                 monkeypatch,
                                                                 capsys):
    """A `.contextlake.kb.toml` found by walking up from the cwd sits inside a
    repository checkout, so a retention cap it sets is a cap whatever is
    checked out can rewrite.

    Same gate `[serve] keys_file` and the quota defaults already go through.
    """
    keys_path, _ = _key_file(tmp_path)
    work = tmp_path / "checkout"
    work.mkdir()
    (work / ".contextlake.kb.toml").write_text("[serve]\nusage = false\n")
    monkeypatch.chdir(work)

    args = _serve_args(tmp_path)
    rc, captured = _run_serve(args, monkeypatch, keys_path)
    err = capsys.readouterr().err
    assert rc == 0
    assert isinstance(captured.get("usage"), usage.Recorder), (
        "a config found by walking up from the cwd turned recording off")
    assert "IGNORED [serve] usage" in err

    # The same key in the file the operator NAMED is honoured.
    args = _serve_args(tmp_path, "\n[serve]\nusage = false\n")
    rc, captured = _run_serve(args, monkeypatch, keys_path)
    capsys.readouterr()
    assert rc == 0 and captured.get("usage") is None


def test_an_unreadable_serve_usage_value_refuses_the_start(tmp_path, monkeypatch,
                                                           capsys):
    """A cap dropped in silence leaves the file growing while an operator
    believes it is bounded."""
    keys_path, _ = _key_file(tmp_path)
    args = _serve_args(tmp_path, '\n[serve]\nusage_max_lines = "lots"\n')
    rc, _captured = _run_serve(args, monkeypatch, keys_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "usage_max_lines" in err and "lots" in err


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _key_file(tmp_path):
    records = []
    _record, value = keys_mod.create(records, "alpha")
    path = tmp_path / "mcp-keys.json"
    keyfile.write_document(path, [r.to_dict() for r in records])
    return path, value


def _ok_app():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _drive_lifespan(app):
    """One full ASGI lifespan: startup, then shutdown."""
    messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return messages.pop(0) if messages else {"type": "lifespan.shutdown"}

    async def send(message):
        return None

    asyncio.run(app({"type": "lifespan"}, receive, send))


def _wire_call(app, key: str, tool: str, arguments=None, request_id=2):
    import http.client

    from test_mcp_identity_propagates import bound_server

    headers_json = {"Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "usage", "version": "1"}}}
    with bound_server(app) as hostport:
        host, port = hostport.split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            headers = {"Host": hostport, "Authorization": f"Bearer {key}",
                       **headers_json}
            conn.request("POST", "/mcp", body=json.dumps(initialize),
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
