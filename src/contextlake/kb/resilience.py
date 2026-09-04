"""Circuit breaking + jittered retry for the knowledge layer's network calls.

The mirror tier has had :func:`contextlake.core.retry_with_backoff` since the
beginning; the knowledge layer's network paths -- the connectors (Atlassian,
Figma, Slack, GitLab, and the generic MCP query path) and the model providers
(Ollama, OpenAI, Anthropic) -- had nothing but a timeout. That is the whole cost
model of a fleet run against a sick endpoint: a 120s MCP timeout paid *once per
call*, so 480 repos x one dead source is sixteen hours of waiting to learn what
the first three calls already proved. A breaker turns that into "three calls,
then skip until it might have recovered".

Two knobs, deliberately separate:

* :class:`CircuitBreaker` -- consecutive *endpoint* failures open the circuit;
  while open, calls are refused instantly with :class:`CircuitOpenError`; after
  ``cooldown`` one call is let through as a probe (half-open) and its outcome
  decides whether the circuit closes again or the cooldown restarts.
* the retry loop itself, which is *not* reimplemented here -- it is
  ``core.retry_with_backoff``, called with this tier's own ``is_transient``
  predicate. Layering runs core (stdlib only) -> kb (optional extra), so kb may
  import core; the reverse would break the offline/no-kb guarantee that
  ``tests/kb/test_offline_boundary.py`` pins.

Two predicates rather than one, because "retry this" and "the endpoint is sick"
are different questions:

* :func:`is_endpoint_failure` decides what the *breaker* counts. A rejected
  request -- Ollama's 404 "model not found", a 401 -- says nothing about the
  endpoint's health, and counting it would replace an actionable
  ``run 'ollama pull X'`` message with "circuit open" on the fourth call.
* :func:`is_retryable` decides what the *retry loop* repeats. A timeout is an
  endpoint failure but is never retried: the call already spent its full timeout
  budget, so a retry doubles exactly the stall this module exists to remove.

Failure stays loud. Every transition is logged once, and a refused call raises
rather than returning an empty result that reads like "nothing found" -- see
:func:`note_unavailable` for the best-effort call sites that must swallow.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse

from ..core import classify_error, retry_with_backoff
from ..logging_setup import get_logger

__all__ = [
    "CircuitBreaker", "CircuitOpenError", "breaker_for", "degraded_calls", "describe",
    "endpoint_key", "find_in_chain", "is_endpoint_failure", "is_retryable",
    "note_unavailable", "reset_breakers",
]

# Three strikes before a source is written off: enough that a single blip or a
# server restart mid-run doesn't disable a working connector, few enough that a
# fleet run pays the timeout a handful of times instead of once per repo.
DEFAULT_THRESHOLD = 3
# Long enough to outlast a restart or a rate-limit window, short enough that a
# long run recovers by itself rather than needing to be re-launched.
DEFAULT_COOLDOWN = 60.0
# One retry, not three: see the module docstring on why timeouts aren't retried
# at all -- what this buys is the fast-failing case (connection refused mid
# restart, a 503), where a second attempt costs a quarter of a second.
DEFAULT_ATTEMPTS = 2
DEFAULT_BACKOFF_INITIAL = 0.25
DEFAULT_BACKOFF_MAX = 5.0

# Status codes that name a *transient* server-side condition rather than a
# rejected request. 429/503 say "try again" outright; 408 is the server giving up
# on a slow request. Other 5xx count against the endpoint's health (see
# `is_endpoint_failure`) but are not repeated: a bare 500 from a model endpoint
# is usually deterministic for the request that caused it.
_RETRY_STATUS = frozenset({429, 503})
_UNHEALTHY_STATUS = frozenset({408, 429})

def _optional_exception_types(attribute: str) -> tuple[type, ...]:
    """The named exception class from every httpx flavour that is installed.

    httpx is the transport under the MCP streamable-HTTP client, and its errors
    are plain ``Exception`` subclasses, not ``OSError``. Without them in the
    tuples below, a refused connection to a hosted MCP server reads as "not the
    endpoint's fault" and the breaker never opens. Measured on this build: five
    calls to a closed port, five dials, ``failures=0``.

    Resolved at import rather than on first use, because the tuples below are
    built once: a type added after they are built is a type no predicate ever
    tests, which is a fix that ships and does nothing.

    Two module names because ``mcp>=2.0`` depends on the ``httpx2`` fork while
    other callers still have ``httpx``. Both are optional to this module, so one
    that is absent contributes nothing rather than breaking the import.
    """
    found = []
    for module_name in ("httpx", "httpx2"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


# `TransportError` is httpx's base for everything that failed below the HTTP
# response: connect, read, write, pool, proxy, protocol. `HTTPStatusError` is
# deliberately not under it, so a rejected request keeps its own message instead
# of turning into "circuit open".
_HTTPX_TRANSPORT_TYPES = _optional_exception_types("TransportError")
_HTTPX_TIMEOUT_TYPES = _optional_exception_types("TimeoutException")

# Timeout spellings across the paths this module guards: urllib/sockets, the MCP
# client's `asyncio.wait_for`, httpx, and the `glab` subprocess. Listed as a
# tuple because they are not all one class: 3.10 aliases several onto the builtin
# TimeoutError, but subprocess.TimeoutExpired stays distinct at every version.
_TIMEOUT_TYPES = (
    socket.timeout, TimeoutError, asyncio.TimeoutError, subprocess.TimeoutExpired,
    *_HTTPX_TIMEOUT_TYPES,
)

# Exception types both predicates below can actually read. Anything else has to
# fall back to classifying its message text.
_TRANSPORT_TYPES = (urllib.error.HTTPError, urllib.error.URLError, OSError,
                    *_HTTPX_TRANSPORT_TYPES, *_TIMEOUT_TYPES)

# JSON-RPC error codes the MCP SDK raises when the *connection* failed rather
# than the request being refused. A stdio MCP server that starts and then exits
# arrives as `MCPError(-32000, "Connection closed")` two ExceptionGroups deep,
# and nothing above can read it: that class is a plain Exception. Measured on
# this build before this existed: four spawn-and-die calls, four dials,
# `failures=0`, circuit closed.
#
# Matched on the numeric code, never on the message, and never on an exception
# merely having a `code`: `urllib.error.HTTPError.code` is an HTTP status and
# would collide with these values' own numbering. The numbers are wire constants
# from the MCP spec. The SDK's class name for them moved between majors
# (`McpError` -> `MCPError`) and the numbers did not, which is why they are
# written out here rather than imported.
_MCP_CONNECTION_CLOSED = -32000
_MCP_REQUEST_TIMEOUT = -32001
_MCP_ENDPOINT_CODES = frozenset({_MCP_CONNECTION_CLOSED, _MCP_REQUEST_TIMEOUT})


def _chain(exc: BaseException):
    """Every exception inside ``exc``: itself, group members, ``raise ... from``.

    Breadth-first, so the outermost real cause wins, and cycle-safe.
    ``__context__`` is deliberately not followed: an incidental exception that
    merely happened to be in flight says nothing about this call.

    A group is detected by its ``exceptions`` attribute rather than by
    ``isinstance(..., BaseExceptionGroup)``: that builtin arrived in 3.11 and
    this package supports 3.10, where anyio raises the ``exceptiongroup``
    backport's class instead.
    """
    seen = {id(exc)}
    queue = [exc]
    while queue:
        current = queue.pop(0)
        yield current
        nested = list(getattr(current, "exceptions", None) or ())
        if current.__cause__ is not None:
            nested.append(current.__cause__)
        for candidate in nested:
            if id(candidate) not in seen:
                seen.add(id(candidate))
                queue.append(candidate)


def find_in_chain(exc: BaseException, types) -> BaseException | None:
    """The first exception of ``types`` inside ``exc``'s wrappers, or ``None``.

    Public because ``isinstance`` is the wrong test on any exception that came
    out of the MCP client. An :class:`~.mcp_client.McpToolError` -- the server
    saying no -- arrives from a real stdio server inside a *doubly nested*
    ``ExceptionGroup``, so a caller asking ``isinstance(e, McpToolError)``
    silently never matches and its whole branch is dead code that passes every
    test written against a directly-raised fake.
    """
    for current in _chain(exc):
        if isinstance(current, types):
            return current
    return None


def _leaf(exc: BaseException) -> BaseException:
    """The first exception inside ``exc`` that is not itself a group wrapper."""
    for candidate in _chain(exc):
        if not getattr(candidate, "exceptions", None):
            return candidate
    return exc


def _unwrap(exc: BaseException) -> BaseException:
    """The transport-level exception inside a wrapper, or ``exc`` unchanged.

    The MCP client runs under anyio task groups, which re-raise whatever went
    wrong as a (sometimes doubly nested) ``ExceptionGroup`` whose ``str()`` is
    the useless "unhandled errors in a TaskGroup (1 sub-exception)". Measured
    against a blackholed synthetic MCP server before this existed: the real
    ``TimeoutError`` was two groups deep, every failure classified as "not the
    endpoint's fault", and the circuit never opened at all -- i.e. the whole
    feature silently did nothing on the exact path it was built for.
    """
    return find_in_chain(exc, _TRANSPORT_TYPES) or exc


def _mcp_connection_code(exc: BaseException) -> int | None:
    """The MCP connection-level JSON-RPC code inside ``exc``, or ``None``.

    Skips anything already readable as transport-shaped, so an ``HTTPError``
    whose ``code`` is an HTTP status is never read as a JSON-RPC one.
    """
    for current in _chain(exc):
        if isinstance(current, _TRANSPORT_TYPES):
            continue
        code = getattr(current, "code", None)
        if type(code) is int and code in _MCP_ENDPOINT_CODES:
            return code
    return None


class CircuitOpenError(RuntimeError):
    """Raised instead of making a call the breaker has written off for now.

    Deliberately an exception and not a ``None``/empty return: the call sites
    this guards are best-effort and would otherwise turn "we refused to ask"
    into "the source had nothing", which is the failure mode that makes a
    fleet-wide outage look like a clean run.
    """

    def __init__(self, key: str, retry_in: float):
        self.key = key
        self.retry_in = retry_in
        super().__init__(
            f"circuit open for {key}: not calling it again for {retry_in:.0f}s after "
            "repeated failures (results from this source will be incomplete)"
        )

    def __reduce__(self):
        """Rebuild from the two values ``__init__`` needs.

        The default ``cls(*self.args)`` is one argument short and raises
        TypeError. See ``kb.parse.RepoTooLarge.__reduce__``.
        """
        return (self.__class__, (self.key, self.retry_in))


def _timed_out(exc: BaseException) -> bool:
    """Whether ``exc`` is (or wraps) a timeout rather than a fast failure."""
    if isinstance(exc, _TIMEOUT_TYPES):
        return True
    # urllib reports a socket timeout as URLError(reason=timeout), which is not
    # a wrapper `_unwrap` can see through (the reason is an attribute, not a
    # cause), so it is checked here instead.
    reason = getattr(exc, "reason", None)
    return isinstance(reason, _TIMEOUT_TYPES)


def _child_stderr(exc: BaseException) -> str:
    """The child process's own error text, if ``exc`` carries any.

    ``CalledProcessError`` and ``TimeoutExpired`` both capture stderr when the
    call used ``capture_output``, but neither puts it in ``str(exc)``. Decoded
    here because the attribute is bytes unless the call passed ``text=True``.
    """
    raw = getattr(exc, "stderr", None)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    return raw.strip() if isinstance(raw, str) else ""


def describe(exc: BaseException) -> str:
    """A log-ready reason for ``exc``, seeing through the task-group wrappers.

    Without this every MCP failure reads "unhandled errors in a TaskGroup
    (1 sub-exception)", which tells a user nothing about whether the server was
    slow, refused, or rejected the request.

    A failed child process needs the same treatment for the same reason:
    ``str(CalledProcessError)`` is only "Command '[...]' returned non-zero exit
    status 1", so a DNS failure, a 401 from an expired token and a 404 for a
    project that moved all read identically -- and the one line that says which
    was thrown away with the child's stderr. It is appended instead. Truncated,
    because a CLI can be verbose and this is one log line.

    A wrapper carrying something that is *not* transport-shaped falls back to the
    innermost real exception rather than the wrapper. Without that, a tool call
    the server rejected reads as "unhandled errors in a TaskGroup", which is the
    one line the reader needed replaced by no line at all.
    """
    inner = find_in_chain(exc, _TRANSPORT_TYPES) or _leaf(exc)
    reason = str(inner).strip() or type(inner).__name__
    stderr = _child_stderr(inner)
    if not stderr:
        return reason
    first = stderr.splitlines()[0][:200]
    return f"{reason}: {first}" if first not in reason else reason


def is_endpoint_failure(exc: BaseException) -> bool:
    """Whether ``exc`` means *the endpoint* is unhealthy (so the breaker counts it).

    True for anything transport-shaped (connection refused/reset, DNS, TLS,
    timeout) and for the server-side status codes. False for a request the server
    understood and rejected -- a 404 for an unpulled Ollama model, a 401 for a
    missing key -- because no amount of skipping calls fixes those, and hiding
    their message behind "circuit open" would remove the one line that tells the
    user what to do.

    An MCP connection-level code (-32000 "Connection closed", -32001 request
    timeout) counts too. That is how a stdio server which spawned and then died
    reports itself, and it is the common shape of a down MCP server: the process
    starts, so nothing raises ``FileNotFoundError``, and the failure arrives as
    an SDK error two task-group wrappers deep.
    """
    if _mcp_connection_code(exc) is not None:
        return True
    exc = _unwrap(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in _UNHEALTHY_STATUS
    if isinstance(exc, _TRANSPORT_TYPES):
        return True
    # Text-classified failures (the `glab` CLI, MCP servers that report an error
    # as a message rather than an exception type) reuse the mirror tier's
    # classifier rather than growing a second one.
    return classify_error(str(exc)) in ("network", "timeout", "dns", "tls")


def is_retryable(exc: BaseException) -> bool:
    """Whether ``exc`` is worth one more immediate attempt.

    Only failures that failed *fast*: a connection refused by a restarting
    server, or an explicit 429/503 "come back". A timeout is excluded on purpose
    (see the module docstring): the attempt already consumed its whole timeout
    budget, and repeating it doubles the worst-case stall D-8 is about.

    An MCP request timeout (-32001) is excluded here for the same reason, by its
    code: it arrives as a plain SDK exception that no type test above can read,
    so without this row it would fall through to the text classifier and be
    repeated.
    """
    if _mcp_connection_code(exc) == _MCP_REQUEST_TIMEOUT:
        return False
    exc = _unwrap(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRY_STATUS
    if _timed_out(exc):
        return False
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return True
    return classify_error(str(exc)) == "network"


def endpoint_key(prefix: str, url: str | None) -> str:
    """A breaker key naming an endpoint by scheme/host/port only.

    Never the path, query or userinfo: a configured MCP or model URL can carry a
    token, and this string is printed in log lines. Two sources pointed at the
    same host deliberately share one breaker -- it is the same endpoint, and its
    health is one fact.
    """
    if not url:
        return prefix
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return prefix
    host = parts.hostname or ""
    if not host:
        return prefix
    port = f":{parts.port}" if parts.port else ""
    return f"{prefix}:{parts.scheme}://{host}{port}"


class CircuitBreaker:
    """Consecutive-failure breaker around one endpoint, with retry built in.

    States: *closed* (calls pass), *open* (calls raise :class:`CircuitOpenError`
    without touching the network), *half-open* (the cooldown has elapsed; exactly
    one call is let through to see whether the endpoint came back).

    ``clock`` is injectable so tests can cross the cooldown boundary without
    sleeping for real.
    """

    def __init__(self, key: str, *, threshold: int = DEFAULT_THRESHOLD,
                 cooldown: float = DEFAULT_COOLDOWN, attempts: int = DEFAULT_ATTEMPTS,
                 backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
                 backoff_max: float = DEFAULT_BACKOFF_MAX, clock=None):
        self.key = key
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown)
        self.attempts = max(1, int(attempts))
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False
        self._announced = False
        self._last_error = ""

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown:
            return "half-open"
        return "open"

    def call(self, fn, *args, **kwargs):
        """Run ``fn(*args, **kwargs)`` under the breaker, retrying transient failures.

        The call is handed to ``core.retry_with_backoff`` as a zero-argument
        ``functools.partial`` so the callee's own keyword arguments can never
        collide with the retry loop's (``max_retries`` and friends).
        """
        probing = self._enter()
        try:
            result = retry_with_backoff(
                functools.partial(fn, *args, **kwargs),
                max_retries=self.attempts,
                backoff_initial=self.backoff_initial,
                backoff_max=self.backoff_max,
                is_transient=is_retryable,
            )
        except Exception as e:
            self._record_failure(e)
            raise
        else:
            self._record_success()
            return result
        finally:
            if probing:
                # Belt and braces for a BaseException (Ctrl-C) escaping the two
                # branches above: a probe left in flight would wedge the breaker
                # open forever, since no later call could claim the probe slot.
                with self._lock:
                    self._probing = False

    def _enter(self) -> bool:
        """Admit the call, or raise. True when this call is the half-open probe."""
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return False
            if state == "half-open" and not self._probing:
                self._probing = True
                return True
            retry_in = max(0.0, self.cooldown - (self._clock() - (self._opened_at or 0.0)))
            announce = not self._announced
            self._announced = True
            last_error = self._last_error
        if announce:
            # Once per open period, not per refused call: a fleet run would
            # otherwise print one line per repo. WARNING, not DEBUG -- a run that
            # silently skipped a whole source is indistinguishable from a run
            # where that source genuinely had nothing to say.
            get_logger().warning(
                "resilience: skipping %s for %.0fs -- circuit open after %d consecutive "
                "failure(s) (%s); results from this source will be incomplete",
                self.key, retry_in, self.threshold, last_error or "unknown error")
        raise CircuitOpenError(self.key, retry_in)

    def _record_failure(self, exc: BaseException) -> None:
        if not is_endpoint_failure(exc):
            # A rejected request, not a sick endpoint: let the caller see its own
            # error untouched, and leave the failure count alone.
            with self._lock:
                self._probing = False
            return
        with self._lock:
            was_open = self._opened_at is not None  # i.e. this was the half-open probe
            self._probing = False
            self._failures += 1
            self._last_error = describe(exc)
            opening = was_open or self._failures >= self.threshold
            if opening:
                self._opened_at = self._clock()
                self._announced = False
            failures, last_error = self._failures, self._last_error
        if opening:
            get_logger().warning(
                "resilience: circuit OPEN for %s after %d consecutive failure(s) (%s) -- "
                "further calls are skipped for %.0fs",
                self.key, failures, last_error, self.cooldown)

    def _record_success(self) -> None:
        with self._lock:
            recovered = self._opened_at is not None
            self._failures = 0
            self._opened_at = None
            self._probing = False
            self._announced = False
        if recovered:
            get_logger().info("resilience: circuit closed for %s -- endpoint recovered",
                              self.key)


_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()
# Best-effort calls written off since the process started (see `degraded_calls`).
_degraded_calls = 0


def breaker_for(key: str, **kwargs) -> CircuitBreaker:
    """The process-wide breaker for ``key``, created on first use.

    Shared state is the point: connectors and provider clients are constructed
    per source (and sometimes per repo), so a breaker owned by an instance would
    forget everything between calls and never open. ``kwargs`` configure the
    breaker only when it is first created -- each endpoint is configured at
    exactly one call site, so first-caller-wins is unambiguous there.
    """
    with _registry_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key, **kwargs)
            _breakers[key] = breaker
        return breaker


def reset_breakers() -> None:
    """Forget every breaker. For tests -- breaker state is process-wide, so a
    test that trips one would otherwise short-circuit an unrelated later one."""
    global _degraded_calls
    with _registry_lock:
        _breakers.clear()
        _degraded_calls = 0


def degraded_calls() -> int:
    """How many best-effort calls have been written off so far this process.

    Logging a swallowed failure keeps it visible to a human reading the console,
    but a command still has to *decide* with it: a connect run whose every call
    was refused stored no links for a reason, and printing the same green
    "0 links stored" as a healthy run made an expired token indistinguishable
    from a repo with no open work. Callers snapshot this around their own run
    and compare, so the count stays a per-run measure despite being kept
    process-wide (the breakers it sits beside are process-wide too).
    """
    with _registry_lock:
        return _degraded_calls


def note_unavailable(what: str, exc: BaseException) -> None:
    """Log why a best-effort call produced nothing, so degrading stays visible.

    Several connector calls are documented as never raising (verification and
    enrichment must not break the association graph). Returning ``None``/``[]``
    *silently* is what makes an outage look like an empty result, so the reason
    is logged here instead. A refused call is the exception: :meth:`_enter`
    already announced the open circuit once, and repeating it per repo would
    bury the announcement it duplicates.

    Every call through here counts toward :func:`degraded_calls`, refused ones
    included -- a call the breaker skipped still produced nothing, and the
    caller's success/failure decision needs to know that.
    """
    global _degraded_calls
    with _registry_lock:
        _degraded_calls += 1
    if isinstance(exc, CircuitOpenError):
        return
    get_logger().warning("%s unavailable: %s", what, describe(exc))
