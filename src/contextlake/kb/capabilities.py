"""What an MCP server said it offers, remembered for the length of one run.

**The contract, in one sentence: this answers "which tool names did this MCP
server advertise when we asked it during *this* run, and did the ask even
work", and nothing else.** It never answers "is the tool there now". A tool
list is true when written and silently wrong the moment the provider renames a
tool, so a reader that treats it as current state gets a confident wrong answer
instead of a stale one it can see is stale.

Three things end a record: it ages past :data:`MAX_AGE_SECONDS`, the transport
identity changes (the record is keyed by :func:`mcp_client.server_key`, so a
source repointed at another host, or spawned with different arguments or a
different environment, reads as no record), or :func:`reset_run` is called.

**No command in ``src/`` calls :func:`reset_run` today.** The record is
therefore scoped to the process, and the age bound is the only one that runs.
That is what makes "for the length of one run" true here rather than true by
luck: the one path that reaches this module is the short-lived ``kb enrich``,
where process and run are the same thing, and the age bound holds the claim up
anyway in a caller where they are not. A record kept for the life of a *process*
with no age bound would, in a long-lived one, answer a question about a provider
from an hour ago and never re-ask, which is the stale answer this module exists
to refuse to give.

``reset_run`` is the hook a long-lived caller (a server, a test) uses to state
the run boundary itself. It is kept, and named as uncalled rather than described
as a mechanism that runs, because a documented mechanism with no caller reads
as a bound that is in force. ``tests/kb/test_capabilities.py`` pins the two
directions: no caller and this paragraph, or a caller and no paragraph.

It is consulted **only on the failure path**. Nothing reads it to pick a tool.
A cache read to choose what to call becomes the source of truth at call time,
and the configured ``tool`` is the only legal value anyway: substituting a
different one silently changes what a source means.

The record lives for the run and is not written to disk. Persisting it, so
``kb doctor`` could call a probe old, needs a store directory that this path
does not have: ``connectors/enrich.search_source`` hands the connector a
``SourceCfg`` and nothing else, and every would-be reader of a persisted file
(``cmds/doctor.py``, ``source_cmd.py``) is a separate change.

**The latch is the point.** ``kb enrich`` calls the tool once per repo, so a
renamed tool fails once per repo. Re-probing on each failure is a network storm
aimed at the provider that has just broken, on a fleet 480 times over. The
first failure per server re-probes; every later failure reads the recorded
answer. It reuses the breaker's *key*, not the breaker itself: a
:class:`~.resilience.CircuitBreaker` resets on any success and expires on a
cooldown, and this record must survive both.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

from pydantic import ValidationError

from ..logging_setup import get_logger
from .mcp_client import McpProbeShapeError, ToolList, list_tools, server_key
from .resilience import CircuitOpenError, describe, find_in_chain, is_endpoint_failure

# What "the server answered, and the answer was unreadable" looks like when it
# reaches this module. ``ValidationError`` is the one that actually fires: the
# MCP SDK parses the ``tools/list`` frame into ``ListToolsResult`` before this
# package sees it, so a missing or wrongly-typed ``tools`` field never reaches
# ``mcp_client``'s own check. ``McpProbeShapeError`` covers the remaining case
# where the SDK hands back a result object with no ``tools`` at all.
_MALFORMED_TYPES = (ValidationError, McpProbeShapeError)

__all__ = [
    "MALFORMED", "MAX_AGE_SECONDS", "OK", "REJECTED", "UNREACHABLE",
    "explain_tool_failure", "probe_tools", "reprobe_once", "reset_run",
]

# The four answers a probe can give, reported separately because they need
# different things done about them: a server that never answered, one that
# answered "no", one that answered with a shape nothing can read, and one that
# worked. Collapsing them is how "we could not ask" comes to read as "it offers
# nothing", which is the claim this module exists to refuse to make.
OK = "ok"
UNREACHABLE = "unreachable"
REJECTED = "rejected"
MALFORMED = "malformed"

# How many advertised tool names one diagnostic line lists before it stops.
_NAMES_SHOWN = 12

# How long one answer stands. Long enough that a fleet enrich re-asks a server a
# handful of times at most, short enough that a provider which came back is not
# written off for the life of a long-running process. A judgement, not a
# measurement, which is why it is a named constant and not a literal buried in a
# comparison.
MAX_AGE_SECONDS = 300.0

ProbeResult = tuple[str, "ToolList | None", str]

# Injectable so a test can cross the age boundary without sleeping, the way
# `CircuitBreaker` takes a `clock`.
_clock = time.monotonic

_probed: dict[str, tuple[float, ProbeResult]] = {}
_explained: set[tuple[str, str, str]] = set()
_gates: dict[str, threading.Lock] = {}
_lock = threading.Lock()


def reset_run() -> None:
    """Forget this run's probes and diagnostics.

    **Nothing in ``src/`` calls this.** It is here for a caller whose process
    outlives one run and can say where the boundary is, and it is what keeps one
    test's latch out of the next test's count. The bound that actually runs
    today is :data:`MAX_AGE_SECONDS`; see the module docstring.
    """
    with _lock:
        _probed.clear()
        _explained.clear()
        _gates.clear()


def _safe_detail(key: str, url: str | None, exc: BaseException) -> str:
    """A log-ready reason naming the server by its scrubbed key, never its URL.

    ``describe`` sees through the doubly nested anyio ``ExceptionGroup`` whose
    ``str()`` is the useless "unhandled errors in a TaskGroup". The URL is then
    swapped back out for the key if the transport put it in the message: a
    hosted MCP URL can carry a token, which is why ``server_key`` scrubs path,
    query and userinfo in the first place.
    """
    reason = describe(exc)
    if url and url in reason:
        reason = reason.replace(url, key)
    return f"{key}: {reason}"


def probe_tools(
    *, command: str | None = None, args: Sequence[str] = (), url: str | None = None,
    env: dict | None = None, timeout: float = 8,
) -> ProbeResult:
    """Ask one MCP server what it offers. Never raises; returns which of the four.

    The outcome is decided by the exception's *type*, never by matching its
    text. A substring test on a message like "unknown tool" is the unanchored
    match this codebase has been bitten by before: it passes on wording nobody
    controls and fails the day the provider rewrites a sentence.

    ``timeout`` defaults to 8s, matching ``kb doctor``'s per-source bound rather
    than :func:`mcp_client.list_tools`'s 90s. This runs on a failure path, where
    the caller has already spent a full call timeout.

    **The classification is made with :func:`~.resilience.find_in_chain`, not
    with ``except`` clauses.** Both transports run under anyio task groups and
    re-raise whatever went wrong as an ``ExceptionGroup``, one or two deep. The
    object handed to an ``except`` clause is the group, so
    ``except ValidationError`` matches nothing a real server produces, and
    ``except McpProbeShapeError`` matched only a directly-raised stub. Measured
    on this build: a raw server answering ``tools/list`` with ``{"result": {}}``
    raises ``ExceptionGroup(ExceptionGroup(ValidationError))``, and
    ``isinstance`` on the outer object is False while ``find_in_chain`` finds
    the leaf.

    MALFORMED is tested before UNREACHABLE on purpose. An answer that arrived
    and could not be read is a fact about the answer, and it outranks anything
    :func:`~.resilience.is_endpoint_failure` may later decide about the same
    exception.
    """
    key = server_key(command, args, url, env=env)
    try:
        return OK, list_tools(command=command, args=args, timeout=timeout, env=env, url=url), ""
    except CircuitOpenError as e:
        # Raised by the breaker itself, unwrapped, before any transport ran.
        return UNREACHABLE, None, _safe_detail(key, url, e)
    except Exception as e:  # noqa: BLE001 - a probe reports, it does not raise
        if find_in_chain(e, _MALFORMED_TYPES) is not None:
            return MALFORMED, None, _safe_detail(key, url, e)
        outcome = UNREACHABLE if is_endpoint_failure(e) else REJECTED
        return outcome, None, _safe_detail(key, url, e)


def _recorded(key: str) -> ProbeResult | None:
    """This run's answer for ``key``, unless it has aged out. Caller holds ``_lock``."""
    entry = _probed.get(key)
    if entry is None:
        return None
    recorded_at, result = entry
    if _clock() - recorded_at < MAX_AGE_SECONDS:
        return result
    # Aged out: drop the answer and the "already said this" marks that went with
    # it, so a server that has changed since gets both a fresh probe and a fresh
    # line about it.
    del _probed[key]
    for mark in [m for m in _explained if m[2] == key]:
        _explained.discard(mark)
    return None


def reprobe_once(key: str, probe) -> ProbeResult:
    """Run ``probe`` the first time ``key`` asks this run; replay it after that.

    A failed probe is recorded too. Without that, a server that is down re-probes
    on every repo, which is the storm the latch exists to stop, at the moment the
    provider can least afford it.
    """
    with _lock:
        recorded = _recorded(key)
        if recorded is not None:
            return recorded
        gate = _gates.setdefault(key, threading.Lock())
    with gate:
        with _lock:
            recorded = _recorded(key)
        if recorded is not None:
            return recorded
        try:
            result: ProbeResult = probe()
        except Exception as e:  # noqa: BLE001 - a raising probe latches like a failing one
            result = (UNREACHABLE, None, describe(e))
        with _lock:
            _probed[key] = (_clock(), result)
        return result


def _log_once(key: tuple[str, str, str], message: str, *fields) -> None:
    """Warn once per (source, tool, server) this run, not once per repo."""
    with _lock:
        if key in _explained:
            return
        _explained.add(key)
    get_logger().warning(message, *fields)


def explain_tool_failure(
    *, source: str, tool: str, command: str | None = None, args: Sequence[str] = (),
    url: str | None = None, env: dict | None = None, timeout: float = 8,
) -> None:
    """Say, by name, why a tool call the server rejected keeps failing.

    Emits at most one warning per source, tool and server per run. Returns
    nothing: the outcome is available from :func:`probe_tools` for a caller that
    wants to branch on it, and a return value nobody reads is one that can drift
    from what was actually logged.

    A renamed tool is otherwise invisible: the connector degrades the way
    connectors are supposed to, the run reports its documents, and nothing
    anywhere says the tool moved. The claim is made from the probed name list
    and never from the error text. Two cases refuse to make it at all: a probe
    that did not work (it cannot know what the server offers) and a truncated
    list (page one cannot prove absence).
    """
    key = server_key(command, args, url, env=env)
    outcome, tools, detail = reprobe_once(
        key,
        lambda: probe_tools(command=command, args=args, url=url, env=env, timeout=timeout),
    )
    if outcome != OK or tools is None:
        _log_once(
            (source, tool, key), "source %r: the tool %r failed, and the server could not be "
            "re-asked which tools it offers (%s), so whether %r is still advertised is unknown",
            source, tool, detail, tool)
        return

    names = [t.name for t in tools.tools]
    if tool in names:
        return
    if tools.truncated:
        _log_once(
            (source, tool, key), "source %r: the tool %r failed and is not on the first page of "
            "this MCP server's advertised list, which is partial, so it may still exist",
            source, tool)
        return

    shown = ", ".join(names[:_NAMES_SHOWN]) or "no tools at all"
    more = f", +{len(names) - _NAMES_SHOWN} more" if len(names) > _NAMES_SHOWN else ""
    _log_once(
        (source, tool, key), "source %r: the tool %r it is configured to call is not in this MCP "
        "server's advertised list any more (the server now offers: %s%s). Set a new tool with "
        "`contextlake kb source add %s --set tool=NAME`",
        source, tool, shown, more, source)
