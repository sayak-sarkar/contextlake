"""Every exception this package raises must survive a pickle round-trip.

`kb index --workspace` runs `index_repo_dir` in a `ProcessPoolExecutor`. An
exception raised in a worker is pickled back to the parent and unpickled inside
the pool's manager thread, in `result_reader.recv()`. That thread cannot
attribute a failure to one future, so anything raised there breaks the WHOLE
executor: every healthy worker is sent SIGTERM and every pending future gets
`BrokenProcessPool`. One refused repository ended a 656-repo run in a minute.

Python rebuilds an exception as `cls(*self.args)`. Each class below formats a
message, passes that one string to `Exception.__init__`, and keeps the real
values on `self`, so `self.args` is a 1-tuple and the rebuild is short of
arguments. Every one of them defines `__reduce__` for that reason.

`pickle.dumps` succeeds on all of them, which is why this went unnoticed: the
send side is fine and only the parent detonates. Assert on the round trip.
"""

import importlib
import inspect
import pickle
import pkgutil

import pytest

import contextlake
from contextlake.kb.lock import StoreBusy
from contextlake.kb.mcp_client import McpToolError
from contextlake.kb.parse import GrammarNotInstalled, RepoTooLarge
from contextlake.kb.resilience import CircuitOpenError
from contextlake.schedule.runlock import RunBusy

# One sample per exception, and the attribute that proves the values came back
# rather than a bare message. Written out rather than generated: four of these
# cannot be built from a naive argument filler, and a discovery test that skips
# what it cannot construct passes while testing nothing.
SAMPLES = {
    RepoTooLarge: (
        RepoTooLarge("team/svc", 6_600_000_000, 3_000_000_000, {"xml": 673.0, "code": 188.0}),
        "breakdown",
    ),
    GrammarNotInstalled: (
        GrammarNotInstalled("ruby", "tree_sitter_ruby", "ruby"),
        "module",
    ),
    McpToolError: (McpToolError("search", "no such index"), "detail"),
    CircuitOpenError: (CircuitOpenError("example.test", 30.0), "retry_in"),
    StoreBusy: (StoreBusy({"pid": 4321, "command": "kb index"}), "holder"),
    RunBusy: (RunBusy({"pid": 4321, "job": "nightly"}), "holder"),
}


def _custom_exceptions():
    """Every exception class this package defines with its own ``__init__``.

    Discovery rather than a written list, so adding an exception cannot quietly
    escape the guard. A class that inherits ``__init__`` unchanged rebuilds from
    ``args`` correctly and is not at risk.
    """
    found = set()
    for mod_info in pkgutil.walk_packages(contextlake.__path__, "contextlake."):
        try:
            mod = importlib.import_module(mod_info.name)
        except Exception:  # noqa: BLE001 - an optional extra being absent is not a failure here
            continue
        for obj in vars(mod).values():
            if (inspect.isclass(obj) and issubclass(obj, BaseException)
                    and obj.__module__ == mod.__name__ and "__init__" in vars(obj)):
                found.add(obj)
    return found


def test_every_custom_exception_has_a_sample():
    """A new exception with its own __init__ must be added to SAMPLES.

    Without this, `test_custom_exceptions_survive_a_pickle_round_trip` only
    covers what someone remembered to list, and the next exception ships with
    the same defect.
    """
    discovered = _custom_exceptions()
    missing = discovered - set(SAMPLES)
    assert not missing, (
        "these exceptions define __init__ and have no sample in SAMPLES, so nothing "
        f"checks that they can cross a process boundary: {sorted(c.__qualname__ for c in missing)}"
    )


@pytest.mark.parametrize("cls", list(SAMPLES), ids=lambda c: c.__qualname__)
def test_custom_exceptions_survive_a_pickle_round_trip(cls):
    original, attr = SAMPLES[cls]
    # noqa: S301 - the payload is this test's own object, dumped one line earlier.
    # Round-tripping it is the assertion; there is no untrusted input here.
    restored = pickle.loads(pickle.dumps(original))  # noqa: S301
    assert type(restored) is cls
    # The attribute, not just the message: a reduce that rebuilt the class from
    # its formatted string would still raise the right type and carry nothing.
    assert getattr(restored, attr) == getattr(original, attr)
    assert str(restored) == str(original)
