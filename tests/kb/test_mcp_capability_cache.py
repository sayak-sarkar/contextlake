"""A renamed MCP tool must be named, once, and must not start a network storm.

The failure measured on this build before the fix: an `mcp` source configured to
call a tool the provider had renamed returned ONE Document whose body was the
server's error text ("Unknown tool: renamed_away"). `kb enrich` printed a green
"1 document(s) stored" and fed the error string into the vector index. The store
was not merely un-enriched, it was taught the error.

Two guards, and the tests below hold both directions of each:

* the diagnostic fires when, and only when, the probe proves the tool is gone;
* the re-probe runs once per server per run, not once per repo -- `kb enrich`
  calls the tool once per repo, so N failures without a latch is N probes aimed
  at the provider that has just broken.

Nothing here touches the network. The transport is stubbed wherever the subject
is the latch or the diagnostic, and is a real local subprocess wherever the
subject is *which* failure a server produced. Every host, token and source name
is synthetic.
"""

from __future__ import annotations

import logging
import os
import socket
import sys

import pytest

from contextlake.kb import capabilities
from contextlake.kb.connectors import mcp_query
from contextlake.kb.connectors.mcp_query import mcp_tool_query
from contextlake.kb.mcp_client import McpProbeShapeError, McpToolError, ToolInfo, ToolList
from contextlake.kb.resilience import CircuitOpenError, degraded_calls, reset_breakers

_RENAME_SENTENCE = "is not in this MCP server's advertised list any more"
_UNKNOWN_SENTENCE = "could not be re-asked"

_URL_A = "http://127.0.0.1:1/mcp"
_URL_B = "http://127.0.0.1:2/mcp"


class _TaskGroupError(Exception):
    """A stand-in for anyio's ExceptionGroup that works on 3.10 too.

    The builtin arrived in 3.11 and this package supports 3.10. Same shape a
    real stdio server produces: the real failure, two groups deep.
    """

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = tuple(exceptions)


def _wrapped(inner: BaseException) -> BaseException:
    return _TaskGroupError("unhandled errors in a TaskGroup",
                           [_TaskGroupError("unhandled errors in a TaskGroup", [inner])])


@pytest.fixture(autouse=True)
def _fresh_run():
    """Latch and breakers are both process-wide, so state them as preconditions.

    Without this a probe latched by one test makes the next test's count read 0
    for the wrong reason.
    """
    capabilities.reset_run()
    reset_breakers()
    yield
    capabilities.reset_run()
    reset_breakers()


@pytest.fixture
def logged():
    """Every message the package logger emits, once each.

    `gls_logs` cannot be counted against: it attaches caplog's handler to the
    package logger while `propagate` is also True, so the root handler sees the
    same record again and `caplog.text` holds two copies of every line. That is
    fine for "does this phrase appear" and wrong for "how many times". A handler
    of our own on that logger receives each emit exactly once, which is what a
    "logged once per run, not once per repo" claim needs.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("contextlake")
    handler = _Collect(level=logging.INFO)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _cfg(name="synthetic-wiki", tool="search", url=_URL_A):
    return {"type": "mcp", "name": name, "tool": tool, "url": url,
            "arg_template": {"query": "{terms}"}}


def _rejecting_call_tool(monkeypatch, tool="search"):
    """`call_tool` that fails the way a renamed tool fails, wrapper and all."""
    def _boom(**_kwargs):
        raise _wrapped(McpToolError(tool, f"Unknown tool: {tool}"))
    monkeypatch.setattr(mcp_query, "call_tool", _boom)


def _counting_probe(monkeypatch, result):
    """Swap in a probe that records how many times it was actually run."""
    calls = []

    def _probe(**kwargs):
        calls.append(kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(capabilities, "probe_tools", _probe)
    return calls


def _tools(*names):
    return ToolList(tools=[ToolInfo(n, f"help for {n}", {}) for n in names], truncated=False)


# --- the diagnostic ---------------------------------------------------------

def test_a_renamed_tool_produces_the_named_diagnostic(monkeypatch, gls_logs):
    """The configured tool is gone, and the log says so by name."""
    _rejecting_call_tool(monkeypatch)
    _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert gls_logs.text, "the capture saw nothing at all"
    assert _RENAME_SENTENCE in gls_logs.text
    assert "search_v2" in gls_logs.text
    assert "synthetic-wiki" in gls_logs.text


def test_a_tool_that_is_still_advertised_gets_no_rename_claim(monkeypatch, gls_logs):
    """The positive row: the guard discriminates instead of firing on every failure.

    Without it, an unconditional diagnostic passes the renamed-tool test above
    and tells every user their tool was removed.
    """
    _rejecting_call_tool(monkeypatch)
    _counting_probe(monkeypatch, (capabilities.OK, _tools("search", "search_v2"), ""))

    before = degraded_calls()
    assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert _RENAME_SENTENCE not in gls_logs.text
    assert _UNKNOWN_SENTENCE not in gls_logs.text
    # The generic degrade still happens and is still counted, so the run's own
    # verdict rule keeps working.
    assert "mcp tool 'search'" in gls_logs.text
    assert degraded_calls() == before + 1


def test_a_failed_reprobe_does_not_claim_a_rename(monkeypatch, gls_logs):
    """A probe that did not work cannot know which tools the server offers."""
    _rejecting_call_tool(monkeypatch)
    _counting_probe(monkeypatch,
                    (capabilities.UNREACHABLE, None, "mcp:http://127.0.0.1:1 refused"))

    assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert _UNKNOWN_SENTENCE in gls_logs.text
    assert _RENAME_SENTENCE not in gls_logs.text


def test_a_probe_that_raises_is_treated_as_a_failed_probe(monkeypatch, gls_logs):
    """A raising probe must not escape into the connector, which never raises."""
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, OSError("connection refused"))

    for _ in range(5):
        assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert len(calls) == 1, "a failed probe must latch too, or a dead server is re-dialled"
    assert _UNKNOWN_SENTENCE in gls_logs.text
    assert _RENAME_SENTENCE not in gls_logs.text


def test_a_raising_diagnostic_does_not_abort_the_connector(monkeypatch, gls_logs):
    """`mcp_tool_query` is documented as never raising, and runs once per repo.

    A diagnostic that threw would turn one renamed tool into an aborted enrich
    for the whole fleet.
    """
    _rejecting_call_tool(monkeypatch)

    def _explode(**_kwargs):
        raise RuntimeError("diagnostic blew up")

    monkeypatch.setattr(mcp_query, "explain_tool_failure", _explode)
    assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert "diagnostic blew up" in gls_logs.text


def test_a_truncated_list_does_not_claim_the_tool_was_removed(monkeypatch, gls_logs):
    """Page one cannot prove absence, so no removal is claimed from it."""
    _rejecting_call_tool(monkeypatch)
    partial = ToolList(tools=[ToolInfo("other", "", {})], truncated=True)
    _counting_probe(monkeypatch, (capabilities.OK, partial, ""))

    assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert _RENAME_SENTENCE not in gls_logs.text
    assert "partial" in gls_logs.text


def test_the_diagnostic_is_logged_once_per_run_not_once_per_repo(monkeypatch, logged):
    """One line for the run. `kb enrich` calls this once per repo.

    The five generic degrade lines are the control: they prove all five calls
    happened, so the single rename line is a fact about the diagnostic and not
    about a loop that ran once.
    """
    _rejecting_call_tool(monkeypatch)
    _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    for _ in range(5):
        mcp_tool_query(_cfg(), ["Widget"])
    assert len([m for m in logged if _RENAME_SENTENCE in m]) == 1
    assert len([m for m in logged if "unavailable" in m]) == 5


def test_two_servers_behind_one_host_are_not_told_about_each_other(monkeypatch, logged):
    """The damage the server key exists to stop, read as a user reads it.

    One gateway, two MCP servers mounted at different paths. Before the hosted
    key carried the path, both shared one capability record, so the second
    source was told BY NAME that its server offers the first server's tools,
    with a `--set tool=NAME` instruction that would have pointed it at a tool
    its own server does not have.

    Asserted on the warning line rather than on the key string, because the key
    is the mechanism and the sentence is the damage.

    Every value here is synthetic: no real host, org or tool name.
    """
    wiki = "https://mcp.synthetic.invalid/wiki/mcp"
    tickets = "https://mcp.synthetic.invalid/tickets/mcp"

    _rejecting_call_tool(monkeypatch)
    monkeypatch.setattr(
        capabilities, "probe_tools",
        lambda **kw: (capabilities.OK,
                      _tools("wiki_search" if kw.get("url") == wiki else "tickets_search"), ""))

    mcp_tool_query(_cfg(name="synthetic-a", url=wiki), ["Widget"])
    mcp_tool_query(_cfg(name="synthetic-b", url=tickets), ["Widget"])

    lines = [m for m in logged if _RENAME_SENTENCE in m]
    for_b = [m for m in lines if "synthetic-b" in m]
    assert len(for_b) == 1, "each source gets its own line, or the count is a latch artefact"
    assert "tickets_search" in for_b[0]
    assert "wiki_search" not in for_b[0]
    # The other direction, so this is "each server answers for itself" and not
    # "the second source is never told anything".
    for_a = [m for m in lines if "synthetic-a" in m]
    assert len(for_a) == 1
    assert "wiki_search" in for_a[0] and "tickets_search" not in for_a[0]


# --- the latch --------------------------------------------------------------

def test_the_reprobe_happens_once_across_an_n_repo_run(monkeypatch):
    """Five failures, one probe. Asserted as a count, not as 'the run passed'."""
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    for _ in range(5):
        assert mcp_tool_query(_cfg(), ["Widget"]) == []
    assert len(calls) == 1


def test_a_run_with_no_failures_probes_nothing(monkeypatch):
    """The positive row for the latch: a working source is never probed at all.

    A failure-only count test cannot tell a latch apart from code that probes on
    every call, because both would give 1 only by luck; this pins 0.
    """
    monkeypatch.setattr(mcp_query, "call_tool",
                        lambda **_k: [{"title": "a", "text": "b"}])
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search"), ""))

    for _ in range(5):
        assert len(mcp_tool_query(_cfg(), ["Widget"])) == 1
    assert len(calls) == 0


def test_the_latch_is_keyed_by_server_not_by_source(monkeypatch):
    """Two servers, two probes. One assertion carrying both directions."""
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    for _ in range(3):
        mcp_tool_query(_cfg(name="wiki-a", url=_URL_A), ["Widget"])
        mcp_tool_query(_cfg(name="wiki-b", url=_URL_B), ["Widget"])
    assert len(calls) == 2


def test_two_sources_on_one_server_probe_once_between_them(monkeypatch):
    """Deliberate, and a documented divergence from the ticket's wording.

    T2.3.4 asks for "once each" for two sources. Keyed by `server_key`, two
    sources pointed at the same host probe ONCE between them, because "which
    tools does this server advertise" is one fact about one server -- the same
    reasoning `endpoint_key` records for sharing one breaker. Probing twice is
    the storm the latch exists to stop.
    """
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    for _ in range(3):
        mcp_tool_query(_cfg(name="wiki-a", url=_URL_A), ["Widget"])
        mcp_tool_query(_cfg(name="wiki-b", url=_URL_A), ["Widget"])
    assert len(calls) == 1


def test_a_record_older_than_the_bound_is_re_probed(monkeypatch):
    """The answer stands for a bounded time, not for the life of the process.

    `reset_run` has one intended caller, the top of a command. A record that
    only ever expired there would, in any long-lived caller, answer from an hour
    ago and never re-ask -- the stale answer this module refuses to give.

    Precondition: the clock is injected, so no test sleeps.
    """
    now = [1000.0]
    monkeypatch.setattr(capabilities, "_clock", lambda: now[0])
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    mcp_tool_query(_cfg(), ["Widget"])
    # Positive row: inside the bound the answer still stands, so this is an age
    # check and not "probe every time".
    now[0] += capabilities.MAX_AGE_SECONDS - 1
    mcp_tool_query(_cfg(), ["Widget"])
    assert len(calls) == 1

    now[0] += 2
    mcp_tool_query(_cfg(), ["Widget"])
    assert len(calls) == 2


def test_an_aged_out_record_lets_the_diagnostic_speak_again(monkeypatch, logged):
    """A fresh probe deserves a fresh line; the once-per-run mark ages with it."""
    now = [1000.0]
    monkeypatch.setattr(capabilities, "_clock", lambda: now[0])
    _rejecting_call_tool(monkeypatch)
    _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    mcp_tool_query(_cfg(), ["Widget"])
    now[0] += capabilities.MAX_AGE_SECONDS - 1
    mcp_tool_query(_cfg(), ["Widget"])
    assert len([m for m in logged if _RENAME_SENTENCE in m]) == 1

    now[0] += 2
    mcp_tool_query(_cfg(), ["Widget"])
    assert len([m for m in logged if _RENAME_SENTENCE in m]) == 2


def test_reset_run_makes_the_next_run_probe_again(monkeypatch):
    """The record is scoped to a run, not to the life of the process."""
    _rejecting_call_tool(monkeypatch)
    calls = _counting_probe(monkeypatch, (capabilities.OK, _tools("search_v2"), ""))

    mcp_tool_query(_cfg(), ["Widget"])
    assert len(calls) == 1
    capabilities.reset_run()
    mcp_tool_query(_cfg(), ["Widget"])
    assert len(calls) == 2


# --- what a probe reports ---------------------------------------------------

def _probe_raising(monkeypatch, exc):
    def _boom(**_kwargs):
        raise exc
    monkeypatch.setattr(capabilities, "list_tools", _boom)


# A raw JSON-RPC stdio MCP server. Raw rather than the SDK's own server, because
# the SDK cannot be made to answer `tools/list` with a shape it would refuse to
# build -- and that shape is the whole subject of the MALFORMED rows below.
# `sys.argv[1]` picks how it answers.
_RAW_SERVER = """
import json, sys

MODE = sys.argv[1]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-raw", "version": "0"},
        }})
    elif method == "tools/list":
        if MODE == "method_not_found":
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "Method not found"}})
        elif MODE == "unauthorized":
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32002, "message": "unauthorized: no scope for tools"}})
        elif MODE == "no_tools_field":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif MODE == "tools_not_a_list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": "not-a-list"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "alpha", "description": "", "inputSchema": {"type": "object"}},
                {"name": "beta", "description": "", "inputSchema": {"type": "object"}}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
"""

# A server that starts and then exits: the common shape of a down stdio MCP
# server (a bridge that cannot authenticate, a crashed process).
_DYING_SERVER = "import sys\nsys.exit(3)\n"


def _raw_args(tmp_path, mode):
    script = tmp_path / "synthetic_raw_server.py"
    if not script.exists():
        script.write_text(_RAW_SERVER)
    return [str(script), mode]


def _closed_port() -> int:
    """A port nothing is listening on, chosen rather than assumed.

    A hard-coded port can be held by a leftover server from an earlier run, and
    this file has one row whose whole claim is that the dial fails.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_probe_tells_unreachable_rejected_and_malformed_apart(tmp_path):
    """Four answers, on the shapes real servers were measured producing.

    Every row here comes from a real spawn or a real dial, not a stand-in. The
    earlier version of this test raised `McpProbeShapeError` and a bare
    `ConnectionRefusedError` from a stub, and **neither is a shape either
    transport produces**: both arrive one or two `ExceptionGroup`s deep, and a
    malformed `tools/list` answer never reaches `mcp_client`'s own check at all
    because the SDK validates the frame into `ListToolsResult` first and raises
    `pydantic.ValidationError`. Measured on this build before the fix: both
    malformed rows below classified as REJECTED.

    One transport is enough for the shape rows. The classification is made in
    `probe_tools` on the exception, and `_alist` runs the same session body down
    both branches, so what differs per transport is only which exception arrives.
    The hosted row is kept for the one shape only that transport produces: a
    refused TCP connection, which is httpx's `ConnectError` rather than the SDK's
    `MCPError`.

    Precondition: local subprocesses and one dial to a closed local port. No
    network, and no real host, org or token name.
    """
    def _probe(mode):
        capabilities.reset_run()
        reset_breakers()
        return capabilities.probe_tools(
            command=sys.executable, args=_raw_args(tmp_path, mode), timeout=15)

    # The server answered "no". Two different rejections, so the row is about
    # the class of answer and not about one JSON-RPC code.
    assert _probe("method_not_found")[0] == capabilities.REJECTED
    assert _probe("unauthorized")[0] == capabilities.REJECTED

    # The server answered, and the answer could not be read. Two shapes: the
    # field missing, and the field present with the wrong type.
    assert _probe("no_tools_field")[0] == capabilities.MALFORMED
    assert _probe("tools_not_a_list")[0] == capabilities.MALFORMED

    # The positive row. Without it a classifier answering "rejected" for
    # everything, success included, passes the four rows above.
    outcome, tools, detail = _probe("healthy")
    assert (outcome, detail) == (capabilities.OK, "")
    assert [t.name for t in tools.tools] == ["alpha", "beta"]

    # The server never answered: it started and died. This is also the only row
    # with no `url` at all, so it is what pins the `url=None` path through
    # `_safe_detail` -- the detail names the program, and there is nothing to
    # swap a URL out of.
    capabilities.reset_run()
    reset_breakers()
    dying = tmp_path / "synthetic_dying_server.py"
    dying.write_text(_DYING_SERVER)
    outcome, _tools, detail = capabilities.probe_tools(
        command=sys.executable, args=[str(dying)], timeout=15)
    assert outcome == capabilities.UNREACHABLE
    assert detail.startswith(f"mcp:{os.path.basename(sys.executable)}#")

    # The hosted transport's own unreachable shape: a refused TCP connection.
    capabilities.reset_run()
    reset_breakers()
    url = f"http://127.0.0.1:{_closed_port()}/mcp"
    assert capabilities.probe_tools(url=url, timeout=15)[0] == capabilities.UNREACHABLE


def test_a_circuit_that_is_already_open_reads_as_unreachable(monkeypatch):
    """The breaker raises before any transport runs, so it arrives unwrapped.

    Its own row, because the four rows above all reach a transport and this one
    deliberately does not. Without it, moving the `CircuitOpenError` clause
    under the general handler would go unnoticed.
    """
    _probe_raising(monkeypatch, CircuitOpenError("mcp:https://mcp.synthetic.invalid", 60))
    assert capabilities.probe_tools(url=_URL_A, timeout=1)[0] == capabilities.UNREACHABLE


def test_the_sdk_handing_back_a_result_with_no_tools_reads_as_malformed(monkeypatch):
    """`McpProbeShapeError`'s own row: raised by this package, not by the SDK.

    Unreachable from a real server today (the SDK validates the frame first,
    which is the row above), so it is stubbed and labelled as such. It is what
    would fire if the SDK stopped validating, and the classifier has to read it
    through the same wrapper the transports use.
    """
    _probe_raising(monkeypatch, _wrapped(McpProbeShapeError("no 'tools' field")))
    assert capabilities.probe_tools(url=_URL_A, timeout=1)[0] == capabilities.MALFORMED


def test_a_probe_failure_names_the_host_and_never_the_url_query(monkeypatch, gls_logs):
    """A hosted MCP URL can carry a token, and this string is logged.

    Every value here is synthetic: no real host, org or token name.
    """
    url = "https://mcp.invalid.test/mcp?token=SYNTHETIC-NOT-A-REAL-TOKEN"
    _probe_raising(monkeypatch, ConnectionRefusedError(f"cannot reach {url}"))

    _outcome, _tools, detail = capabilities.probe_tools(url=url, timeout=1)
    # Positive row: the message still names the server. A detail naming nothing
    # would satisfy the two leak checks below and help nobody.
    assert "mcp.invalid.test" in detail
    assert "SYNTHETIC-NOT-A-REAL-TOKEN" not in detail
    assert "?token=" not in detail

    _rejecting_call_tool(monkeypatch)
    monkeypatch.setattr(capabilities, "probe_tools",
                        lambda **_k: (capabilities.UNREACHABLE, None, detail))
    mcp_tool_query(_cfg(url=url), ["Widget"])
    assert "mcp.invalid.test" in gls_logs.text
    assert "SYNTHETIC-NOT-A-REAL-TOKEN" not in gls_logs.text
