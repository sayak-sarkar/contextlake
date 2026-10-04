"""Nodes reached through an edge carry that edge's confidence (W03).

The server's instructions tell agents that results are confidence-tagged and that
INFERRED/AMBIGUOUS ones need checking. `find_callers` and the verbs beside it sorted by
the edge's confidence and then returned the node without it, so a call site that was one
guess among several looked the same as one read straight from source.

Every value below is asserted by node id and call line, never as "the key is present".
A field that is always "EXTRACTED", or copied from the wrong edge, passes a presence
check and fails these.
"""

import asyncio
from datetime import date

import pytest
from mcp import Client

from contextlake.kb import server as server_mod
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.server import NodeOut, build_server
from contextlake.kb.store.sqlite_store import SqliteStore

EXTRACTED, INFERRED, AMBIGUOUS = (Confidence.EXTRACTED, Confidence.INFERRED,
                                  Confidence.AMBIGUOUS)


def _prov(f, ln):
    return Provenance(source_file=f, source_line=ln, verified_at=date(2026, 8, 11))


def _fn(nid, name, f):
    return Node(id=nid, repo="app", kind="function", name=name, file=f, line_start=1)


@pytest.fixture
def server(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    s.upsert_nodes("app", [
        _fn("target", "Target", "t.py"),
        _fn("sure", "Sure", "sure.py"),
        _fn("guess", "Guess", "guess.py"),
        _fn("mixed", "Mixed", "mixed.py"),
        _fn("mid", "Mid", "mid.py"),
        _fn("end", "End", "end.py"),
        Node(id="Base", repo="app", kind="class", name="Base", file="b.py", line_start=1),
        Node(id="Sub", repo="app", kind="class", name="Sub", file="c.py", line_start=1),
        Node(id="pkg", repo="app", kind="package", name="widgetlib"),
        Node(id="mani", repo="app", kind="file", name="package.json", file="package.json"),
    ])
    s.upsert_edges("app", [
        # Three callers of `target`: one read from source, one a guess, and one that
        # has BOTH, from two different lines (one entry per call site).
        Edge(src="sure", dst="target", relation="calls", confidence=EXTRACTED,
             provenance=_prov("sure.py", 5)),
        Edge(src="guess", dst="target", relation="calls", confidence=AMBIGUOUS,
             provenance=_prov("guess.py", 9)),
        Edge(src="mixed", dst="target", relation="calls", confidence=EXTRACTED,
             provenance=_prov("mixed.py", 20)),
        Edge(src="mixed", dst="target", relation="calls", confidence=AMBIGUOUS,
             provenance=_prov("mixed.py", 30)),
        # A two-hop route whose hops differ: guess -> mid is EXTRACTED, mid -> end is
        # AMBIGUOUS, so a label copied from the wrong hop is visible.
        Edge(src="guess", dst="mid", relation="calls", confidence=EXTRACTED,
             provenance=_prov("guess.py", 11)),
        Edge(src="mid", dst="end", relation="calls", confidence=AMBIGUOUS,
             provenance=_prov("mid.py", 7)),
        Edge(src="Sub", dst="Base", relation="inherits", confidence=INFERRED,
             provenance=_prov("c.py", 2)),
        Edge(src="mani", dst="pkg", relation="depends_on", confidence=INFERRED,
             provenance=_prov("package.json", 4)),
    ])
    yield build_server(s)
    s.close()


def _call(server, tool, args):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go()).structured_content


def _site_conf(nodes, line_key="call_line"):
    """{(node id, cited line): confidence}: one key per call site."""
    return {(n["id"], n[line_key]): n["confidence"] for n in nodes}


def test_find_callers_labels_each_call_site_with_its_own_edges_confidence(server):
    nodes = _call(server, "find_callers", {"node_id": "target"})["nodes"]
    assert _site_conf(nodes) == {
        ("sure", 5): "EXTRACTED",
        ("guess", 9): "AMBIGUOUS",
        ("mixed", 20): "EXTRACTED",
        ("mixed", 30): "AMBIGUOUS",
    }


def test_an_ambiguous_caller_no_longer_reads_like_an_extracted_one(server):
    """The reported harm: with the field absent the two entries differed only by id."""
    by_id = {n["id"]: n for n in
             _call(server, "find_callers", {"node_id": "target"})["nodes"]
             if n["id"] in ("sure", "guess")}
    assert by_id["sure"]["confidence"] != by_id["guess"]["confidence"]


def test_find_callees_labels_the_callee_with_the_edge_it_was_reached_by(server):
    nodes = _call(server, "find_callees", {"node_id": "guess"})["nodes"]
    assert _site_conf(nodes) == {("target", 9): "AMBIGUOUS", ("mid", 11): "EXTRACTED"}


def test_find_dependents_carries_the_manifest_edges_confidence(server):
    nodes = _call(server, "find_dependents", {"package": "widgetlib"})["nodes"]
    assert _site_conf(nodes, "edge_line") == {("mani", 4): "INFERRED"}


def test_each_path_hop_carries_the_confidence_of_its_own_edge(server):
    res = _call(server, "shortest_path", {"src_id": "guess", "dst_id": "end"})
    assert [n["id"] for n in res["nodes"]] == ["guess", "mid", "end"]
    assert [n["confidence"] for n in res["nodes"]] == [None, "EXTRACTED", "AMBIGUOUS"]


def test_the_paths_first_node_has_no_edge_so_no_confidence(server):
    res = _call(server, "shortest_path", {"src_id": "guess", "dst_id": "target"})
    assert res["nodes"][0]["id"] == "guess"
    assert res["nodes"][0]["confidence"] is None


def test_ask_callers_route_carries_confidence(server):
    res = _call(server, "ask", {"question": "who calls Target"})
    assert res["route"] == "callers"
    assert _site_conf(res["nodes"]) == {
        ("sure", 5): "EXTRACTED", ("guess", 9): "AMBIGUOUS",
        ("mixed", 20): "EXTRACTED", ("mixed", 30): "AMBIGUOUS",
    }


def test_ask_subclasses_route_carries_confidence(server):
    res = _call(server, "ask", {"question": "what subclasses Base"})
    assert res["route"] == "subclasses"
    assert _site_conf(res["nodes"], "edge_line") == {("Sub", 2): "INFERRED"}


def test_ask_dependents_route_carries_confidence(server):
    res = _call(server, "ask", {"question": "what depends on widgetlib"})
    assert res["route"] == "dependents"
    assert _site_conf(res["nodes"], "edge_line") == {("mani", 4): "INFERRED"}


@pytest.mark.parametrize("tool,args", [
    ("get_node", {"node_id": "target"}),
    ("find_definition", {"name": "Target"}),
    ("search_code", {"query": "Target"}),
])
def test_verbs_that_reach_a_node_without_an_edge_report_none(server, tool, args):
    """No edge, so no confidence to report. None must not be filled in as a default."""
    res = _call(server, tool, args)
    # `get_node` returns one node, which the SDK wraps as {"result": {...}}.
    nodes = res.get("nodes") or [res.get("result", res)]
    assert nodes and all("confidence" in n and n["confidence"] is None for n in nodes)


def test_the_field_is_optional_so_existing_clients_keep_working():
    """Additive: a payload from an older server, without the field, still validates."""
    assert "confidence" in NodeOut.model_fields
    assert "confidence" not in NodeOut.model_json_schema().get("required", [])
    assert NodeOut(id="a", repo="r", kind="function", name="a").confidence is None


def test_the_server_instructions_name_the_field_the_results_carry():
    """The instructions promise confidence-tagged results; the promise must name a
    field that exists on the object and is filled on an edge-reached node."""
    assert "`confidence`" in server_mod._INSTRUCTIONS
    assert "confidence" in NodeOut.model_fields
