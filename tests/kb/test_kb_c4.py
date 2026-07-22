"""Tests for the C4 namespace-boundary data model (kb.c4)."""

from datetime import date

from contextlake.kb import c4
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="x", source_line=1, verified_at=date(2026, 6, 21))


def _fnode(rid, name):
    return Node(id=make_id(rid, name), repo=rid, kind="file", name=name)


def _edge(rid, name, dst, relation):
    return Edge(src=make_id(rid, name), dst=dst, relation=relation,
                confidence=Confidence.INFERRED, provenance=_PROV)


def _seed(store_dir):
    """3 repos under acme/pay + 1 outside; web->api HTTP (internal),
    ship/api->web HTTP (boundary). Each repo also gets a shard for its brief."""
    s = SqliteStore(store_dir / "index.sqlite")
    for rid in ("acme/pay/api", "acme/pay/web", "acme/pay/core", "acme/ship/api"):
        s.upsert_repo(Repo(id=rid, path=f"/repos/{rid}"))
    ep_e = make_id("endpoint", "/orders")
    ep_f = make_id("endpoint", "/ship")
    # api exposes /orders
    s.upsert_nodes("acme/pay/api",
                   [_fnode("acme/pay/api", "ctrl"),
                    Node(id=ep_e, repo="acme/pay/api", kind="endpoint", name="/orders")])
    s.upsert_edges("acme/pay/api", [_edge("acme/pay/api", "ctrl", ep_e, "exposes")])
    # web calls /orders (-> internal web->api) and exposes /ship
    s.upsert_nodes("acme/pay/web",
                   [_fnode("acme/pay/web", "app"),
                    Node(id=ep_f, repo="acme/pay/web", kind="endpoint", name="/ship")])
    s.upsert_edges("acme/pay/web",
                   [_edge("acme/pay/web", "app", ep_e, "calls_http"),
                    _edge("acme/pay/web", "app", ep_f, "exposes")])
    # ship/api calls /ship (-> boundary ship/api->web)
    s.upsert_nodes("acme/ship/api", [_fnode("acme/ship/api", "cl")])
    s.upsert_edges("acme/ship/api", [_edge("acme/ship/api", "cl", ep_f, "calls_http")])
    s.close()
    # shards for briefs
    for rid, head in (("acme/pay/api", "a1"), ("acme/pay/web", "w1"),
                      ("acme/pay/core", "c1"), ("acme/ship/api", "s1")):
        node = Node(id=make_id(rid, "m"), repo=rid, kind="class", name="Main",
                    file="m.py", lang="python")
        write_shard(store_dir, GraphShard(repo=rid, head_commit=head, nodes=[node], edges=[]))
    return SqliteStore(store_dir / "index.sqlite")


def test_c4_model_buckets_namespaces_and_splits_edges(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)  # depth 2 -> acme/pay, acme/ship
    ns = {b.namespace for b in model.boundaries}
    assert "acme/pay" in ns and "acme/ship" in ns
    # every container maps to exactly one boundary
    total_containers = sum(len(b.containers) for b in model.boundaries)
    assert total_containers == model.meta["container_count"]
    # the acme/ship/api -> acme/pay/web edge is a boundary (cross-namespace) edge
    boundary_edges = [e for e in model.edges if e.boundary]
    assert any(e.src.startswith("acme/ship") and e.dst.startswith("acme/pay")
               for e in boundary_edges)
    # the intra-acme/pay edges are internal
    assert any(not e.boundary for e in model.edges)
    # weights preserved, confidence INFERRED
    assert all(e.confidence == "INFERRED" and e.weight >= 1 for e in model.edges)


def test_to_c4_dot_emits_clusters_and_labeled_edges(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)
    dot = c4.to_c4_dot(model)
    assert dot.startswith("digraph")
    assert "subgraph cluster_" in dot          # boundaries drawn as clusters
    assert 'label="acme/pay"' in dot           # boundary label present
    assert "http" in dot                       # flavor-labeled edge
    # deterministic: same model renders identically
    assert c4.to_c4_dot(model) == dot


def test_c4_payload_parents_and_cytoscape_elements(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)
    payload = c4.c4_payload(model)
    # namespace parent nodes present
    parents = [n for n in payload["nodes"] if n.get("kind") == "namespace"]
    assert parents, "expected namespace compound parent nodes"
    # every container node points at a parent
    containers = [n for n in payload["nodes"] if n.get("kind") == "repo"]
    assert containers and all(n.get("parent") for n in containers)
    # cytoscape elements carry data.parent for compound rendering
    from contextlake.kb import visualize as viz
    els = viz._cytoscape_elements(payload)
    node_els = [e for e in els if e["data"].get("id") and "source" not in e["data"]]
    assert any(e["data"].get("parent") for e in node_els)


def test_c4_payload_edge_join_invariant(tmp_path):
    """Every payload edge's src/dst must exactly string-match some node id --
    otherwise cytoscape silently drops the edge (no visible error)."""
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)
    payload = c4.c4_payload(model)
    node_ids = {n["id"] for n in payload["nodes"]}
    assert payload["edges"], "expected at least one edge in the fixture"
    for e in payload["edges"]:
        assert e["src"] in node_ids, f"edge src {e['src']!r} has no matching node id"
        assert e["dst"] in node_ids, f"edge dst {e['dst']!r} has no matching node id"
