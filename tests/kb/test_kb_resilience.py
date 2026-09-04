"""Circuit breaker + jittered retry for the knowledge layer's network paths (RC-P2-3 / D-8).

Offline and instant by construction: the breaker's clock is injected (so crossing
a 60s cooldown costs nothing), and the retry loop's backoff goes through
``core.time.sleep``, which the shared ``no_sleep`` fixture stubs out. Nothing here
opens a socket or spawns a process.

The behaviour under test is the whole point of the module, so it is pinned
end-to-end rather than only at the class: the four state transitions, the fact
that a *rejected request* (Ollama's 404 for an unpulled model) must not count
toward the breaker, and that a refused call stays audible in the log instead of
turning into an empty result.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest

import contextlake.kb.embeddings.ollama as emb_ollama
import contextlake.kb.llm.ollama as llm_ollama
import contextlake.kb.mcp_client as mcp_client
from contextlake.kb.connectors.figma import FigmaConnector
from contextlake.kb.mcp_client import server_key
from contextlake.kb.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    breaker_for,
    degraded_calls,
    describe,
    endpoint_key,
    is_endpoint_failure,
    is_retryable,
    note_unavailable,
    reset_breakers,
)


class FakeClock:
    """A monotonic clock the test advances by hand (no real sleeping)."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _down(_=None):
    """A callable standing in for an endpoint that refuses connections."""
    raise ConnectionRefusedError("connection refused")


def _breaker(clock=None, **kw) -> CircuitBreaker:
    kw.setdefault("threshold", 3)
    kw.setdefault("cooldown", 60.0)
    kw.setdefault("attempts", 1)  # retry is exercised separately
    return CircuitBreaker("test-endpoint", clock=clock or FakeClock(), **kw)


# --- state machine ---------------------------------------------------------

def test_closed_until_threshold_then_opens():
    breaker = _breaker()
    for _ in range(2):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)
        assert breaker.state == "closed"

    with pytest.raises(ConnectionRefusedError):
        breaker.call(_down)
    assert breaker.state == "open"


def test_open_circuit_short_circuits_without_calling_through():
    calls = []

    def record_then_fail():
        calls.append(1)
        raise ConnectionRefusedError("connection refused")

    breaker = _breaker()
    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(record_then_fail)
    assert len(calls) == 3

    # Every further call is refused instantly -- the endpoint is never touched.
    for _ in range(10):
        with pytest.raises(CircuitOpenError):
            breaker.call(record_then_fail)
    assert len(calls) == 3, "an open circuit must not reach the endpoint"


def test_open_circuit_error_says_why_rather_than_returning_nothing():
    breaker = _breaker()
    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)

    with pytest.raises(CircuitOpenError) as exc:
        breaker.call(_down)
    message = str(exc.value)
    assert "circuit open" in message
    assert "test-endpoint" in message
    assert exc.value.retry_in == pytest.approx(60.0)


def test_half_open_after_cooldown_lets_one_probe_through_and_recovers():
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)
    assert breaker.state == "open"

    clock.advance(60.0)
    assert breaker.state == "half-open"

    assert breaker.call(lambda: "back") == "back"
    assert breaker.state == "closed"
    assert breaker.failures == 0


def test_failed_half_open_probe_reopens_for_a_fresh_cooldown():
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)

    clock.advance(60.0)
    with pytest.raises(ConnectionRefusedError):
        breaker.call(_down)  # the probe fails
    assert breaker.state == "open"

    clock.advance(59.0)
    assert breaker.state == "open", "the cooldown must restart from the failed probe"
    clock.advance(1.0)
    assert breaker.state == "half-open"


def test_success_resets_the_failure_count():
    breaker = _breaker()
    for _ in range(2):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)
    assert breaker.failures == 2

    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.failures == 0

    # ...so the next two failures start from scratch and do NOT open the circuit.
    for _ in range(2):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)
    assert breaker.state == "closed"


# --- what counts as a failure ----------------------------------------------

def _http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {}).encode()
    return urllib.error.HTTPError("http://endpoint.invalid", code, "err", {},
                                  io.BytesIO(payload))


@pytest.mark.parametrize("exc", [
    ConnectionRefusedError("refused"),
    TimeoutError("timed out"),
    subprocess.TimeoutExpired(["glab"], 30),
    urllib.error.URLError("name resolution failed"),
    _http_error(500),
    _http_error(503),
    _http_error(429),
])
def test_endpoint_shaped_failures_count(exc):
    assert is_endpoint_failure(exc) is True


@pytest.mark.parametrize("exc", [
    _http_error(404),
    _http_error(401),
    _http_error(400),
    ValueError("malformed response"),
])
def test_rejected_requests_do_not_count(exc):
    assert is_endpoint_failure(exc) is False


class _TaskGroupError(Exception):
    """Stand-in for anyio's ``ExceptionGroup``: the same duck-typed
    ``exceptions`` member list ``_unwrap`` walks.

    Spelled out rather than using the builtin ``ExceptionGroup`` because the kb
    test matrix still includes Python 3.10, where that builtin does not exist --
    which is also why ``_unwrap`` matches on the attribute rather than the type.
    """

    def __init__(self, message, exceptions):
        super().__init__(message)
        self.exceptions = tuple(exceptions)


def _task_group_wrapped(inner: BaseException) -> BaseException:
    """The shape anyio hands back: the real failure, two task groups deep.

    Reproduced from an actual ``call_tool`` against a blackholed synthetic MCP
    server -- ``ExceptionGroup(ExceptionGroup(TimeoutError()))``, whose ``str()``
    is the uninformative "unhandled errors in a TaskGroup (1 sub-exception)".
    """
    return _TaskGroupError("unhandled errors in a TaskGroup",
                           [_TaskGroupError("unhandled errors in a TaskGroup", [inner])])


def test_task_group_wrapped_timeout_is_still_recognised_as_the_endpoint_failing():
    """Without unwrapping, the breaker never opens on the MCP path at all.

    Measured before the fix: a `kb connect` run against an unreachable MCP
    server classified every failure as "not the endpoint's fault", so the
    circuit stayed closed for the entire fleet.
    """
    wrapped = _task_group_wrapped(TimeoutError())
    assert "TaskGroup" in str(wrapped), "the wrapper's own message says nothing useful"
    assert is_endpoint_failure(wrapped) is True
    assert is_retryable(wrapped) is False
    assert describe(wrapped) == "TimeoutError"


def test_task_group_wrapped_refusal_is_retryable_like_a_bare_one():
    wrapped = _task_group_wrapped(ConnectionRefusedError("connection refused"))
    assert is_endpoint_failure(wrapped) is True
    assert is_retryable(wrapped) is True
    assert describe(wrapped) == "connection refused"


def test_describe_reads_through_a_wrapper_around_a_non_transport_failure():
    """A rejection is not transport-shaped, and it still has to be readable.

    Without the fallback the whole line is "unhandled errors in a TaskGroup
    (1 sub-exception)", which replaces the one sentence the reader needed with
    no sentence at all. This is the shape a real MCP server produces when it
    answers a tool call with an error.
    """
    wrapped = _task_group_wrapped(RuntimeError("the server said no"))
    assert is_endpoint_failure(wrapped) is False, "a refusal is not the endpoint failing"
    assert describe(wrapped) == "the server said no"
    # Positive row for the other branch: a transport failure inside the same
    # wrapper still wins, so the fallback did not displace the transport match.
    assert describe(_task_group_wrapped(TimeoutError())) == "TimeoutError"


def test_mcp_wrapped_rejection_still_does_not_count_against_the_endpoint():
    wrapped = _task_group_wrapped(_http_error(404))
    assert is_endpoint_failure(wrapped) is False


def test_timeouts_are_never_retried_but_fast_failures_are():
    # A timeout already burned its whole budget; repeating it doubles the stall
    # this module exists to remove.
    assert is_retryable(TimeoutError("timed out")) is False
    assert is_retryable(subprocess.TimeoutExpired(["glab"], 30)) is False
    assert is_retryable(ConnectionRefusedError("refused")) is True
    assert is_retryable(_http_error(503)) is True
    assert is_retryable(_http_error(500)) is False


def test_retry_reuses_the_mirror_tiers_backoff_loop(no_sleep):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("refused")
        return "ok"

    breaker = _breaker(attempts=3)
    assert breaker.call(flaky) == "ok"
    assert len(attempts) == 3
    assert breaker.state == "closed"


def test_a_retried_call_counts_as_one_failure_not_one_per_attempt(no_sleep):
    breaker = _breaker(attempts=3)
    with pytest.raises(ConnectionRefusedError):
        breaker.call(_down)
    assert breaker.failures == 1


def test_kwargs_never_collide_with_the_retry_loops_own_parameters():
    seen = {}

    def endpoint(url, *, max_retries=None, timeout=None):
        seen.update(url=url, max_retries=max_retries, timeout=timeout)
        return "ok"

    assert _breaker().call(endpoint, "http://endpoint.invalid",
                           max_retries="callee's own", timeout=5) == "ok"
    assert seen == {"url": "http://endpoint.invalid",
                    "max_retries": "callee's own", "timeout": 5}


# --- observability ---------------------------------------------------------

def test_opening_and_skipping_are_both_logged_once(gls_logs):
    breaker = _breaker()
    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            breaker.call(_down)
    for _ in range(20):
        with pytest.raises(CircuitOpenError):
            breaker.call(_down)

    opened = [r for r in gls_logs.records if "circuit OPEN" in r.getMessage()]
    skipped = [r for r in gls_logs.records if "skipping test-endpoint" in r.getMessage()]
    assert len(opened) == 1
    # One line per open period, not one per refused call -- a fleet run would
    # otherwise print one per repo -- but at WARNING, so it cannot be missed.
    assert len(skipped) == 1
    assert skipped[0].levelname == "WARNING"


def test_note_unavailable_reports_a_real_failure_but_not_a_repeat_of_the_open_circuit(gls_logs):
    note_unavailable("figma design ABC", ConnectionRefusedError("refused"))
    note_unavailable("figma design ABC", CircuitOpenError("mcp:figma", 42.0))

    messages = [r.getMessage() for r in gls_logs.records]
    assert any("figma design ABC unavailable: refused" in m for m in messages)
    assert not any("circuit open for mcp:figma" in m for m in messages)


# --- keys ------------------------------------------------------------------

def test_endpoint_key_keeps_credentials_out_of_the_key_and_the_log():
    key = endpoint_key("mcp", "https://user:s3cret@mcp.endpoint.invalid:8443/v1/mcp?token=abc")
    assert key == "mcp:https://mcp.endpoint.invalid:8443"
    assert "s3cret" not in key and "token" not in key


def test_breaker_registry_shares_state_across_freshly_built_clients():
    reset_breakers()
    first = breaker_for("shared", threshold=2, attempts=1)
    # A connector/provider rebuilt mid-run must find the SAME health record --
    # instance-owned state would reset the count and never open.
    assert breaker_for("shared") is first
    reset_breakers()
    assert breaker_for("shared") is not first


# --- the regression this design exists to prevent --------------------------

def test_unpulled_ollama_model_keeps_its_actionable_message_forever(monkeypatch):
    """A 404 "model not found" is a rejected request, not a sick endpoint.

    If it counted toward the breaker, the fourth call would swap the one message
    that tells the user what to do (``ollama pull ...``) for "circuit open".
    """
    error = 'model "nomic-embed-text" not found, try pulling it first'
    body = json.dumps({"error": error}).encode()

    def fake_post(url, payload, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(body))

    monkeypatch.setattr(emb_ollama, "post_json", fake_post)
    embedder = emb_ollama.OllamaEmbedder(model="nomic-embed-text", base_url="http://x:11434")

    for _ in range(5):
        with pytest.raises(RuntimeError) as exc:
            embedder.embed(["alpha"])
        assert "ollama pull nomic-embed-text" in str(exc.value)

    key = endpoint_key("embeddings:ollama", "http://x:11434")
    assert breaker_for(key).state == "closed"


def test_mcp_server_key_identifies_a_stdio_bridge_by_its_target(monkeypatch):
    # `npx -y mcp-remote@latest <url>` -- two sources bridging to different hosts
    # must not share one health record, and the key must stay printable.
    figma = server_key("npx", ["-y", "mcp-remote@latest", "https://mcp.figma.invalid/v1"], None)
    other = server_key("npx", ["-y", "mcp-remote@latest", "https://mcp.other.invalid/v1"], None)
    assert figma != other
    # The readable half names the bridge and its target; the argv digest after
    # `#` is what separates two bridges the readable half cannot tell apart
    # (same host, different path or different credential in the argv).
    assert figma.startswith("mcp:npx:https://mcp.figma.invalid#")


def test_dead_mcp_server_is_skipped_after_three_calls_not_retried_per_design(
        monkeypatch, gls_logs, no_sleep):
    """The D-8 scenario, end to end through a real connector.

    A fleet run asks one Figma MCP about design after design. Today each call
    waits out the full timeout; here the breaker inside ``call_tool`` writes the
    server off after three, and the connector still returns ``None`` (its
    documented best-effort contract) with the reason on screen.
    """
    dialled = []

    async def unreachable(*_a, **_k):
        dialled.append(1)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(mcp_client, "_acall", unreachable)
    connector = FigmaConnector("figma", mcp_url="https://mcp.figma.invalid/v1", timeout=120)

    for _ in range(3):
        assert connector.fetch_metadata("FILEKEY") is None
    # A refused connection failed fast, so each of the three calls got one retry
    # (see is_retryable) -- three *failures*, six dial attempts.
    assert len(dialled) == 6

    for _ in range(20):
        assert connector.fetch_metadata("FILEKEY") is None
    assert len(dialled) == 6, "a written-off MCP server must not be dialled again"

    messages = [r.getMessage() for r in gls_logs.records]
    assert any("circuit OPEN" in m for m in messages)
    # `mcp_url` here reaches the server through the `npx mcp-remote` stdio bridge,
    # so the key names the bridge and its target -- and carries no credential.
    assert any("skipping mcp:npx:https://mcp.figma.invalid" in m for m in messages)
    assert any("figma design FILEKEY unavailable" in m for m in messages), \
        "a swallowed failure must still say why, or it reads as 'no metadata'"


def test_unreachable_ollama_llm_opens_after_three_calls(monkeypatch, no_sleep):
    calls = []

    def fake_post(url, payload, timeout):
        calls.append(url)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(llm_ollama, "post_json", fake_post)
    client = llm_ollama.OllamaLlm(model="llama3.1", base_url="http://x:11434")

    for _ in range(3):
        with pytest.raises(ConnectionRefusedError):
            client.generate("hello")
    before = len(calls)

    with pytest.raises(CircuitOpenError):
        client.generate("hello")
    assert len(calls) == before, "the fourth generate() must not reach the daemon"


def _called_process_error(stderr):
    return subprocess.CalledProcessError(
        1, ["glab", "api", "projects/x/merge_requests"], output="", stderr=stderr)


def test_describe_surfaces_the_child_processes_own_stderr():
    """A failed CLI call has to say which failure it was.

    `str(CalledProcessError)` is only "Command '[...]' returned non-zero exit
    status 1", so a DNS failure, a 401 from an expired token and a 404 for a
    moved project all read identically -- the one line that distinguishes them
    is on the child's stderr, which was being thrown away.
    """
    unauthorized = describe(_called_process_error("401 Unauthorized (HTTP 401)\n"))
    not_found = describe(_called_process_error("404 Not Found (HTTP 404)\n"))

    assert "401" in unauthorized
    assert "404" in not_found
    assert unauthorized != not_found, "an invalid token must not read like a missing project"


def test_describe_keeps_working_when_there_is_no_child_stderr():
    assert describe(ConnectionRefusedError("connection refused")) == "connection refused"
    assert describe(_called_process_error("")) == describe(_called_process_error(None))


def test_note_unavailable_counts_swallowed_calls_so_callers_can_judge_the_run():
    """Connector calls are contractually non-raising, so a command cannot see a
    dead source through a try/except. Counting the write-offs is what lets a run
    that fetched nothing because everything failed avoid reporting success."""
    reset_breakers()
    before = degraded_calls()
    note_unavailable("gitlab (glab api)", _called_process_error("401 Unauthorized\n"))
    note_unavailable("gitlab (glab api)", CircuitOpenError("glab-api", 42.0))
    assert degraded_calls() - before == 2, "a refused call produced nothing too"
