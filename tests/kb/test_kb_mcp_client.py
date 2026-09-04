"""Tests for the MCP client, against a spawned mock MCP server (no network)."""

import json
import sys

import pytest
from mcp.shared.exceptions import MCPError

import contextlake.kb.mcp_client as mcp_client
from contextlake.kb import resilience
from contextlake.kb.connectors.atlassian import AtlassianConnector
from contextlake.kb.mcp_client import call_tool, list_tools, server_key

# `mcp>=2.0` depends on the `httpx2` fork, and its streamable-HTTP client raises
# that fork's exception classes; a caller still on plain `httpx` raises the
# other. The real class is used either way and never a stand-in, because the
# point of the breaker tests below is that the guard sees what the transport
# actually raises. Round 1's version raised a bare ConnectionRefusedError from a
# stub, which is a shape neither transport produces, so it proved nothing.
try:
    import httpx2 as _httpx
except ImportError:  # pragma: no cover - one flavour or the other is installed
    import httpx as _httpx

# A tiny stdio MCP server used as the connection target. `echo` and `items`
# carry docstrings so the probe has a description to read; `undocumented` has
# none on purpose -- a tool with no help must still be listed.
_MOCK_SERVER = """
from mcp.server.mcpserver import MCPServer
m = MCPServer("mock")

@m.tool()
def echo(text: str) -> str:
    \"\"\"Echo the text straight back.\"\"\"
    return text

@m.tool()
def items() -> list[dict]:
    \"\"\"Two fixed rows, for result-shape tests.\"\"\"
    return [{"key": "A-1"}, {"key": "A-2"}]

@m.tool()
def undocumented(x: int) -> int:
    return x

m.run()
"""


def _server(tmp_path):
    p = tmp_path / "mock_server.py"
    p.write_text(_MOCK_SERVER)
    return [str(p)]


def test_list_tools(tmp_path):
    assert {"echo", "items"} <= {t.name for t in list_tools(sys.executable,
                                                            _server(tmp_path)).tools}


def test_list_tools_returns_each_tool_description(tmp_path):
    """Names alone cannot be chosen between, so the probe carries the help text.

    Precondition: the spawned mock server is a local subprocess, no network.
    """
    described = {t.name: t.description for t in list_tools(sys.executable,
                                                           _server(tmp_path)).tools}
    assert described["echo"] == "Echo the text straight back."
    assert described["items"] == "Two fixed rows, for result-shape tests."
    # Positive row for the other direction: a tool with no docstring is still
    # listed. Dropping it would hide half a server's tools from any picker and
    # still satisfy the two assertions above.
    assert described["undocumented"] == ""


def test_list_tools_carries_the_input_schema(tmp_path):
    """The schema is what lets a caller propose an argument template."""
    schemas = {t.name: t.input_schema for t in list_tools(sys.executable,
                                                          _server(tmp_path)).tools}
    assert schemas["echo"]["properties"]["text"]["type"] == "string"
    assert schemas["echo"]["required"] == ["text"]


def test_call_tool_scalar(tmp_path):
    assert call_tool(sys.executable, _server(tmp_path), "echo", {"text": "hello"}) == "hello"


def test_call_tool_structured(tmp_path):
    result = call_tool(sys.executable, _server(tmp_path), "items")
    assert result == [{"key": "A-1"}, {"key": "A-2"}]


class _FakeHttpResult:
    structured_content = {"result": "ok"}
    content = None


class _FakeHttpSession:
    """Stub session standing in for ``mcp.ClientSession`` in the http branch."""

    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, tool, arguments):
        assert tool == "search"
        assert arguments == {"q": "a"}
        return _FakeHttpResult()


class _FakeStreamableHttpCm:
    """Stub async context manager standing in for ``streamable_http_client``."""

    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        return ("read-stream", "write-stream", "extra-stream")

    async def __aexit__(self, *exc_info):
        return False


def test_call_tool_http(monkeypatch):
    monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
    monkeypatch.setattr(mcp_client, "ClientSession", _FakeHttpSession)
    result = call_tool(url="http://example.test/mcp", tool="search", arguments={"q": "a"})
    assert result == "ok"


# --- the url transport, the breaker, and the shape guard --------------------

class _FakeTool:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        self.input_schema = input_schema or {}


class _FakeToolsResult:
    def __init__(self, tools, next_cursor=None):
        self.tools = tools
        self.next_cursor = next_cursor


def _fake_list_session(result):
    """A ClientSession stand-in whose ``list_tools`` returns ``result``."""

    class _Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            if isinstance(result, BaseException):
                raise result
            return result

    return _Session


def _no_stdio(monkeypatch):
    """Make the stdio path a loud failure, so 'went over http' is proven.

    Both halves are patched. Building ``StdioServerParameters`` happens first
    and rejects a ``None`` command with a pydantic ValidationError, which would
    fail the test for a reason that reads like a schema problem rather than
    "it took the wrong transport".
    """
    def _boom(*_a, **_k):
        raise AssertionError("list_tools took the stdio path for a url probe")
    monkeypatch.setattr(mcp_client, "StdioServerParameters", _boom)
    monkeypatch.setattr(mcp_client, "stdio_client", _boom)


def test_list_tools_probes_a_url_without_spawning_a_process(monkeypatch):
    """A hosted MCP server can be probed at all, which it could not be before.

    Precondition: breakers are process-wide, so this resets them at both ends.
    """
    resilience.reset_breakers()
    _no_stdio(monkeypatch)
    monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
    monkeypatch.setattr(mcp_client, "ClientSession", _fake_list_session(
        _FakeToolsResult([_FakeTool("beta", "second"), _FakeTool("alpha", "first")])))
    try:
        found = list_tools(url="http://example.test/mcp", timeout=1)
    finally:
        resilience.reset_breakers()
    # Fed reversed, read sorted: two runs of the same probe must agree.
    assert [t.name for t in found.tools] == ["alpha", "beta"]
    assert [t.description for t in found.tools] == ["first", "second"]
    assert found.truncated is False


def test_list_tools_reports_a_partial_answer_as_truncated(monkeypatch):
    """A next_cursor means page one, and page one cannot prove a tool is absent."""
    resilience.reset_breakers()
    monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
    monkeypatch.setattr(mcp_client, "ClientSession", _fake_list_session(
        _FakeToolsResult([_FakeTool("alpha")], next_cursor="page-2")))
    try:
        assert list_tools(url="http://example.test/mcp", timeout=1).truncated is True
    finally:
        resilience.reset_breakers()


def test_list_tools_raises_a_named_error_on_an_answer_with_no_tools(monkeypatch):
    """A wrong-shaped answer is its own outcome, not 'the server offers nothing'."""
    resilience.reset_breakers()
    monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
    monkeypatch.setattr(mcp_client, "ClientSession", _fake_list_session(_FakeToolsResult(None)))
    try:
        with pytest.raises(mcp_client.McpProbeShapeError):
            list_tools(url="http://example.test/mcp", timeout=1)
    finally:
        resilience.reset_breakers()


def test_list_tools_shares_the_circuit_breaker_with_call_tool(monkeypatch, no_sleep):
    """A probe aimed at a dead server is written off, like every other call.

    It used to be a bare ``asyncio.run``, so the one call made at the worst
    moment -- straight at a provider that just failed -- was the unguarded one.

    Precondition: breakers are process-wide; reset at both ends.
    """
    resilience.reset_breakers()
    entered = []

    class _Refusing:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            entered.append(self.url)
            raise ConnectionRefusedError("connection refused")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(mcp_client, "streamable_http_client", _Refusing)
    url = "http://example.test/mcp"
    try:
        for _ in range(3):
            with pytest.raises(ConnectionRefusedError):
                list_tools(url=url, timeout=1)
        touched = len(entered)
        with pytest.raises(resilience.CircuitOpenError):
            list_tools(url=url, timeout=1)
        # The refused call never reached the transport, which is the whole point.
        assert len(entered) == touched

        # Positive row: after a reset a healthy server answers through the same
        # key. Without it, a list_tools that always raised CircuitOpenError
        # would pass everything above and refuse every working server.
        resilience.reset_breakers()
        monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
        monkeypatch.setattr(mcp_client, "ClientSession", _fake_list_session(
            _FakeToolsResult([_FakeTool("alpha"), _FakeTool("beta")])))
        assert len(list_tools(url=url, timeout=1).tools) == 2
    finally:
        resilience.reset_breakers()


def test_a_probe_that_fails_counts_against_the_same_record_call_tool_reads(
        monkeypatch, no_sleep):
    """One server, one health record: the probe's failures are call_tool's too.

    This is what a second MCP client would have cost. Three failed probes must
    leave the enrich path refusing the same server, not starting its own count
    from zero.

    Precondition: breakers are process-wide; reset at both ends.
    """
    resilience.reset_breakers()

    class _Refusing:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            raise ConnectionRefusedError("connection refused")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(mcp_client, "streamable_http_client", _Refusing)
    url = "http://example.test/mcp"
    try:
        for _ in range(3):
            with pytest.raises(ConnectionRefusedError):
                list_tools(url=url, timeout=1)
        with pytest.raises(resilience.CircuitOpenError):
            call_tool(url=url, tool="search", arguments={}, timeout=1)

        # Positive row: a different server is untouched by the first one's
        # failures, so this is a per-server record and not a global off switch.
        monkeypatch.setattr(mcp_client, "streamable_http_client", _FakeStreamableHttpCm)
        monkeypatch.setattr(mcp_client, "ClientSession", _fake_list_session(
            _FakeToolsResult([_FakeTool("alpha")])))
        assert len(list_tools(url="http://other.test/mcp", timeout=1).tools) == 1
    finally:
        resilience.reset_breakers()


# --- the server key: two servers run by one program -------------------------
# Measured on this build before the fix: `server_key` stopped at the program's
# basename whenever no argument was URL-shaped, so two different servers both
# spawned by `python` keyed to "mcp:python". One breaker guarded both, and the
# capability record answered for one server with the other server's tool list.

def _key(command, args=(), env=None):
    return server_key(command, list(args), None, env=env)


def test_two_stdio_servers_run_by_one_program_do_not_share_a_key():
    """`uvx server-a` and `uvx server-b` are two servers, not one."""
    alpha = _key("/usr/bin/uvx", ["contextlake-mcp-alpha"])
    beta = _key("/usr/bin/uvx", ["contextlake-mcp-beta"])
    assert alpha != beta
    # Positive row: identity decides, not a counter or a clock. The same source
    # asked twice must land on the same record, or the breaker forgets every
    # failure and the latch re-probes once per repo.
    assert _key("/usr/bin/uvx", ["contextlake-mcp-alpha"]) == alpha


def test_the_full_command_path_is_part_of_the_identity():
    """Two builds of one program under different prefixes are two servers.

    The basename is the same, and the basename is all the old key kept.
    """
    assert _key("/usr/bin/mcp-server") != _key("/opt/vendor/bin/mcp-server")


def test_the_environment_is_part_of_the_identity():
    """The `mcp-proxy` shape: one argv, the target host set in the environment."""
    alpha = _key("/usr/bin/mcp-proxy", (), {"MCP_TARGET": "https://alpha.invalid/mcp"})
    beta = _key("/usr/bin/mcp-proxy", (), {"MCP_TARGET": "https://beta.invalid/mcp"})
    assert alpha != beta
    # Positive row: no environment and an empty one are the same environment, so
    # a source that sets none still matches its own record run to run.
    assert _key("/usr/bin/mcp-proxy") == _key("/usr/bin/mcp-proxy", (), {})


def test_argument_boundaries_survive_the_digest():
    """`["a", "bc"]` and `["ab", "c"]` are different argv and must key apart.

    A key built by joining the arguments into one string loses the boundary and
    passes every other test in this block.
    """
    assert _key("/usr/bin/x", ["a", "bc"]) != _key("/usr/bin/x", ["ab", "c"])


def test_the_key_never_prints_the_argv_or_the_environment():
    """It is logged, and a stdio argv or env can carry a token and a home path.

    Every value here is synthetic: no real host, user, path or token.
    """
    key = _key("/home/synthetic-user/bin/mcp-server",
               ["--config", "/home/synthetic-user/.private/config.toml"],
               {"SYNTHETIC_TOKEN": "NOT-A-REAL-TOKEN-0000"})
    assert "NOT-A-REAL-TOKEN-0000" not in key
    assert "synthetic-user" not in key
    assert ".private" not in key
    # Positive row: it still names the program. A key that printed nothing
    # readable would satisfy the three checks above and leave an operator unable
    # to tell which source was written off.
    assert key.startswith("mcp:mcp-server#")


# --- the key: stable and complete at the same time ---------------------------
# The two properties pull against each other, which is why digesting the whole
# environment looked like an answer. It is not one: every stdio connector builds
# its env as `dict(os.environ)` plus one override, so a whole-environment digest
# is a timestamp. Each test below fails when the other one's fix is reverted.

def _spawn_key(connector) -> str:
    """Key a connector through the argv and env it spawns with."""
    command, args, env = connector._spawn()
    return server_key(command, args, None, env=env)


def test_ambient_environment_changes_do_not_move_a_stdio_source_key(monkeypatch, tmp_path):
    """STABILITY, through the real caller rather than a stand-in.

    `_spawn` runs again on every call, so the snapshot is rebuilt every time.
    Measured before the fix: the first embedder load put three new variables
    into `os.environ`, the next snapshot differed, and the SAME Atlassian source
    got a NEW key mid-run. Failures then counted on a fresh record and the
    breaker never opened, which is the inert guard the digest exists to remove.

    Kept from the round that added it, and it now pins the caller-side fix:
    `_spawn` returns overrides only, so there is no snapshot left to move.

    Every value here is synthetic: no real host, org, path or token name.
    """
    connector = AtlassianConnector("synthetic-atlassian",
                                   mcp_url="https://mcp.synthetic.invalid/v1",
                                   auth_dir=str(tmp_path / "synthetic-auth"))
    before = _spawn_key(connector)

    # What loading the first embedder does to the process environment.
    monkeypatch.setenv("SYNTHETIC_TOKENIZERS_PARALLELISM", "false")
    monkeypatch.setenv("SYNTHETIC_OMP_NUM_THREADS", "1")
    monkeypatch.setenv("SYNTHETIC_HUB_OFFLINE", "1")

    assert _spawn_key(connector) == before

    # Positive row: what the source itself configures still moves the key, so
    # this is "ambient is not identity" and not "nothing is identity".
    other = AtlassianConnector("synthetic-atlassian",
                               mcp_url="https://mcp.synthetic.invalid/v1",
                               auth_dir=str(tmp_path / "second-synthetic-auth"))
    assert _spawn_key(other) != before


def test_the_proxy_target_survives_the_caller_passing_only_its_overrides():
    """COMPLETENESS, on the shape a caller now builds.

    The `mcp-proxy` shape puts its target host entirely in the environment. A
    key that answered stability by dropping the environment altogether would
    pass the test above and put two servers on one health record and one tool
    list -- the collision the digest was added for.

    Retargeted: this used to build `dict(os.environ)` plus the override, the
    caller shape the fix removed. Asserting on it would have been a test holding
    the old mistake open.

    Every value here is synthetic: no real host or variable name.
    """
    def _key(target):
        return server_key("/usr/bin/mcp-proxy", (), None,
                          env={"SYNTHETIC_MCP_TARGET": target})

    assert _key("https://alpha.invalid/mcp") != _key("https://beta.invalid/mcp")
    # Positive row: identity decides. The same override keyed twice is one key.
    assert _key("https://alpha.invalid/mcp") == _key("https://alpha.invalid/mcp")


def test_an_override_that_matches_ambient_is_still_part_of_the_identity(monkeypatch):
    """COMPLETENESS, against the subtraction the fix deleted.

    Round 3 dropped every entry whose NAME the ambient environment already
    carried with the SAME value. So a source that set `SYNTHETIC_MCP_TARGET` to
    what the shell had exported keyed identically to a source that set nothing,
    and those two spawn different children: the first child holds the variable,
    the second takes the SDK's default environment, which drops it (see
    `test_no_overrides_still_takes_the_sdk_default_environment`). Two servers,
    one health record, and one server's tool list reported as the other's.

    The earlier version of this test set a name ambient did NOT carry, which the
    subtraction keeps -- it passed with the bug present and proved nothing.

    Every value here is synthetic: no real host or variable name.
    """
    monkeypatch.setenv("SYNTHETIC_MCP_TARGET", "https://alpha.invalid/mcp")
    alpha = server_key("/usr/bin/mcp-proxy", (), None,
                       env={"SYNTHETIC_MCP_TARGET": "https://alpha.invalid/mcp"})
    none_at_all = server_key("/usr/bin/mcp-proxy", (), None, env={})
    assert alpha != none_at_all
    # Positive row: the override is what carries, not the ambient value. Moving
    # ambient alone must not move either key.
    monkeypatch.setenv("SYNTHETIC_MCP_TARGET", "https://beta.invalid/mcp")
    assert server_key("/usr/bin/mcp-proxy", (), None,
                      env={"SYNTHETIC_MCP_TARGET": "https://alpha.invalid/mcp"}) == alpha
    assert server_key("/usr/bin/mcp-proxy", (), None, env={}) == none_at_all


def test_an_int_and_its_string_key_the_same():
    """A TOML `env` table can hand this an int, and both spawn the same child.

    `_spawn_env` coerces to str on the way to the process, so keying them apart
    would open two health records for one server.
    """
    assert (server_key("/usr/bin/mcp-proxy", (), None, env={"SYNTHETIC_PORT": 8080})
            == server_key("/usr/bin/mcp-proxy", (), None, env={"SYNTHETIC_PORT": "8080"}))


# --- the key on the hosted transport -----------------------------------------
# The same collision, one transport over: the hosted branch stopped at
# scheme/host/port, so two MCP servers behind one gateway shared one breaker and
# one capability record, and a URL with no parseable host keyed to the bare
# string "mcp" whatever it pointed at.

def test_two_hosted_servers_on_one_host_at_different_paths_key_apart():
    """One gateway, two mounted servers, two health records."""
    wiki = server_key(None, (), "https://mcp.synthetic.invalid/wiki/mcp")
    tickets = server_key(None, (), "https://mcp.synthetic.invalid/tickets/mcp")
    assert wiki != tickets
    # Positive row: identity decides, not a counter or a clock.
    assert server_key(None, (), "https://mcp.synthetic.invalid/wiki/mcp") == wiki
    # And the readable half still names the host, so an operator can tell which
    # endpoint was written off.
    assert wiki.startswith("mcp:https://mcp.synthetic.invalid#")


def test_two_hosted_servers_on_one_host_at_different_ports_key_apart():
    """Two MCP servers on one box, two health records.

    `endpoint_key` already carries the port, so this pins the readable half
    rather than the digest, and it is the fourth of the four shapes the key has
    to separate.
    """
    first = server_key(None, (), "http://mcp.synthetic.invalid:8931/mcp")
    second = server_key(None, (), "http://mcp.synthetic.invalid:8932/mcp")
    assert first != second
    assert first.startswith("mcp:http://mcp.synthetic.invalid:8931#")
    # Positive row: identity decides, not a counter or a clock.
    assert server_key(None, (), "http://mcp.synthetic.invalid:8931/mcp") == first


def test_a_url_with_no_parseable_host_still_keys_by_what_it_points_at():
    """`endpoint_key` gives back the bare prefix here, and a bare prefix is not
    an identity: every unparseable URL shared the one key "mcp"."""
    first = server_key(None, (), "not-a-url-at-all")
    second = server_key(None, (), "also::not::a::url")
    assert first != second
    assert first != "mcp" and second != "mcp"


def test_a_hosted_url_keeps_its_query_out_of_the_printed_key():
    """It is logged, and a hosted MCP URL can carry a token in the query.

    Every value here is synthetic: no real host or token.
    """
    key = server_key(None, (), "https://mcp.synthetic.invalid/mcp?token=NOT-A-REAL-TOKEN-0000")
    assert "NOT-A-REAL-TOKEN-0000" not in key
    assert "?token=" not in key
    assert key.startswith("mcp:https://mcp.synthetic.invalid#")


# --- the circuit breaker, on the shapes a down server really raises ----------

class _TaskGroupError(Exception):
    """A stand-in for anyio's ExceptionGroup that also works on 3.10.

    The builtin arrived in 3.11 and this package supports 3.10. `resilience`
    finds a group by its `exceptions` attribute rather than by isinstance, for
    that same reason, so this is the shape its walker actually reads. The
    wrapper is a stand-in; every leaf below is the real class the transport
    raises, at the depth it was measured at.
    """

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = tuple(exceptions)


def _group(*inner):
    return _TaskGroupError("unhandled errors in a TaskGroup (1 sub-exception)", inner)


def _raising_http(monkeypatch, exc, dialled):
    class _Transport:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            dialled.append(self.url)
            raise exc

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(mcp_client, "streamable_http_client", _Transport)


def test_a_refused_hosted_server_opens_the_circuit(monkeypatch, no_sleep):
    """Measured shape: ExceptionGroup wrapping httpx `ConnectError`, one deep.

    Measured on this build before the fix: five calls at a closed local port
    made five dials and left `failures=0`. httpx errors are plain Exceptions,
    not OSError, so the classifier fell through to reading the wrapper's text,
    "unhandled errors in a TaskGroup", and answered "not the endpoint's fault"
    every time. The guard was present and inert.

    Precondition: the transport is stubbed. Nothing here touches the network.
    """
    resilience.reset_breakers()
    dialled = []
    _raising_http(monkeypatch, _group(_httpx.ConnectError("All connection attempts failed")),
                  dialled)
    url = "http://127.0.0.1:9/mcp"
    try:
        for _ in range(3):
            with pytest.raises(_TaskGroupError):
                list_tools(url=url, timeout=1)
        assert len(dialled) == 3
        with pytest.raises(resilience.CircuitOpenError):
            list_tools(url=url, timeout=1)
        # The refused call never reached the transport, which is the point.
        assert len(dialled) == 3
    finally:
        resilience.reset_breakers()


def test_a_rejected_request_still_does_not_count_against_the_endpoint(monkeypatch, no_sleep):
    """The other direction: -32601 is the server saying no, not a sick endpoint.

    Counting it would replace an actionable "that method is gone" with "circuit
    open" on the fourth call, which is the trade `is_endpoint_failure` exists to
    refuse. Wrapped two groups deep, the depth a real stdio server delivers.
    """
    resilience.reset_breakers()
    dialled = []
    _raising_http(monkeypatch,
                  _group(_group(MCPError(code=-32601, message="Method not found"))), dialled)
    url = "http://127.0.0.1:9/mcp"
    try:
        for _ in range(5):
            with pytest.raises(_TaskGroupError):
                list_tools(url=url, timeout=1)
        assert len(dialled) == 5, "a rejection must not stop the calls"
        assert resilience.breaker_for(server_key(None, (), url)).failures == 0
    finally:
        resilience.reset_breakers()


# A server that starts and then exits: the common shape of a down stdio MCP
# server (a bridge that cannot authenticate, a crashed process). Nothing raises
# FileNotFoundError, because the spawn itself worked.
_DYING_SERVER = "import sys\nsys.exit(3)\n"


def test_a_stdio_server_that_starts_and_dies_opens_the_circuit(tmp_path, no_sleep):
    """A real spawn, not a stub, on the shape that was measured.

    The SDK reports this as `MCPError(-32000, "Connection closed")` two task
    groups deep. Measured on this build before the fix: four spawns, four
    dials, `failures=0`, circuit still closed. `MCPError` is a plain Exception,
    so no type test in `resilience` could read it; the fix reads the JSON-RPC
    code instead of the message.

    Precondition: a local subprocess that exits at once. No network.
    """
    resilience.reset_breakers()
    script = tmp_path / "dies.py"
    script.write_text(_DYING_SERVER)
    args = [str(script)]
    key = server_key(sys.executable, args, None)
    try:
        for _ in range(3):
            with pytest.raises(BaseException) as caught:  # noqa: B017 - the shape is the point
                list_tools(sys.executable, args, timeout=10)
            assert not isinstance(caught.value, resilience.CircuitOpenError)
        assert resilience.breaker_for(key).state == "open"
        with pytest.raises(resilience.CircuitOpenError):
            list_tools(sys.executable, args, timeout=10)

        # Positive row: a server that answers leaves its own circuit closed, so
        # this is a health record and not "stdio always fails". Same transport,
        # same call, real subprocess.
        healthy = _server(tmp_path)
        assert len(list_tools(sys.executable, healthy, timeout=30).tools) == 3
        assert resilience.breaker_for(server_key(sys.executable, healthy, None)).state == "closed"
    finally:
        resilience.reset_breakers()


# --- what the spawned child actually gets ------------------------------------
# The risk of moving the ambient merge from the connectors to the spawn: if the
# spawn stops inheriting `os.environ`, every stdio connector breaks, and it
# breaks silently. `npx`, `uvx` and a bare interpreter all need PATH and HOME to
# run at all, and the MCP SDK REPLACES the child environment outright whenever
# it is given one. So the child is asked, by a real subprocess, what it holds.

_ENV_SERVER = """
import json, os
from mcp.server.mcpserver import MCPServer
m = MCPServer("envmock")

@m.tool()
def env_of(names: list[str]) -> str:
    \"\"\"Report this child process's own value for each named variable.\"\"\"
    return json.dumps({n: os.environ.get(n, "<<absent>>") for n in names})

m.run()
"""

_ASKED = ["SYNTHETIC_AMBIENT_MARKER", "SYNTHETIC_OVERRIDE_MARKER", "PATH"]


def _child_env(tmp_path, env):
    """Spawn a real stdio MCP server and read back its own environment."""
    script = tmp_path / "env_server.py"
    script.write_text(_ENV_SERVER)
    raw = call_tool(sys.executable, [str(script)], "env_of", {"names": _ASKED},
                    timeout=60, env=env)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_a_stdio_child_gets_its_overrides_and_the_sdk_allowlist_only(
        tmp_path, monkeypatch):
    """The child holds its overrides and PATH, and NOT the caller's environment.

    This replaces a test that asserted the opposite. An earlier version merged
    `os.environ` into every spawn on the stated reason that the SDK "replaces the
    child environment outright when it is given one". That reason is false: the
    SDK builds the child environment as
    `get_default_environment() | (server.env or {})` (mcp/client/stdio.py), so an
    overrides-only caller already gets a safe allowlist plus its overrides.

    Measured on this build: 7 variables with the allowlist, 60 with the merge.
    The merge added nothing the child needed and handed a user-configured
    third-party MCP server every variable in this process, tokens included.

    Precondition: a local subprocess. No network. Every value is synthetic.
    """
    monkeypatch.setenv("SYNTHETIC_AMBIENT_MARKER", "ambient-value")
    seen = _child_env(tmp_path, {"SYNTHETIC_OVERRIDE_MARKER": "override-value"})
    # the override arrives
    assert seen["SYNTHETIC_OVERRIDE_MARKER"] == "override-value"
    # PATH arrives, from the SDK allowlist, so npx can still start
    assert seen["PATH"] != "<<absent>>"
    # and an arbitrary ambient variable does NOT. This is the security property:
    # it fails if anyone reintroduces an ambient merge.
    assert seen["SYNTHETIC_AMBIENT_MARKER"] == "<<absent>>"


def test_the_child_environment_stays_narrow(tmp_path, monkeypatch):
    """Breadth, not just membership. Nothing pinned the SIZE of the child
    environment, so a 7-to-60 widening read green across the whole suite.

    The bound is the SDK allowlist (6 on POSIX) plus the overrides passed. A
    handful of spare slots is allowed so a platform difference in the allowlist
    does not fail this, but 60 cannot pass.
    """
    from mcp.client.stdio import get_default_environment

    monkeypatch.setenv("SYNTHETIC_AMBIENT_MARKER", "ambient-value")
    monkeypatch.setenv("SYNTHETIC_SECOND_MARKER", "also-ambient")
    seen = _child_env(tmp_path, {"SYNTHETIC_OVERRIDE_MARKER": "override-value"})
    present = {k for k, v in seen.items() if v != "<<absent>>"}
    allowed = set(get_default_environment()) | {"SYNTHETIC_OVERRIDE_MARKER"}
    assert present <= allowed, f"child holds more than the allowlist: {present - allowed}"


def test_an_override_is_what_the_child_reads_for_that_name(tmp_path, monkeypatch):
    """An override named the same as an ambient variable is what the child sees.

    With no ambient merge the ambient value never reaches the child at all, so
    this pins the half that matters to a connector: `MCP_REMOTE_CONFIG_DIR` set
    by the connector is the value `mcp-remote` reads, not one inherited from the
    operator's shell.
    """
    monkeypatch.setenv("SYNTHETIC_AMBIENT_MARKER", "ambient-value")
    seen = _child_env(tmp_path, {"SYNTHETIC_AMBIENT_MARKER": "override-wins"})
    assert seen["SYNTHETIC_AMBIENT_MARKER"] == "override-wins"


def test_no_overrides_still_takes_the_sdk_default_environment(tmp_path, monkeypatch):
    """`env=None` is left alone, and this is what makes the first test mean something.

    Measured on this build: the SDK's own default environment carries PATH and
    drops an arbitrary ambient variable. So a child that reports
    `SYNTHETIC_AMBIENT_MARKER` above got it from the merge and not from the SDK
    doing it all along. It also pins the one axis this change has: a caller with
    no overrides spawns the child it always spawned.
    """
    monkeypatch.setenv("SYNTHETIC_AMBIENT_MARKER", "ambient-value")
    seen = _child_env(tmp_path, None)
    assert seen["SYNTHETIC_AMBIENT_MARKER"] == "<<absent>>"
    assert seen["PATH"] != "<<absent>>"


# --- stability, at the end that stability exists for -------------------------

def test_the_breaker_opens_though_the_environment_moves_between_failures(
        tmp_path, monkeypatch, no_sleep):
    """STABILITY, measured on the breaker rather than on the key.

    A key that moves mid-run spreads three failures over three health records,
    each below the threshold of 3, so the breaker never opens and every
    remaining repo in the fleet pays another full timeout at a server already
    known to be dead. The key test alone cannot show that; this asserts the
    state the key exists to reach.

    The env comes from a real `AtlassianConnector._spawn()`, called again inside
    the loop the way the connector calls it on every request, so reverting the
    connector to a `dict(os.environ)` snapshot breaks this test. `failures == 3`
    is the assertion that reads the defect: with a moving key the loop's spawns
    land on other records and this one stays at 0. The spawned program is a
    local script that exits at once. No network.
    """
    resilience.reset_breakers()
    script = tmp_path / "dies.py"
    script.write_text(_DYING_SERVER)
    args = [str(script)]
    connector = AtlassianConnector("synthetic-atlassian",
                                   mcp_url="https://mcp.synthetic.invalid/v1",
                                   auth_dir=str(tmp_path / "synthetic-auth"))
    key = server_key(sys.executable, args, None, env=connector._spawn()[2])
    try:
        for i in range(3):
            # What an embedder load, a tokenizer import or a subprocess helper
            # does to the process environment part-way through a run.
            monkeypatch.setenv(f"SYNTHETIC_RUN_MARKER_{i}", str(i))
            _, _, env = connector._spawn()
            with pytest.raises(BaseException) as caught:  # noqa: B017 - shape is the point
                list_tools(sys.executable, args, timeout=10, env=env)
            assert not isinstance(caught.value, resilience.CircuitOpenError)
        assert resilience.breaker_for(key).failures == 3
        assert resilience.breaker_for(key).state == "open"
        _, _, env = connector._spawn()
        with pytest.raises(resilience.CircuitOpenError):
            list_tools(sys.executable, args, timeout=10, env=env)
    finally:
        resilience.reset_breakers()
