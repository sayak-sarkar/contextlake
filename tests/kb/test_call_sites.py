"""Call-site provenance on the callers/callees verbs.

The graph always stored the call site: every edge carries
``Provenance.source_line``. The response models discarded it, so "who calls X"
answered with the CALLER'S DEFINITION line and the reader had to grep the body to
find the call. These tests pin the fix, and one of them is deliberately a negative
assertion -- nothing in the suite previously forbade returning the definition line,
which is why the defect survived.
"""

import asyncio
from datetime import date

import pytest
from mcp import Client

from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore

# The caller's definition spans lines 10-40 and it calls the target TWICE, from
# lines 20 and 30. Every number here is distinct on purpose: if the code returned
# the definition line, or the wrong edge's line, or collapsed the two sites, the
# assertions below can tell which of those happened.
_CALLER_DEF_LINE = 10
_CALL_LINES = (20, 30)
_PROV_FILE = "app.cpp"


def _seed(store):
    store.upsert_nodes("team/svc", [
        Node(id="caller", repo="team/svc", kind="function", name="Driver",
             file=_PROV_FILE, line_start=_CALLER_DEF_LINE, line_end=40),
        Node(id="target", repo="team/svc", kind="function", name="Encode",
             file="codec.cpp", line_start=100, line_end=120),
        # A second, single-site caller, so the "sites == distinct callers" case is
        # covered too and the note stays silent there.
        Node(id="other", repo="team/svc", kind="function", name="Once",
             file="other.cpp", line_start=1, line_end=9),
    ])
    store.upsert_edges("team/svc", [
        Edge(src="caller", dst="target", relation="calls",
             confidence=Confidence.EXTRACTED,
             provenance=Provenance(source_file=_PROV_FILE, source_line=line,
                                   verified_at=date(2026, 8, 10)))
        for line in _CALL_LINES
    ] + [
        Edge(src="other", dst="target", relation="calls",
             confidence=Confidence.EXTRACTED,
             provenance=Provenance(source_file="other.cpp", source_line=4,
                                   verified_at=date(2026, 8, 10))),
    ])


@pytest.fixture
def server(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    _seed(s)
    yield build_server(s)
    s.close()


def _call(server, tool, args):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(go()).structured_content


def test_find_callers_reports_the_call_site_not_the_definition(server):
    res = _call(server, "find_callers", {"node_id": "target"})
    from_caller = [n for n in res["nodes"] if n["id"] == "caller"]

    assert {n["call_line"] for n in from_caller} == set(_CALL_LINES)
    assert all(n["call_file"] == _PROV_FILE for n in from_caller)


def test_call_line_is_not_the_callers_definition_line(server):
    """The assertion that would have caught the original defect.

    Kept separate from the test above because it is the one that fails on the old
    behaviour: the previous code returned the caller node untouched, so `call_line`
    would be absent and the only line on the object was the definition's.
    """
    res = _call(server, "find_callers", {"node_id": "target"})
    from_caller = [n for n in res["nodes"] if n["id"] == "caller"]

    for n in from_caller:
        assert n["line_start"] == _CALLER_DEF_LINE      # definition, still reported
        assert n["call_line"] != n["line_start"]        # and the call site is not it
        assert n["call_line"] is not None


def test_one_entry_per_call_site_not_per_caller(server):
    """Two calls from one function are two answers, and the note says so."""
    res = _call(server, "find_callers", {"node_id": "target"})

    assert res["total"] == 3                                    # 3 call sites
    assert len([n for n in res["nodes"] if n["id"] == "caller"]) == 2
    # 3 sites, 2 distinct callers -- the note must disclose the difference so the
    # count cannot be read as a fan-in of 3.
    assert "3 call sites" in res["note"]
    assert "2 distinct" in res["note"]


def test_note_stays_silent_when_sites_equal_callers(server):
    """No boilerplate when there is nothing to disclose."""
    res = _call(server, "find_callees", {"node_id": "other"})

    assert res["total"] == 1
    assert res["note"] is None


def test_find_callees_mirrors_and_agrees_on_the_line(server):
    """A calls B: find_callees(A) and find_callers(B) must cite the same lines."""
    callees = _call(server, "find_callees", {"node_id": "caller"})
    callers = _call(server, "find_callers", {"node_id": "target"})

    assert {n["id"] for n in callees["nodes"]} == {"target"}
    callee_lines = sorted(n["call_line"] for n in callees["nodes"])
    caller_lines = sorted(n["call_line"] for n in callers["nodes"]
                          if n["id"] == "caller")
    assert callee_lines == caller_lines == sorted(_CALL_LINES)


def test_get_neighbors_edges_carry_the_line_too(server):
    """The same field was missing from EdgeOut, so every edge-returning verb cited
    a file with no line."""
    res = _call(server, "get_neighbors", {"node_id": "target", "relation": "calls"})

    lines = sorted(e["source_line"] for e in res["edges"])
    assert lines == [4, 20, 30]
    assert all(e["source_file"] for e in res["edges"])


def test_find_callees_distinguishes_unknown_symbol_from_no_calls(server):
    """A leaf function calling nothing and a symbol that does not exist both return
    an empty list; only the note separates them."""
    unknown = _call(server, "find_callees", {"node_id": "no_such_symbol"})
    leaf = _call(server, "find_callees", {"node_id": "target"})

    assert unknown["nodes"] == [] and "No indexed symbol" in unknown["note"]
    assert leaf["nodes"] == [] and leaf["note"] is None
