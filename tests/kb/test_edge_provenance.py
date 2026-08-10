"""Non-call verbs cite the edge they traversed (E1-S2-T2).

From the provenance audit: `find_dependents` told the caller "INFERRED from
manifests, verify against the cited file" and then did not cite the file; the
subclasses walk dropped where the inheritance is declared; `shortest_path` asserted
a route while citing none of its adjacencies.

These use `edge_file`/`edge_line` rather than `call_file`/`call_line`, because a
`depends_on` edge's provenance is a manifest declaration and an `inherits` edge's is
a base-class mention. Calling either a "call_line" would be a plausible-looking lie.
"""

import asyncio
from datetime import date

import pytest
from mcp import Client

from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import build_server
from contextlake.kb.store.sqlite_store import SqliteStore

_MANIFEST_LINE = 42
_INHERITS_LINE = 7
_CALL_LINE = 13


def _prov(f, ln):
    return Provenance(source_file=f, source_line=ln, verified_at=date(2026, 8, 11))


@pytest.fixture
def server(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("app", [
        Node(id="pkg", repo="app", kind="package", name="widgetlib"),
        Node(id="mani", repo="app", kind="file", name="package.json", file="package.json"),
        Node(id="Base", repo="app", kind="class", name="Base", file="b.h", line_start=3),
        Node(id="Derived", repo="app", kind="class", name="Derived", file="d.h", line_start=70),
        Node(id="A", repo="app", kind="function", name="A", file="a.cpp", line_start=10),
        Node(id="B", repo="app", kind="function", name="B", file="b.cpp", line_start=20),
    ])
    s.upsert_edges("app", [
        Edge(src="mani", dst="pkg", relation="depends_on", confidence=Confidence.INFERRED,
             provenance=_prov("package.json", _MANIFEST_LINE)),
        Edge(src="Derived", dst="Base", relation="inherits", confidence=Confidence.EXTRACTED,
             provenance=_prov("d.h", _INHERITS_LINE)),
        Edge(src="A", dst="B", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=_prov("a.cpp", _CALL_LINE)),
    ])
    yield build_server(s)
    s.close()


def _call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go()).structured_content


def test_find_dependents_cites_the_manifest_line_it_promises(server):
    """Its own note says "verify against the cited file". Now there is one."""
    n = _call(server, "find_dependents", {"package": "widgetlib"})["nodes"][0]
    assert n["edge_file"] == "package.json"
    assert n["edge_line"] == _MANIFEST_LINE


def test_find_dependents_does_not_pretend_a_manifest_is_a_call(server):
    """The naming test: a dependency declaration must not arrive as a `call_line`."""
    n = _call(server, "find_dependents", {"package": "widgetlib"})["nodes"][0]
    assert n["call_file"] is None
    assert n["call_line"] is None


def test_the_subclasses_walk_cites_where_inheritance_is_declared(server):
    n = _call(server, "ask", {"question": "what subclasses Base"})["nodes"][0]
    assert n["edge_file"] == "d.h"
    assert n["edge_line"] == _INHERITS_LINE
    assert n["edge_line"] != n["line_start"]      # not the subclass's own definition


def test_each_path_hop_cites_the_edge_that_makes_it_adjacent(server):
    res = _call(server, "shortest_path", {"src_id": "A", "dst_id": "B"})
    by_id = {n["id"]: n for n in res["nodes"]}
    assert by_id["B"]["edge_file"] == "a.cpp"
    assert by_id["B"]["edge_line"] == _CALL_LINE


def test_the_paths_seed_node_has_no_hop_to_cite(server):
    """The first node was not reached by an edge, so inventing provenance for it would
    be worse than leaving it empty."""
    res = _call(server, "shortest_path", {"src_id": "A", "dst_id": "B"})
    by_id = {n["id"]: n for n in res["nodes"]}
    assert by_id["A"]["edge_file"] is None
    assert by_id["A"]["edge_line"] is None


def test_the_call_verbs_still_use_the_call_fields(server):
    """No result should ever carry both pairs; each verb uses the one its relation
    matches."""
    n = _call(server, "find_callers", {"node_id": "B"})["nodes"][0]
    assert n["call_line"] == _CALL_LINE
    assert n["edge_file"] is None and n["edge_line"] is None
