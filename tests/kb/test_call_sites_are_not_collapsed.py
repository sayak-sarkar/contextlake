"""One `calls` edge per call SITE, all the way to the answering verb.

Two places used to say the parser collapsed calls to one edge per
(caller, callee) pair, and both were wrong: the parser builds that stream with
``per_site=True``. Prose disagreeing with code is a defect on its own, but the
reason it survived is that nothing asserted the behaviour end to end. The parser
side had a test; the claim that mattered to a user, "if I call this from three
lines do I get three citations", did not.
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

import pytest

pytest.importorskip("tree_sitter")

from contextlake.kb.parse import index_repo_dir  # noqa: E402


def _repo_calling_three_times():
    tmp = tempfile.mkdtemp()
    root = pathlib.Path(tmp)
    (root / "m.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    a = helper()\n"
        "    b = helper()\n"
        "    c = helper()\n"
        "    return a + b + c\n",
        encoding="utf-8")
    subprocess.run(["git", "init", "-q", tmp], check=True)
    return tmp


def test_three_invocations_from_one_caller_make_three_call_edges():
    """The behaviour the docstrings got wrong. A per-pair graph gives 1 here."""
    shard = index_repo_dir(_repo_calling_three_times(), "probe")
    calls = [e for e in shard.edges if e.relation == "calls"]

    assert len(calls) == 3, (
        f"expected one edge per call site, got {len(calls)}. A collapse to one "
        f"edge per (caller, callee) pair is what the server docstrings used to "
        f"claim, and it discards the second and third call from a function.")

    # All three are the same pair: this is per-SITE, not three different callers.
    assert len({(e.src, e.dst) for e in calls}) == 1


def test_the_distinct_caller_count_differs_from_the_entry_count():
    """`find_callers`'s note reports both so "42 calls from 6 callers" cannot
    read as "42 callers". That note only means anything when the two numbers
    can differ, which is exactly what per-site edges made possible."""
    shard = index_repo_dir(_repo_calling_three_times(), "probe")
    calls = [e for e in shard.edges if e.relation == "calls"]

    entries = len(calls)
    distinct_callers = len({e.src for e in calls})

    assert entries == 3
    assert distinct_callers == 1
    assert entries != distinct_callers, (
        "if these can never differ, the note that distinguishes them is dead text")
