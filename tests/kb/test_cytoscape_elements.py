"""Tests for kb/visualize.py's _cytoscape_elements (the raw payload -> cytoscape.js
element-list translation shared by every graph render)."""

from contextlake.kb.visualize import _cytoscape_elements


def _payload(nodes, edges):
    return {"nodes": nodes, "edges": edges}


def test_every_node_carries_a_deg_even_with_no_edges():
    """The style sheet maps node width/height via mapData(deg, ...) on every node;
    a node missing `deg` entirely (not just deg=0) makes cytoscape.js log a console
    warning on first render. deg must always be present, isolated nodes included."""
    payload = _payload([{"id": "a", "name": "A", "kind": "file"}], [])
    els = _cytoscape_elements(payload)
    assert els[0]["data"]["deg"] == 0


def test_degree_counts_each_incident_edge_once_per_endpoint():
    payload = _payload(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        [{"src": "a", "dst": "b"}, {"src": "a", "dst": "c"}],
    )
    els = {e["data"]["id"]: e["data"] for e in _cytoscape_elements(payload) if "id" in e["data"]}
    assert els["a"]["deg"] == 2  # two distinct edges touch a
    assert els["b"]["deg"] == 1
    assert els["c"]["deg"] == 1


def test_self_loop_excluded_from_degree():
    """Matches app.js's own n.degree(false) (includeLoops=false) exactly -- a
    self-referencing edge must not inflate a node's degree."""
    payload = _payload([{"id": "a"}], [{"src": "a", "dst": "a"}])
    els = _cytoscape_elements(payload)
    assert els[0]["data"]["deg"] == 0
