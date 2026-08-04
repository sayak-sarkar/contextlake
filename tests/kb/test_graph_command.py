"""Tests for the `contextlake graph` visualizer (bounded subgraph + renderers)."""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from contextlake import style
from contextlake.cli import main
from contextlake.kb import visualize as viz
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import _CDN_URL

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "examples" / "fixtures" / "sample-graph.json"


def _node(nid, repo="r", kind="function", name=None):
    return Node(id=nid, repo=repo, kind=kind, name=name or nid)


def _edge(src, dst, relation="calls"):
    return Edge(src=src, dst=dst, relation=relation, confidence=Confidence.EXTRACTED,
                provenance=Provenance(source_file="a.py", source_line=1,
                                      verified_at=date(2026, 6, 21)))


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def _hub(store, leaves=200):
    nodes = [_node("H", kind="class")] + [_node(f"L{i}") for i in range(leaves)]
    store.upsert_nodes("r", nodes)
    store.upsert_edges("r", [_edge("H", f"L{i}") for i in range(leaves)])


# --- bounded extraction --------------------------------------------------

def test_extract_respects_fanout_cap(store):
    _hub(store, leaves=200)
    nodes, _ = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=500, max_fanout=30)
    assert len(nodes) == 31  # the hub + at most max_fanout neighbours


def test_extract_respects_max_nodes_and_induces_edges(store):
    _hub(store, leaves=200)
    nodes, edges = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=20, max_fanout=1000)
    assert len(nodes) == 20  # global node cap stops expansion
    ids = {n.id for n in nodes}
    # induced subgraph: no dangling edges to capped-out neighbours
    assert all(e.src in ids and e.dst in ids for e in edges)
    assert len(edges) == 19


def test_truncation_meta_is_honest(store):
    _hub(store, leaves=30)
    # overview/repo report a true total; extract reports truncation WITHOUT a total
    # (BFS early-stops, so a number would be a fabrication).
    rm = {}
    viz.repo_subgraph(store, "r", max_nodes=5, meta=rm)
    assert rm["truncated"] is True and rm["total"] == 31
    em = {}
    viz.extract_subgraph(store, ["H"], hops=1, max_nodes=5, max_fanout=100, meta=em)
    assert em["truncated"] is True and "total" not in em
    # an untruncated view says so, with no scary banner
    full = {}
    viz.repo_subgraph(store, "r", max_nodes=500, meta=full)
    assert full["truncated"] is False


def test_truncation_banner_reaches_html(store):
    _hub(store, leaves=2)
    pay = viz.to_payload(_payload(store)["nodes"], [],
                         {"mode": "repo", "truncated": True, "total": 99})
    html = viz.to_html(pay, cdn=True)
    assert 'id="trunc"' in html and '"truncated": true' in html and '"total": 99' in html


def test_extract_skips_unknown_seed(store):
    store.upsert_nodes("r", [_node("a")])
    nodes, edges = viz.extract_subgraph(store, ["does-not-exist", "a"], hops=1)
    assert {n.id for n in nodes} == {"a"}


def test_seed_limit_of_zero_means_zero_not_the_default(store):
    # `limit or 20` made an explicit 0 indistinguishable from "unset". The CLI now
    # refuses 0 at parse time, but the falsiness was the root cause and it is
    # reachable from any library caller, so it is pinned here too.
    from types import SimpleNamespace
    store.upsert_nodes("r", [_node(f"n{i}", name="dup") for i in range(3)])
    args = SimpleNamespace(node=None, kind=None, repo=None, name="dup", limit=0)
    assert viz.seed_ids_from_args(store, args) == []


def test_repo_subgraph_includes_one_hop_neighbors_outside_the_repo(store):
    # a->x used to be dropped entirely (both endpoints had to be in-repo); it's
    # now surfaced one hop out -- the same shape as a linked GitLab MR/Figma
    # design/Slack channel/wiki section one hop from the code that links to it,
    # whether the neighbor sits in a sentinel/partition repo or (as here) just
    # another ordinary repo -- the widening isn't restricted by node.repo.
    store.upsert_repo(Repo(id="r", path="/r"))
    store.upsert_nodes("r", [_node("a", repo="r"), _node("b", repo="r")])
    store.upsert_nodes("other", [_node("x", repo="other")])
    store.upsert_edges("r", [_edge("a", "b"), _edge("a", "x")])  # a->x leaves the repo
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100)
    assert {n.id for n in nodes} == {"a", "b", "x"}
    assert {(e.src, e.dst) for e in edges} == {("a", "b"), ("a", "x")}


def test_repo_subgraph_includes_one_hop_external_nodes(store):
    # matches the shape of a real cross-tool link: a code file `touched_by` a
    # GitLab MR node that lives in the (external) sentinel repo, with no node
    # of its own in `team/api` -- it must still surface one hop out.
    store.upsert_nodes("team/api", [
        Node(id="team_api_pay_py", repo="team/api", kind="file", name="pay.py"),
    ])
    mr = Node(id="gitlab_mr_team_api_42", repo="(external)", kind="mr", name="MR #42")
    store.upsert_nodes("(external)", [mr])
    store.upsert_edges("team/api", [Edge(src="team_api_pay_py", dst=mr.id, relation="touched_by",
                                         confidence=Confidence.EXTRACTED,
                                         provenance=Provenance(source_file="gitlab",
                                                               verified_at=date(2026, 8, 3)))])
    nodes, edges = viz.repo_subgraph(store, "team/api")
    assert mr.id in {n.id for n in nodes}
    assert (("team_api_pay_py", mr.id) in {(e.src, e.dst) for e in edges})


def test_repo_subgraph_one_hop_external_nodes_do_not_recurse(store):
    # a one-hop node's OWN neighbors must never be walked -- otherwise the
    # widening cascades past one hop, which the spec explicitly rules out.
    store.upsert_nodes("r", [_node("a", repo="r")])
    store.upsert_nodes("other", [_node("x", repo="other"), _node("y", repo="other")])
    store.upsert_edges("r", [_edge("a", "x")])
    store.upsert_edges("other", [_edge("x", "y")])  # a second hop -- must not appear
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100)
    ids = {n.id for n in nodes}
    assert ids == {"a", "x"}
    assert "y" not in ids
    assert ("x", "y") not in {(e.src, e.dst) for e in edges}


def test_repo_subgraph_one_hop_external_nodes_exempt_from_max_nodes(store):
    # the node cap governs the repo-internal selection query; one-hop external
    # nodes are additive on top of it, not counted against it.
    store.upsert_nodes("r", [_node("a", repo="r")])
    store.upsert_nodes("other", [_node(f"x{i}", repo="other") for i in range(5)])
    store.upsert_edges("r", [_edge("a", f"x{i}") for i in range(5)])
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=1)
    ids = {n.id for n in nodes}
    assert "a" in ids
    assert {f"x{i}" for i in range(5)} <= ids  # none dropped by max_nodes=1


def test_repo_subgraph_one_hop_external_edges_count_toward_max_edges(store):
    # unlike max_nodes, one-hop edges DO count toward max_edges -- once an edge
    # reaches the page, a Mermaid renderer can't distinguish an internal edge
    # from a one-hop external one, so exempting them would reopen the exact
    # render failure max_edges exists to prevent.
    store.upsert_nodes("r", [_node("a", repo="r")])
    store.upsert_nodes("other", [_node(f"x{i}", repo="other") for i in range(5)])
    store.upsert_edges("r", [_edge("a", f"x{i}") for i in range(5)])
    meta: dict = {}
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100, max_edges=2, meta=meta)
    assert len(edges) == 2
    assert meta["truncated"] is True


def test_repo_subgraph_truncation_keeps_highest_degree_nodes(store):
    # 'zzz' sorts last by node_id but is the hub (5 outgoing edges); 'aaa' and
    # 'bbb' are degree-0 leaves that would win a plain ORDER BY node_id
    # truncation. With max_nodes=1 the survivor must be the hub, not whichever
    # node sorts first alphabetically/by id.
    store.upsert_nodes("r", [_node("zzz", kind="class"), _node("aaa"), _node("bbb")])
    store.upsert_edges("r", [_edge("zzz", f"e{i}") for i in range(5)])
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=1)
    assert [n.id for n in nodes] == ["zzz"]


def test_repo_subgraph_max_nodes_eviction_does_not_leak_back_in_as_one_hop(store):
    # a same-repo node excluded by max_nodes truncation must NOT reappear via
    # the one-hop-external widening -- that would silently defeat the cap the
    # exact use case (a dense repo) it exists for.
    store.upsert_nodes("r", [_node("hub", kind="class")]
                       + [_node(f"leaf{i}") for i in range(30)])
    store.upsert_edges("r", [_edge("hub", f"leaf{i}") for i in range(30)])
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=5)
    assert len(nodes) == 5
    ids = {n.id for n in nodes}
    # every edge returned must land between two of the surviving 5 nodes --
    # none of the evicted leaf nodes leaked back in as "one hop out"
    assert all(e.src in ids and e.dst in ids for e in edges)


def _dense_hub(store, leaves=20):
    """A synthetic repo where a small node cap still yields MORE edges than
    nodes -- a hub with fan-out to every leaf, plus every leaf calling every
    other leaf, so max_nodes alone (the old cap) doesn't bound edge count.
    Mirrors the real-world shape (dense C/C++ contains/calls fan-out) that
    tripped Mermaid's own maxEdges guard -- synthetic, not the real repo."""
    nodes = [_node("hub", kind="class")] + [_node(f"leaf{i}") for i in range(leaves)]
    store.upsert_nodes("r", nodes)
    edges = [_edge("hub", f"leaf{i}") for i in range(leaves)]
    for i in range(leaves):
        for j in range(leaves):
            if i != j:
                edges.append(_edge(f"leaf{i}", f"leaf{j}"))
    store.upsert_edges("r", edges)


def test_repo_subgraph_caps_edges_independently_of_nodes(store):
    _dense_hub(store, leaves=20)  # 20 nodes but 20 + 20*19 = 400 possible edges
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100, max_edges=50)
    assert len(nodes) == 21  # node cap never hit
    # exact, not <=: the cap must stop collection AT max_edges, never over it
    # (an off-by-one here would mean the render can still exceed Mermaid's own
    # hard limit for a differently-shaped dense repo) and never meaningfully
    # under it either while more qualifying edges remain uncollected.
    assert len(edges) == 50
    meta: dict = {}
    viz.repo_subgraph(store, "r", max_nodes=100, max_edges=50, meta=meta)
    assert meta["truncated"] is True
    # edge-only truncation must NOT fabricate a node "total" -- that count is
    # only meaningful (and only computed) when the NODE cap itself was hit.
    assert "total" not in meta


def test_repo_subgraph_edges_under_cap_is_not_truncated(store):
    _dense_hub(store, leaves=5)  # 5 + 5*4 = 25 edges, well under any real cap
    meta: dict = {}
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100, max_edges=400, meta=meta)
    assert meta["truncated"] is False
    assert len(nodes) == 6
    assert len(edges) == 25


def test_repo_subgraph_max_edges_none_means_uncapped(store):
    # the default: callers that don't render through Mermaid (html/dot via
    # `graph --format html`) must keep seeing every edge among the capped
    # nodes, exactly as before this fix -- no new implicit limit.
    _dense_hub(store, leaves=20)
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100)
    assert len(nodes) == 21
    assert len(edges) == 20 + 20 * 19  # every hub + inter-leaf edge, uncapped


def _node_with_file(nid, file, repo="r"):
    return Node(id=nid, repo=repo, kind="function", name=nid, file=file)


def test_repo_subgraph_path_prefix_scopes_to_one_module(store):
    store.upsert_nodes("r", [
        _node_with_file("a", "src/foo.py"), _node_with_file("b", "src/bar.py"),
        _node_with_file("c", "vendor/thirdparty.py"),
    ])
    store.upsert_edges("r", [_edge("a", "b"), _edge("a", "c")])
    nodes, edges = viz.repo_subgraph(store, "r", max_nodes=100, path_prefix="src")
    assert {n.id for n in nodes} == {"a", "b"}
    assert [(e.src, e.dst) for e in edges] == [("a", "b")]  # c is out of scope


def test_repo_subgraph_path_prefix_escapes_sql_wildcards(store):
    # a module name containing literal SQL wildcard characters must be matched
    # literally, not interpreted as a LIKE pattern (e.g. "foo_bar" must not
    # also match "fooXbar").
    store.upsert_nodes("r", [
        _node_with_file("a", "foo_bar/x.py"), _node_with_file("b", "fooXbar/x.py"),
    ])
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=100, path_prefix="foo_bar")
    assert {n.id for n in nodes} == {"a"}


def test_repo_subgraph_path_prefix_does_not_match_sibling_directory(store):
    # "api" must scope to the "api/" module only, not also match a sibling
    # directory that merely starts with the same characters, like "apiv2/".
    store.upsert_nodes("r", [
        _node_with_file("a", "api/x.py"), _node_with_file("b", "apiv2/x.py"),
    ])
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=100, path_prefix="api")
    assert {n.id for n in nodes} == {"a"}
    # a trailing slash on the prefix must still match normally.
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=100, path_prefix="api/")
    assert {n.id for n in nodes} == {"a"}


def test_repo_subgraph_orders_one_hop_external_nodes_deterministically(store):
    # An export must be byte-reproducible: same store, same bytes. The one-hop
    # external nodes were collected in a set and emitted in set-iteration order,
    # i.e. string-hash order, which PYTHONHASHSEED re-randomises per process --
    # so the node SET was stable while the sequence (and the bytes) moved.
    # 12 ids make an accidentally-sorted set order vanishingly unlikely.
    store.upsert_nodes("r", [_node("a", repo="r")])
    ext = [f"x{i:02d}" for i in range(12)]
    store.upsert_nodes("other", [_node(x, repo="other") for x in ext])
    store.upsert_edges("r", [_edge("a", x) for x in ext])
    nodes, _ = viz.repo_subgraph(store, "r", max_nodes=100)
    assert [n.id for n in nodes] == ["a", *sorted(ext)]


def test_extract_subgraph_expands_seeds_in_seed_order(store):
    # Same class of bug one function up: the BFS frontier was `list(seen)` over a
    # set, so multi-seed expansion visited seeds in hash order and appended their
    # neighbours in a per-process-random sequence.
    seeds = [f"s{i:02d}" for i in range(8)]
    store.upsert_nodes("r", [_node(s) for s in seeds]
                       + [_node(f"n{i:02d}") for i in range(8)])
    store.upsert_edges("r", [_edge(s, f"n{i:02d}") for i, s in enumerate(seeds)])
    nodes, _ = viz.extract_subgraph(store, seeds, hops=1, max_nodes=100, max_fanout=50)
    assert [n.id for n in nodes] == seeds + [f"n{i:02d}" for i in range(8)]


def test_repo_modules_ranks_by_size_and_drops_tiny_segments(store):
    files = (["src/a.py"] * 8) + (["vendor/b.py"] * 3) + (["scripts/c.py"] * 1)
    store.upsert_nodes("r", [
        _node_with_file(f"n{i}", f) for i, f in enumerate(files)
    ])
    mods = viz.repo_modules(store, "r", min_nodes=2)
    assert [m["prefix"] for m in mods] == ["src", "vendor"]  # scripts (1) dropped
    assert mods[0] == {"prefix": "src", "nodes": 8}


def test_repo_modules_within_drills_one_level_deeper(store):
    # A repo whose entire code lives under one top-level dir: the depth-1 view
    # offers only "src" (no way to narrow further); `within="src"` must reveal
    # SRC's own children instead of repeating the same top-level answer.
    files = (["src/foo/a.py"] * 6) + (["src/bar/b.py"] * 4) + (["src/loose.py"] * 1) + \
        (["test/c.py"] * 1)
    store.upsert_nodes("r", [
        _node_with_file(f"n{i}", f) for i, f in enumerate(files)
    ])
    top = viz.repo_modules(store, "r", min_nodes=2)
    assert [m["prefix"] for m in top] == ["src"]  # test (1) dropped below min_nodes

    within_src = viz.repo_modules(store, "r", within="src", min_nodes=2)
    assert [m["prefix"] for m in within_src] == ["src/foo", "src/bar"]
    assert within_src[0] == {"prefix": "src/foo", "nodes": 6}
    # a file directly under "src/" (src/loose.py, no further segment) contributes
    # to src's own top-level count but has no child of its own to report here.

    # one level deeper than "src/foo" lands on the file itself (no further
    # subdirectory to report) -- a single-file "module" of its own 6 nodes.
    within_deeper = viz.repo_modules(store, "r", within="src/foo", min_nodes=2)
    assert within_deeper == [{"prefix": "src/foo/a.py", "nodes": 6}]

    # a genuinely empty scope (no matching files at all) reports no modules.
    assert viz.repo_modules(store, "r", within="src/foo/a.py", min_nodes=1) == []


def test_repo_modules_within_escapes_sql_wildcards(store):
    store.upsert_nodes("r", [
        _node_with_file("a", "foo_bar/x/y.py"), _node_with_file("b", "fooXbar/x/y.py"),
    ])
    mods = viz.repo_modules(store, "r", within="foo_bar", min_nodes=1)
    assert [m["prefix"] for m in mods] == ["foo_bar/x"]


def test_overview_aggregates_cross_repo(store):
    # the overview's cross-repo edges come from the package two-hop (publishes ⨝
    # depends_on), NOT raw imports: repoA publishes pkg, repoB depends_on it ->
    # repoB depends_on repoA.
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_repo(Repo(id="repoB", path="/b"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    store.upsert_nodes("repoB", [_node("b1", repo="repoB")])
    store.upsert_edges("repoA", [_edge("a1", "pkg", "publishes")])
    store.upsert_edges("repoB", [_edge("b1", "pkg", "depends_on")])
    nodes, edges = viz.overview_subgraph(store, max_nodes=50)
    assert {n["id"] for n in nodes} == {"repoA", "repoB"}
    assert all(n["kind"] == "repo" for n in nodes)
    dep = [e for e in edges if e["src"] == "repoB" and e["dst"] == "repoA"]
    assert len(dep) == 1 and dep[0]["relation"] == "depends_on"
    assert dep[0]["confidence"] == "INFERRED"  # manifest-derived, not ground truth


def test_overview_never_shows_a_shared_node_sentinel_as_a_repo(store):
    """A `(shared)`/`(packages)`/`(external)` sentinel node (e.g. every module
    imported fleet-wide, via SHARED_REPO) is not a repo and must never be
    ranked, listed, or given its own page as though it were one -- it would
    otherwise be the single largest "repo" in the whole fleet overview."""
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    # 50 module nodes owned by the shared-node sentinel, as parse.py now emits
    store.upsert_nodes("repoA", [_node(f"m{i}", repo="(shared)", kind="module")
                                 for i in range(50)])
    store.upsert_nodes("repoA", [_node("pkg1", repo="(packages)", kind="package")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=50)
    ids = {n["id"] for n in nodes}
    assert ids == {"repoA"}
    assert "(shared)" not in ids and "(packages)" not in ids


def test_overview_keeps_most_connected_not_alphabetical(store):
    # 'zzz' is a hub (sorts last alphabetically) linked to aaa/bbb/ccc; the trivial
    # repos each have degree 1. With max_nodes=2 the hub must survive, not be dropped
    # for an alphabetically-earlier trivial repo.
    for r in ("aaa", "bbb", "ccc", "zzz"):
        store.upsert_repo(Repo(id=r, path="/" + r))
        store.upsert_nodes(r, [_node(r + "1", repo=r)])
    # zzz publishes a package the other three depend on -> it's the connectivity hub
    store.upsert_edges("zzz", [_edge("zzz1", "pkgz", "publishes")])
    for r in ("aaa", "bbb", "ccc"):
        store.upsert_edges(r, [_edge(r + "1", "pkgz", "depends_on")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=2)
    ids = {n["id"] for n in nodes}
    assert "zzz" in ids and len(ids) == 2  # the hub is kept despite sorting last


def test_overview_includes_every_repo_even_without_code(store):
    store.upsert_repo(Repo(id="empty/repo", path="/e"))   # registered, no parsed nodes
    store.upsert_repo(Repo(id="has/code", path="/h"))
    store.upsert_nodes("has/code", [_node("c1", repo="has/code")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=50)
    by_id = {n["id"]: n for n in nodes}
    assert "empty/repo" in by_id and "has/code" in by_id  # one node per repo
    assert by_id["empty/repo"]["attrs"]["node_count"] == 0


# --- exporters -----------------------------------------------------------

def _payload(store):
    n, e = viz.extract_subgraph(store, ["H"], hops=1, max_nodes=10, max_fanout=5)
    return viz.to_payload(n, e, {"mode": "neighborhood"})


def test_json_export_shape(store):
    _hub(store, leaves=20)
    d = json.loads(viz.to_json(_payload(store)))
    assert set(d) == {"nodes", "edges", "meta"}
    assert d["meta"]["mode"] == "neighborhood"
    assert d["nodes"] and "id" in d["nodes"][0]


def test_dot_and_mermaid_export(store):
    _hub(store, leaves=20)
    p = _payload(store)
    assert viz.to_dot(p).startswith("digraph contextlake {")
    assert viz.to_mermaid(p).startswith("graph LR")


def test_graphml_export_is_well_formed_xml_with_attributes(store):
    _hub(store, leaves=3)
    p = _payload(store)
    text = viz.to_graphml(p)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    nodes = root.findall(".//g:node", ns)
    edges = root.findall(".//g:edge", ns)
    assert len(nodes) == len(p["nodes"])
    assert len(edges) == len(p["edges"])
    # every node carries its kind/name as declared <data> keys, not just an id
    kinds = {d.get("key") for n in nodes for d in n.findall("g:data", ns)}
    assert "n_kind" in kinds and "n_name" in kinds


def test_graphml_export_of_a_repo_carries_its_linked_external_nodes(store):
    """The point of widening repo_subgraph: an export a person hands to Gephi or
    Neo4j should carry the repo's linked MRs/designs/threads, not code alone.
    repo_subgraph is tested directly elsewhere; this pins the whole
    repo_subgraph -> to_payload -> renderer chain, where a repo-scoped filter
    anywhere downstream would silently drop the external node again."""
    store.upsert_nodes("team/api", [
        Node(id="team_api_pay_py", repo="team/api", kind="file", name="pay.py"),
    ])
    mr = Node(id="gitlab_mr_team_api_42", repo="(external)", kind="mr", name="MR #42")
    store.upsert_nodes("(external)", [mr])
    store.upsert_edges("team/api", [Edge(src="team_api_pay_py", dst=mr.id, relation="touched_by",
                                         confidence=Confidence.EXTRACTED,
                                         provenance=Provenance(source_file="gitlab",
                                                               verified_at=date(2026, 8, 3)))])
    payload = viz.to_payload(*viz.repo_subgraph(store, "team/api"))
    # GraphML renumbers ids to n0/n1, so the MR shows up by its kind/name data keys
    graphml = viz.to_graphml(payload)
    assert '<data key="n_kind">mr</data>' in graphml
    assert '<data key="n_name">MR #42</data>' in graphml
    assert graphml.count("<node id=") == 2  # the file AND the MR, not code alone
    cypher = viz.to_cypher(payload)
    assert mr.id in cypher  # Cypher keeps the real id as a property
    assert "`touched_by`" in cypher


def test_graphml_escapes_xml_special_characters_in_attribute_values():
    payload = viz.to_payload(
        [_node("n1", name='A<B>&"weird"')], [],
    )
    text = viz.to_graphml(payload)
    assert "A<B>" not in text  # raw '<'/'>' would corrupt the XML structure
    assert "&lt;B&gt;" in text
    # still parses cleanly despite the adversarial name
    import xml.etree.ElementTree as ET
    ET.fromstring(text)


def test_graphml_skips_edges_with_a_missing_endpoint(store):
    _hub(store, leaves=3)
    p = _payload(store)
    p["edges"].append({"src": p["nodes"][0]["id"], "dst": "does-not-exist",
                       "relation": "calls", "confidence": "EXTRACTED", "weight": 1.0})
    text = viz.to_graphml(p)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    # the dangling edge must not appear at all (not even as a broken reference)
    assert len(root.findall(".//g:edge", ns)) == len(p["edges"]) - 1


def test_cypher_export_shape(store):
    _hub(store, leaves=3)
    p = _payload(store)
    text = viz.to_cypher(p)
    lines = text.splitlines()
    node_lines = [ln for ln in lines if ln.startswith("CREATE (n") and "]->" not in ln]
    edge_lines = [ln for ln in lines if "]->" in ln]
    assert len(node_lines) == len(p["nodes"])
    assert len(edge_lines) == len(p["edges"])
    assert 'id: "H"' in text  # the real node id survives as a property, not just the alias


def test_cypher_backtick_quotes_open_vocabulary_kind_and_relation():
    # kind/relation are free text (kb/model.py) -- a value with characters
    # invalid in a bare Cypher identifier (space, hyphen) must still produce
    # syntactically valid Cypher via backtick-quoting, not a bare (and broken)
    # label/relationship-type.
    payload = viz.to_payload(
        [_node("n1", name="A", kind="http endpoint"), _node("n2", name="B", kind="db-table")],
        [_edge("n1", "n2", relation="calls via http")],
    )
    text = viz.to_cypher(payload)
    assert "`http endpoint`" in text
    assert "`db-table`" in text
    assert "`calls via http`" in text


def test_cypher_escapes_a_literal_backtick_in_kind_by_doubling_it():
    payload = viz.to_payload([_node("n1", name="A", kind="weird`kind")], [])
    text = viz.to_cypher(payload)
    assert "weird``kind" in text


def test_cypher_escapes_quotes_and_backslashes_in_string_properties():
    payload = viz.to_payload([_node("n1", name='say "hi"\\there')], [])
    text = viz.to_cypher(payload)
    assert 'say \\"hi\\"\\\\there' in text


def test_cypher_skips_edges_with_a_missing_endpoint(store):
    _hub(store, leaves=3)
    p = _payload(store)
    p["edges"].append({"src": p["nodes"][0]["id"], "dst": "does-not-exist",
                       "relation": "calls", "confidence": "EXTRACTED", "weight": 1.0})
    text = viz.to_cypher(p)
    assert "does-not-exist" not in text


def test_mermaid_edge_label_pipe_does_not_break_delimiter_syntax():
    # A literal "|" in a relation string used to slip straight through _mermaid_escape
    # and break the `-->|label|` delimiter, producing invalid Mermaid (confirmed by
    # feeding the old output through a real Mermaid parser).
    payload = viz.to_payload(
        [_node("n1", name="A"), _node("n2", name="B")],
        [_edge("n1", "n2", relation="calls|injected")],
    )
    out = viz.to_mermaid(payload)
    edge_line = next(line for line in out.splitlines() if "-->" in line)
    # exactly the two delimiter pipes around the label survive -- none from the payload
    assert edge_line.count("|") == 2
    assert "calls/injected" in edge_line


def test_class_diagram_signature_brace_does_not_close_the_class_body_early():
    # A "}" in a member signature used to close the classDiagram body block early,
    # letting anything after it (including a crafted "{") emit as new top-level
    # Mermaid statements -- confirmed via a real Mermaid parser.
    prov = Provenance(source_file="m.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="c1", repo="r", kind="class", name="MyClass"),
        Node(id="m1", repo="r", kind="method", name="DoThing",
             attrs={"signature": "(x: int) }\n  class Injected\n  Injected : +evil() {"}),
    ]
    edges = [Edge(src="c1", dst="m1", relation="contains",
                   confidence=Confidence.EXTRACTED, provenance=prov)]
    out = viz.to_class_diagram(viz.to_payload(nodes, edges))
    # the class body's own closing "}" is the only one -- none smuggled in via the signature
    assert out.count("}") == 1
    # the injected "class Injected" text survives only as inert label content on one line
    # (flattened by the newline-stripping fix), never as its own new top-level statement
    assert not any(line.strip().startswith("class Injected") for line in out.splitlines())


def test_sequence_diagram_node_name_newline_cannot_inject_a_note_directive():
    # A newline embedded in a node name used to become a genuine new top-level line
    # in the emitted Mermaid text -- confirmed rendering as a real Note box via a
    # real Mermaid parser, not just garbled label text.
    nodes = [Node(id="seed", repo="r", kind="function", name="Seed"),
             Node(id="callee", repo="r", kind="function",
                  name="Callee\nNote over p0,p1: INJECTED")]
    prov = Provenance(source_file="m.py", source_line=1, verified_at=date(2026, 6, 21))
    edges = [Edge(src="seed", dst="callee", relation="calls",
                   confidence=Confidence.INFERRED, provenance=prov)]
    payload = viz.to_payload(nodes, edges, meta={"seed_ids": ["seed"]})
    out = viz.to_sequence_diagram(payload)
    assert "\nNote over p0,p1: INJECTED\n" not in out
    # "INJECTED" only ever appears inline as part of a real message/participant line,
    # never as its own bare top-level "Note over ..." directive
    assert all(
        ":" in line or line.strip().startswith(("sequenceDiagram", "participant"))
        for line in out.splitlines()
        if "INJECTED" in line
    )


def test_html_is_offline_by_default(store):
    _hub(store, leaves=5)
    html = viz.to_html(_payload(store))
    assert _CDN_URL not in html        # offline default: no CDN reference
    assert len(html) > 100_000         # the vendored lib is inlined
    html_cdn = viz.to_html(_payload(store), cdn=True)
    assert _CDN_URL in html_cdn        # --cdn references the CDN
    # ...and does not inline any vendored lib. The page's own JS/CSS (app shell,
    # minimap, semantic zoom, LOD labels, legend glyphs, dagre-preview wiring, the
    # PNG/SVG exporters) is always inlined and sits ~113KB; the bound stays under the
    # smallest lib we could accidentally inline (cytoscape-dom-node, ~10KB) so a
    # regression still trips it.
    assert len(html_cdn) < 122_000


def test_kind_icons_are_offline_data_uris_with_contrast():
    icons = viz._kind_icons()
    # one glyph per palette kind, including the flow nodes (endpoint/topic)
    assert {"file", "class", "function", "package", "repo", "endpoint", "topic"} <= set(icons)
    for kind, uri in icons.items():
        assert uri.startswith("data:image/svg+xml;utf8,"), kind   # inlined, no CDN/sprite fetch
        assert "%3Csvg" in uri                                    # percent-encoded SVG
    # contrast is chosen per node fill: white glyph on the dark navy repo node,
    # dark glyph on the light yellow module node — a single colour would vanish on one.
    assert "%23ffffff" in icons["repo"]
    assert "%230E2A33" in icons["module"]


def test_html_inlines_icon_map_token():
    html = viz.to_html({"nodes": [{"id": "a", "kind": "class", "name": "A"}], "edges": []})
    assert "__ICONS__" not in html and "var ICONS =" in html
    assert "data:image/svg+xml" in html


def test_only_architectural_edges_are_labelled():
    # the labelled-flow wiring ships in the inlined app.js
    html = viz.to_html({"nodes": [{"id": "a", "kind": "repo", "name": "A"}], "edges": []})
    assert '"label": edgeLabel' in html and "ARCH_RELS" in html
    block = re.search(r"var ARCH_RELS = \{([^}]*)\}", html, re.S).group(1)
    assert "calls_http" in block and "depends_on" in block   # architectural -> labelled
    assert "contains" not in block                           # structural -> stays clean


def test_overview_repo_carries_dominant_language(store):
    store.upsert_nodes("py-svc", [
        Node(id="a", repo="py-svc", kind="function", name="a", lang="python"),
        Node(id="b", repo="py-svc", kind="function", name="b", lang="python"),
        Node(id="c", repo="py-svc", kind="function", name="c", lang="c")])
    store.upsert_nodes("js-svc", [
        Node(id="x", repo="js-svc", kind="function", name="x", lang="javascript")])
    nodes, _ = viz.overview_subgraph(store, max_nodes=50)
    by_id = {n["id"]: n for n in nodes}
    assert by_id["py-svc"]["lang"] == "python"        # dominant of {python:2, c:1}
    assert by_id["js-svc"]["lang"] == "javascript"


def test_lang_icons_are_offline_lettermarks():
    li = viz._lang_icons()
    assert {"python", "javascript", "typescript", "csharp"} <= set(li)
    for uri in li.values():
        assert uri.startswith("data:image/svg+xml;utf8,") and "%3Ctext" in uri


def test_html_carries_node_detail_and_ui_controls(store):
    store.upsert_nodes("team/x", [Node(id="a", repo="team/x", kind="class", name="A",
                                       qualified_name="x.A", file="a.py", line_start=3),
                                  _node("b", repo="team/x")])
    store.upsert_edges("team/x", [_edge("a", "b")])
    n, e = viz.extract_subgraph(store, ["a"], hops=1)
    html = viz.to_html(viz.to_payload(n, e, {"mode": "neighborhood"}), cdn=True)
    # data the detail panel / search read from
    assert '"qn": "x.A"' in html and '"file": "a.py"' in html
    # the new UI affordances are present
    for control in ('id="search"', 'id="legend"', 'id="png"', 'id="info"', 'data-kind='):
        assert control in html, control


def test_html_layout_initial(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), cdn=True, layout="grid")
    assert 'var LAYOUT = "grid"' in html
    assert viz.to_html(_payload(store), cdn=True, layout="nonsense").count('var LAYOUT = "cose"')


def test_html_carries_contextlake_branding(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), cdn=True)
    assert 'class="glyph"' in html                    # the brand glyph is inlined
    assert 'context<span class="l">lake</span>' in html  # two-tone wordmark
    # the brand palette drives the styling
    for hexcolor in ("#0E2A33", "#137A8B", "#2BB3A3", "#EAF4F4"):
        assert hexcolor in html, hexcolor


# --- packaging -----------------------------------------------------------

def test_edge_detail_is_surfaced(store):
    from datetime import date

    from contextlake.kb.model import Provenance
    store.upsert_nodes("r", [_node("a", kind="class"), _node("b")])
    store.upsert_edges("r", [Edge(
        src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
        provenance=Provenance(source_file="o.py", source_line=12, verified_at=date(2026, 6, 21)),
        context="call", weight=3.0)])
    n, e = viz.extract_subgraph(store, ["a"], hops=1)
    pay = viz.to_payload(n, e, {"mode": "neighborhood"})
    # to_json must NOT throw on the verified_at date, and must carry full provenance
    d = json.loads(viz.to_json(pay))
    ed = d["edges"][0]
    assert ed["prov_file"] == "o.py" and ed["prov_line"] == 12 and ed["verified_at"] == "2026-06-21"
    assert ed["context"] == "call" and ed["confidence"] == "INFERRED" and ed["weight"] == 3.0
    html = viz.to_html(pay, cdn=True)
    assert 'id="edgelegend"' in html and 'data-rel="calls"' in html  # relationship legend/filter
    assert "var REL_COLORS" in html and "showEdgeInfo" in html       # edge inspector wired
    assert '"prov_file": "o.py"' in html                              # provenance reaches the page


def test_cytoscape_asset_is_packaged():
    from importlib.resources import files
    asset = files("contextlake.kb") / "static" / "cytoscape.min.js"
    assert asset.is_file()
    assert "cytoscape" in asset.read_text(encoding="utf-8")[:4000].lower()


def test_app_assets_are_packaged():
    # the visualizer's CSS/JS were extracted to static/ files; they must resolve.
    from importlib.resources import files
    css = files("contextlake.kb") / "static" / "app.css"
    js = files("contextlake.kb") / "static" / "app.js"
    assert css.is_file() and js.is_file()
    assert "--deepwater" in css.read_text(encoding="utf-8")        # a known rule
    assert "function edgeColor" in js.read_text(encoding="utf-8")  # a known function


def test_html_inlines_extracted_assets(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store))
    # the extracted CSS + JS are inlined into the single offline file...
    assert "--deepwater" in html and "function edgeColor" in html
    # ...and no asset placeholder survives in the output.
    assert "__APP_CSS__" not in html and "__APP_JS__" not in html
    # no residual placeholder token: enumerate what the template actually declares and
    # prove every one was substituted. (A blanket __[A-Z]+__ scan would now trip over
    # the `/* @__PURE__ */` annotations inside the vendored extension bundles.)
    placeholders = set(re.findall(r"__[A-Z_]+__", viz._HTML_TEMPLATE))
    assert placeholders and not [p for p in placeholders if p in html]


def test_layout_extension_assets_are_packaged():
    # the two extensions behind the opt-in "dagre (preview)" layout are vendored too,
    # so the page keeps working offline / air-gapped.
    from importlib.resources import files
    dagre = files("contextlake.kb") / "static" / "cytoscape-dagre.min.js"
    domnode = files("contextlake.kb") / "static" / "cytoscape-dom-node.js"
    assert dagre.is_file() and domnode.is_file()
    # cytoscape-dagre bundles dagre itself (no separate dagre file to vendor)…
    assert "cytoscapeDagre" in dagre.read_text(encoding="utf-8")[:4000]
    # …and the dom-node browser build self-registers against window.cytoscape
    dn = domnode.read_text(encoding="utf-8")
    assert "cytoscapeDomNode" in dn[:4000] and "register(globalCytoscape.cytoscape)" in dn


def test_dagre_preview_is_an_extra_layout_never_the_default(store):
    # the preview is additive: it is offered LAST and cose stays the fallback, so an
    # existing page renders exactly as it did before the extensions were vendored.
    assert viz.LAYOUTS[-1] == "dagre" and viz.LAYOUTS[0] == "cose"
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store))
    assert '<option value="dagre">dagre (preview)</option>' in html
    assert 'var LAYOUT = "cose"' in html                    # unchanged default
    assert viz.to_html(_payload(store), layout="dagre").count('var LAYOUT = "dagre"') == 1
    # the opt-in wiring, and the marching-ants animation, ship in the inlined app.js
    assert "applyRenderMode" in html and "line-dash-offset" in html


def test_html_inlines_the_layout_extensions(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store))
    assert "cytoscapeDagre" in html and "cytoscapeDomNode" in html
    assert "https://cdn.jsdelivr.net" not in html          # offline file stays offline


def test_html_cdn_mode_pins_every_vendored_lib(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), cdn=True)
    for url in (_CDN_URL, *viz.html_render._EXT_CDN_URLS.values()):
        assert f'<script src="{url}"></script>' in html
        assert "@" in url.rsplit("/", 3)[1]                 # version-pinned, never latest
    # referenced, not inlined (the libs' own banner comments are the tell — app.js
    # legitimately *names* the globals to feature-detect them)
    assert "cytoscape.js-dagre" not in html


def test_html_sibling_assets_reference_not_inline(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store), assets="sibling")
    # sibling mode references the shared files instead of inlining them
    assert '<link rel="stylesheet" href="app.css">' in html
    assert '<script src="app.js"></script>' in html
    assert '<script src="cytoscape.min.js"></script>' in html
    assert '<script src="cytoscape-dagre.min.js"></script>' in html
    assert '<script src="cytoscape-dom-node.js"></script>' in html
    assert "--deepwater" not in html and "function edgeColor" not in html  # not inlined
    assert "cytoscapeDagre" not in html                                    # nor the extensions


def test_build_site_emits_cross_linked_offline_pages(store, tmp_path):
    store.upsert_repo(Repo(id="repoA", path="/a"))
    store.upsert_repo(Repo(id="repoB", path="/b"))
    store.upsert_nodes("repoA", [_node("a1", repo="repoA")])
    store.upsert_nodes("repoB", [_node("b1", repo="repoB")])
    store.upsert_edges("repoA", [_edge("a1", "pkg", "publishes")])
    store.upsert_edges("repoB", [_edge("b1", "pkg", "depends_on")])
    out = tmp_path / "site"
    viz.build_site(store, out)

    # one shared copy of each asset, plus index + overview + a page per repo
    for asset in ("cytoscape.min.js", "cytoscape-dagre.min.js", "cytoscape-dom-node.js",
                  "app.css", "app.js"):
        assert (out / asset).is_file()
    assert (out / "index.html").is_file() and (out / "overview.html").is_file()
    assert (out / "repo-repoA.html").is_file() and (out / "repo-repoB.html").is_file()

    overview = (out / "overview.html").read_text(encoding="utf-8")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert '"href": "repo-repoA.html"' in overview   # repo node -> its page
    assert 'href="repo-repoA.html"' in index         # index lists the page
    # per-repo pages reference the shared lib rather than inlining ~1 MB each
    repo_html = (out / "repo-repoA.html").read_text(encoding="utf-8")
    assert '<script src="cytoscape.min.js"></script>' in repo_html
    assert "--deepwater" not in repo_html            # css linked, not inlined


def test_build_site_repos_filter(store, tmp_path):
    store.upsert_repo(Repo(id="team/repoA", path="/a"))
    store.upsert_repo(Repo(id="other/repoB", path="/b"))
    store.upsert_nodes("team/repoA", [_node("a1", repo="team/repoA")])
    store.upsert_nodes("other/repoB", [_node("b1", repo="other/repoB")])
    out = tmp_path / "site"
    viz.build_site(store, out, repos=["team/*"])
    # only the matching repo gets a page; the other is filtered out
    assert (out / "repo-team__repoA.html").is_file()
    assert not (out / "repo-other__repoB.html").exists()
    # the overview still lists every repo (fleet map stays whole)
    overview = (out / "overview.html").read_text(encoding="utf-8")
    assert '"id": "team/repoA"' in overview and '"id": "other/repoB"' in overview


def test_md_to_html_renders_and_escapes():
    h = viz._md_to_html(
        "# Title\n\nA `code` and **bold**.\n\n- one\n- two\n\n```\nx=1\n```\n\n<script>x</script>")
    assert "<h1>Title</h1>" in h and "<code>code</code>" in h and "<strong>bold</strong>" in h
    assert "<ul>" in h and "<li>one</li>" in h and "<pre><code>x=1" in h
    assert "&lt;script&gt;" in h and "<script>" not in h   # injection escaped


def test_md_to_html_no_href_attribute_breakout():
    # a crafted link URL with a quote must not break out of href="..." into a handler
    h = viz._md_to_html('[click](https://evil.com" onmouseover="alert(1))')
    assert 'onmouseover="alert' not in h        # no attribute breakout
    assert "&quot;" in h                         # the quote was escaped
    # a normal http(s) link still renders correctly
    h2 = viz._md_to_html("see [docs](https://example.com/x) now")
    assert '<a href="https://example.com/x" rel="noopener noreferrer">docs</a>' in h2


def test_build_site_emits_wiki_page_with_staleness(store, tmp_path):
    # store.path.parent is the kb dir; build_site reads <kb>/wiki/<slug>.md
    store.upsert_repo(Repo(id="team/api", path="/a", head_commit="abc123"))
    store.upsert_nodes("team/api", [_node("a1", repo="team/api")])
    wiki_dir = store.path.parent / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "team__api.md").write_text(
        "# team/api\n\nThe catalog service.\n\n"
        "*Generated from the knowledge graph of `team/api` at commit `abc123` on 2026-06-25.*\n")
    out = tmp_path / "site"
    viz.build_site(store, out)
    wiki_html = out / "wiki-team__api.html"
    assert wiki_html.is_file()
    body = wiki_html.read_text(encoding="utf-8")
    assert "The catalog service." in body and "fresh" in body   # commit matches -> fresh
    assert 'href="repo-team__api.html"' in body               # links back to the graph
    assert 'href="wiki-team__api.html"' in (out / "index.html").read_text(encoding="utf-8")


# --- live server ---------------------------------------------------------

def test_serve_endpoints(store):
    _hub(store, leaves=5)
    payload = _payload(store)
    port = _free_port()
    srv = viz.build_graph_server(store, payload, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        body = _get(base + "/")
        assert b"<html" in body.lower()
        nb = json.loads(_get(base + "/neighbors?id=H"))
        assert "nodes" in nb and "edges" in nb and nb["nodes"]
    finally:
        srv.shutdown()


def test_site_server_lazy_routes(store):
    store.upsert_repo(Repo(id="team/repoA", path="/a"))
    store.upsert_nodes("team/repoA", [_node("a1", repo="team/repoA")])
    port = _free_port()
    srv = viz.build_site_server(store, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # overview references the shared asset (not inlined); asset served separately
        overview = _get(base + "/")
        assert b'src="cytoscape.min.js"' in overview and b"--deepwater" not in overview
        assert b"--deepwater" in _get(base + "/app.css")
        # repo page is rendered on demand from the store
        repo = _get(base + "/repo-team__repoA.html")
        assert b"<html" in repo.lower() and b'"repo": "team/repoA"' in repo
        # unknown repo slug -> 404 (direct request; _get retries would mask it)
        try:
            urllib.request.urlopen(base + "/repo-nope.html", timeout=1)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


@pytest.mark.slow
def test_graph_and_site_servers_pin_the_host_header(store):
    """Both visualizer servers serve the same graph over the same loopback bind as
    the dashboard, so they get the dashboard's DNS-rebinding defence too -- one
    shared policy (kb/http_base.py) rather than three that drift apart."""
    _hub(store, leaves=2)
    store.upsert_repo(Repo(id="team/repoA", path="/a"))
    store.upsert_nodes("team/repoA", [_node("a1", repo="team/repoA")])
    for build in (lambda p: viz.build_graph_server(store, _payload(store), host="127.0.0.1",
                                                   port=p),
                  lambda p: viz.build_site_server(store, host="127.0.0.1", port=p)):
        port = _free_port()
        srv = build(port)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{port}"
            assert _get(base + "/")  # honest Host -> served
            for route in ("/", "/neighbors?id=H"):
                req = urllib.request.Request(base + route,
                                             headers={"Host": "evil.example.com"})
                try:
                    urllib.request.urlopen(req, timeout=5)
                    raise AssertionError(f"expected 403 for {route}")
                except urllib.error.HTTPError as e:
                    assert e.code == 403
        finally:
            srv.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, tries=50):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - server may still be starting
            last = e
            time.sleep(0.05)
    raise AssertionError(f"request failed: {last}")


# --- CLI integration -----------------------------------------------------

def _kb_config(tmp_path) -> Path:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n')
    return cfg


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def test_cli_graph_formats_and_seeds(tmp_path, capsys):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()

    # --name seed -> mermaid to stdout (charge is reachable from CatalogService)
    assert _run(["kb", "graph", "--config", str(cfg), "--name", "CatalogService", "--kind", "class",
                 "--hops", "1", "--format", "mermaid"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("graph LR") and "CatalogService" in out

    # --node seed -> json to a file
    target = tmp_path / "g.json"
    assert _run(["kb", "graph", "--config", str(cfg), "--node", "demo_app_catalogservice",
                 "--format", "json", "--output", str(target)]) == 0
    d = json.loads(target.read_text())
    assert any(n["id"] == "demo_app_catalogservice" for n in d["nodes"])

    # default html lands in --output
    html = tmp_path / "g.html"
    assert _run(["kb", "graph", "--config", str(cfg), "--search", "Order",
                 "--output", str(html)]) == 0
    assert html.exists() and _CDN_URL not in html.read_text()


def test_cli_graph_reports_what_the_diagram_drew_not_what_it_queried(tmp_path, capsys):
    # An erdiagram of a repo with no SQL DDL is a 200-odd-byte "nothing to draw"
    # comment, yet the console reported the QUERY's node/edge counts as a success:
    # a script trusting the printed numbers would ship an empty diagram.
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    capsys.readouterr()
    out = tmp_path / "er.mmd"
    assert _run(["kb", "graph", "--config", str(cfg), "--repo", "demo/app",
                 "--format", "erdiagram", "--output", str(out)]) == 0
    text = capsys.readouterr().out
    assert "no table/view definitions in this view" in out.read_text(encoding="utf-8")
    assert "(0 nodes, 0 edges)" in text
    assert "nothing in this view for erdiagram to draw" in text
    assert "(2 nodes, 1 edges)" not in text          # the view's counts, not the file's

    # a format that DOES draw this view still reports its real counts
    mm = tmp_path / "g.mmd"
    assert _run(["kb", "graph", "--config", str(cfg), "--repo", "demo/app",
                 "--format", "mermaid", "--output", str(mm)]) == 0
    assert "(2 nodes, 1 edges)" in capsys.readouterr().out


def test_cli_graph_site_honours_cdn(tmp_path):
    # --cdn used to be dropped on the --site path: the export still vendored all
    # three JS libs and carried no CDN reference at all, so a user building for a
    # bandwidth-constrained host silently got the fat offline build instead.
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    out = tmp_path / "site"
    assert _run(["kb", "graph", "--config", str(cfg), "--site", str(out), "--cdn"]) == 0
    overview = (out / "overview.html").read_text(encoding="utf-8")
    assert _CDN_URL in overview
    assert '<script src="cytoscape.min.js"></script>' not in overview
    assert not (out / "cytoscape.min.js").exists()   # nothing vendored
    # contextlake's own assets have no CDN and stay local either way
    assert (out / "app.js").is_file() and (out / "app.css").is_file()


def test_cli_graph_site_defaults_to_the_offline_build(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    out = tmp_path / "site"
    assert _run(["kb", "graph", "--config", str(cfg), "--site", str(out)]) == 0
    assert (out / "cytoscape.min.js").is_file()
    assert _CDN_URL not in (out / "overview.html").read_text(encoding="utf-8")


def test_cli_graph_requires_a_seed(tmp_path):
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "index", "--config", str(cfg), "--source", str(FIXTURE)]) == 0
    assert _run(["kb", "graph", "--config", str(cfg)]) == 2  # no seed -> usage error


def test_cli_graph_overview_on_empty_store_warns_with_index_hint(tmp_path, capsys):
    # An empty overview means nothing was indexed yet -- writing a 0-node HTML and
    # calling it a success would hide that from the user (same trap cmd_index's
    # empty-workspace guard avoids). Nothing has been indexed for this config.
    # capsys (not gls_logs) matches this file's convention for commands invoked
    # through main(), which rebinds the console handler via setup_logging().
    cfg = _kb_config(tmp_path)
    assert _run(["kb", "graph", "--config", str(cfg), "--overview"]) == 0

    text = capsys.readouterr().out
    out = tmp_path / "kb" / "graphs" / "overview.html"
    assert out.exists()  # the artifact is still written -- the guard only fixes reporting
    assert style.warn(f"Wrote html (0 nodes, 0 edges) -> {out}: the store is empty.") in text
    assert "Run `contextlake kb index` first" in text
    assert style.ok(f"Wrote html (0 nodes, 0 edges) -> {out}") not in text  # not a bare success


# --- class diagram --------------------------------------------------------

def _class_payload():
    prov = Provenance(source_file="m.py", source_line=1, verified_at=date(2026, 6, 21))
    nodes = [
        Node(id="base", repo="r", kind="class", name="BaseController"),
        Node(id="sub", repo="r", kind="class", name="OrdersController"),
        Node(id="iface", repo="r", kind="interface", name="Named"),
        Node(id="m1", repo="r", kind="method", name="handle",
             attrs={"signature": "(self, req)"}),
        Node(id="f", repo="r", kind="file", name="m.py"),
    ]
    edges = [
        Edge(src="base", dst="m1", relation="contains", confidence=Confidence.EXTRACTED,
             provenance=prov),
        Edge(src="sub", dst="base", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
        Edge(src="sub", dst="iface", relation="inherits", confidence=Confidence.INFERRED,
             provenance=prov),
        # a calls edge must NOT appear in a class diagram
        Edge(src="sub", dst="base", relation="calls", confidence=Confidence.INFERRED,
             provenance=prov),
    ]
    return viz.to_payload(nodes, edges)


def test_class_diagram_structure():
    out = viz.to_class_diagram(_class_payload())
    assert out.startswith("classDiagram")
    # each classifier is declared (files excluded)
    assert out.count("class c") == 3
    assert '["BaseController"]' in out and '["Named"]' in out
    assert "m.py" not in out                    # file node dropped
    # BaseController owns handle(self, req) as a member
    assert "+handle(self, req)" in out
    # extends -> solid, implements (interface) -> dotted
    assert "<|--" in out and "<|.." in out
    # a calls edge never becomes an association here
    assert out.count("<|") == 2 and "calls" not in out
    assert "-->" not in out


def test_class_diagram_interface_stereotype():
    out = viz.to_class_diagram(_class_payload())
    assert "<<interface>>" in out


def test_class_diagram_empty_when_no_classifiers():
    payload = viz.to_payload([Node(id="f", repo="r", kind="file", name="a.py")], [])
    out = viz.to_class_diagram(payload)
    assert out.startswith("classDiagram") and "no classes" in out


# --- sequence diagram -------------------------------------------------------

def _seq_prov(line):
    return Provenance(source_file="m.py", source_line=line, verified_at=date(2026, 6, 21))


def test_sequence_diagram_orders_by_call_site_line_not_edge_order():
    nodes = [Node(id="a", repo="r", kind="function", name="main"),
             Node(id="b", repo="r", kind="function", name="second"),
             Node(id="c", repo="r", kind="function", name="first")]
    # edges deliberately listed out of source order -- output must still be by line
    edges = [
        Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(9)),
        Edge(src="a", dst="c", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(3)),
    ]
    payload = viz.to_payload(nodes, edges, meta={"seed_ids": ["a"]})
    out = viz.to_sequence_diagram(payload)
    assert out.startswith("sequenceDiagram")
    assert out.index("first()") < out.index("second()")   # line 3 before line 9


def test_sequence_diagram_multi_hop_walks_callees():
    nodes = [Node(id="a", repo="r", kind="function", name="a"),
             Node(id="b", repo="r", kind="function", name="b"),
             Node(id="c", repo="r", kind="function", name="c")]
    edges = [
        Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(1)),
        Edge(src="b", dst="c", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(1)),
    ]
    payload = viz.to_payload(nodes, edges, meta={"seed_ids": ["a"]})
    out = viz.to_sequence_diagram(payload)
    assert "->>" in out and out.count("->>") == 2
    assert "c()" in out   # second hop reached, not just direct calls


def test_sequence_diagram_recursion_does_not_infinite_loop():
    nodes = [Node(id="a", repo="r", kind="function", name="a"),
             Node(id="b", repo="r", kind="function", name="b")]
    edges = [
        Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(1)),
        Edge(src="b", dst="a", relation="calls", confidence=Confidence.INFERRED,
             provenance=_seq_prov(2)),
    ]
    payload = viz.to_payload(nodes, edges, meta={"seed_ids": ["a"]})
    out = viz.to_sequence_diagram(payload)   # must return, not hang
    assert out.count("->>") == 2   # a->b, b->a -- then a is already on the path, stop


def test_sequence_diagram_requires_exactly_one_seed():
    nodes = [Node(id="a", repo="r", kind="function", name="a")]
    no_seed = viz.to_payload(nodes, [], meta={})
    two_seeds = viz.to_payload(nodes, [], meta={"seed_ids": ["a", "b"]})
    for payload in (no_seed, two_seeds):
        out = viz.to_sequence_diagram(payload)
        assert out.startswith("sequenceDiagram") and "one seed" in out


def test_sequence_diagram_truncates_with_a_note_not_silently():
    nodes = [Node(id="a", repo="r", kind="function", name="a")] + [
        Node(id=f"b{i}", repo="r", kind="function", name=f"b{i}") for i in range(5)]
    edges = [Edge(src="a", dst=f"b{i}", relation="calls", confidence=Confidence.INFERRED,
                  provenance=_seq_prov(i)) for i in range(5)]
    payload = viz.to_payload(nodes, edges, meta={"seed_ids": ["a"]})
    out = viz.to_sequence_diagram(payload, max_messages=2)
    assert out.count("->>") == 2
    assert "truncated" in out


def _state_node(name, entity, repo="r"):
    return Node(id=f"{entity}.{name}", repo=repo, kind="state", name=name,
                qualified_name=f"{entity}.{name}", attrs={"entity": entity})


def _state_edge(src, dst, method, line=1):
    return Edge(src=src, dst=dst, relation="transitions_to", confidence=Confidence.INFERRED,
                context=method, provenance=_seq_prov(line))


def test_state_diagram_single_entity_renders_flat():
    created, paid, shipped = (_state_node(n, "Order") for n in ("Created", "Paid", "Shipped"))
    edges = [_state_edge(created.id, paid.id, "pay"), _state_edge(paid.id, shipped.id, "ship")]
    payload = viz.to_payload([created, paid, shipped], edges)
    out = viz.to_state_diagram(payload)
    assert out.startswith("stateDiagram-v2")
    assert "Created --> Paid : pay" in out
    assert "Paid --> Shipped : ship" in out
    assert "state Order {" not in out   # single entity -> flat, no composite wrapper


def test_state_diagram_multiple_entities_get_composite_blocks():
    o_created, o_paid = _state_node("Created", "Order"), _state_node("Paid", "Order")
    i_draft, i_issued = _state_node("Draft", "Invoice"), _state_node("Issued", "Invoice")
    edges = [_state_edge(o_created.id, o_paid.id, "pay"),
             _state_edge(i_draft.id, i_issued.id, "issue")]
    payload = viz.to_payload([o_created, o_paid, i_draft, i_issued], edges)
    out = viz.to_state_diagram(payload)
    assert "state Order {" in out and "state Invoice {" in out
    assert "Created --> Paid : pay" in out
    assert "Draft --> Issued : issue" in out


def test_state_diagram_unreached_value_still_appears_unconnected():
    known, reached = _state_node("Known", "Order"), _state_node("Reached", "Order")
    orphan = _state_node("Orphan", "Order")  # a value the code never transitions to/from
    payload = viz.to_payload([known, reached, orphan], [_state_edge(known.id, reached.id, "go")])
    out = viz.to_state_diagram(payload)
    assert "Orphan" in out
    assert "Orphan -->" not in out and "--> Orphan" not in out


def test_state_diagram_empty_view_says_so_not_silently():
    out = viz.to_state_diagram(viz.to_payload([], []))
    assert out.startswith("stateDiagram-v2") and "no state transitions" in out


def _sql_node(name, kind, repo="r"):
    return Node(id=f"{repo}::{name}", repo=repo, kind=kind, name=name,
                qualified_name=f"schema.sql::{name}")


def _ref_edge(src, dst):
    return Edge(src=src, dst=dst, relation="references", confidence=Confidence.INFERRED,
               provenance=_seq_prov(1))


def test_er_diagram_renders_fk_as_parent_to_child_cardinality():
    customers, orders = _sql_node("customers", "table"), _sql_node("orders", "table")
    payload = viz.to_payload([customers, orders], [_ref_edge(orders.id, customers.id)])
    out = viz.to_er_diagram(payload)
    assert out.startswith("erDiagram")
    # a REFERENCES clause points child -> parent; FK semantics mean one parent row
    # to many child rows, so the parent must be on the "one" side of the notation.
    assert "customers ||--o{ orders : references" in out


def test_er_diagram_includes_a_view_with_no_fk_as_a_bare_entity():
    summary = _sql_node("order_summary", "view")
    payload = viz.to_payload([summary], [])
    out = viz.to_er_diagram(payload)
    assert "order_summary" in out
    assert "||--o{" not in out


def test_er_diagram_ignores_non_reference_edges_and_non_table_nodes():
    a = Node(id="a", repo="r", kind="class", name="A")
    b = Node(id="b", repo="r", kind="class", name="B")
    edges = [Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
                  provenance=_seq_prov(1))]
    payload = viz.to_payload([a, b], edges)
    out = viz.to_er_diagram(payload)
    assert out.startswith("erDiagram") and "no table/view definitions" in out


def test_er_diagram_dedupes_a_repeated_fk_reference():
    customers, orders = _sql_node("customers", "table"), _sql_node("orders", "table")
    edges = [_ref_edge(orders.id, customers.id), _ref_edge(orders.id, customers.id)]
    payload = viz.to_payload([customers, orders], edges)
    out = viz.to_er_diagram(payload)
    assert out.count("customers ||--o{ orders : references") == 1


def test_er_diagram_empty_view_explains_orm_only_schemas_not_silently():
    out = viz.to_er_diagram(viz.to_payload([], []))
    assert out.startswith("erDiagram")
    assert "no table/view definitions" in out


def _hcl_node(address, kind="resource", repo="r"):
    return Node(id=f"{repo}::{address}", repo=repo, kind=kind, name=address,
               qualified_name=f"main.tf::{address}", lang="hcl")


def _depends_on_edge(src, dst):
    return Edge(src=src, dst=dst, relation="depends_on", confidence=Confidence.INFERRED,
               provenance=_seq_prov(1))


def test_deployment_diagram_groups_by_inferred_resource_category():
    vpc = _hcl_node("aws_vpc.main")
    subnet = _hcl_node("aws_subnet.web")
    sg = _hcl_node("aws_security_group.web_sg")
    payload = viz.to_payload([vpc, subnet, sg], [
        _depends_on_edge(subnet.id, vpc.id), _depends_on_edge(sg.id, vpc.id),
    ])
    out = viz.to_deployment_diagram(payload)
    assert out.startswith("graph TD")
    assert "subgraph network" in out
    assert "subgraph security" in out
    assert '"aws_vpc.main"' in out and '"aws_subnet.web"' in out


def test_deployment_diagram_a_db_instance_is_database_not_compute():
    """Regression: "instance" (compute's keyword) is a substring of "db_instance",
    so a naive first-match-wins scan would wrongly file a database resource under
    compute -- caught live before shipping, fixed by checking database first.
    Paired with a real compute resource so the two subgraphs actually render
    (a single-category view is flat and would let this pass vacuously)."""
    db = _hcl_node("aws_db_instance.orders")
    lam = _hcl_node("aws_lambda_function.worker")
    payload = viz.to_payload([db, lam], [])
    out = viz.to_deployment_diagram(payload)
    assert "subgraph database" in out
    assert "subgraph compute" in out
    db_section = out.split("subgraph database")[1].split("end")[0]
    assert '"aws_db_instance.orders"' in db_section
    assert '"aws_lambda_function.worker"' not in db_section


def test_deployment_diagram_single_category_renders_flat_no_subgraph():
    vpc, subnet = _hcl_node("aws_vpc.main"), _hcl_node("aws_subnet.web")
    payload = viz.to_payload([vpc, subnet], [_depends_on_edge(subnet.id, vpc.id)])
    out = viz.to_deployment_diagram(payload)
    assert "subgraph" not in out
    assert "n0 --> n1" in out or "n1 --> n0" in out


def test_deployment_diagram_ignores_non_depends_on_edges_and_non_hcl_nodes():
    a = Node(id="a", repo="r", kind="class", name="A")
    b = Node(id="b", repo="r", kind="class", name="B")
    edges = [Edge(src="a", dst="b", relation="calls", confidence=Confidence.INFERRED,
                  provenance=_seq_prov(1))]
    payload = viz.to_payload([a, b], edges)
    out = viz.to_deployment_diagram(payload)
    assert out.startswith("graph TD") and "no Terraform" in out


def test_deployment_diagram_module_nodes_get_their_own_category():
    mod = _hcl_node("module.vpc", kind="module")
    payload = viz.to_payload([mod], [])
    out = viz.to_deployment_diagram(payload)
    assert '"module.vpc"' in out


def test_deployment_diagram_ignores_non_hcl_module_nodes():
    """Regression: kind="module" isn't exclusive to Terraform -- kb/parse.py emits
    kind="module" package nodes for every code language (lang="python"/"js"/etc).
    A repo with both Terraform AND regular source files must not leak unrelated
    source-module nodes into the deployment diagram."""
    py_mod = Node(id="app_mod", repo="r", kind="module", name="app", lang="python")
    vpc = _hcl_node("aws_vpc.main")
    payload = viz.to_payload([py_mod, vpc], [])
    out = viz.to_deployment_diagram(payload)
    assert "app" not in out
    assert '"aws_vpc.main"' in out
    assert "subgraph" not in out  # single real (HCL) category -> flat


def test_deployment_diagram_data_block_categorized_by_its_type_not_the_literal_data_prefix():
    """Regression: kb/hcl.py addresses a `data` block as `data.<type>.<name>`
    (_address_for_block), so naively splitting on the first dot yields the
    literal string "data" -- which matches no keyword and would silently file
    every data resource under "other" regardless of its real type."""
    db = _hcl_node("data.aws_db_instance.orders", kind="data")
    lam = _hcl_node("aws_lambda_function.worker")
    payload = viz.to_payload([db, lam], [])
    out = viz.to_deployment_diagram(payload)
    assert "subgraph database" in out
    assert "subgraph other" not in out
    db_section = out.split("subgraph database")[1].split("end")[0]
    assert '"data.aws_db_instance.orders"' in db_section


def test_deployment_diagram_empty_view_explains_terraform_only_not_silently():
    out = viz.to_deployment_diagram(viz.to_payload([], []))
    assert out.startswith("graph TD")
    assert "no Terraform" in out
    assert ".tf files" in out


def _write_dense_repo_config(tmp_path, leaves=20):
    kb = tmp_path / "kb"
    kb.mkdir()
    s = SqliteStore(kb / "index.sqlite")
    _dense_hub(s, leaves=leaves)
    s.close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{kb.as_posix()}"\n')
    return cfg


def test_cli_json_format_is_not_edge_capped_by_default(tmp_path, capsys):
    # `--format html`/`json`/`dot` render via cytoscape/DOT, which have no
    # Mermaid-style hard edge limit -- they must keep showing every edge among
    # the capped nodes exactly as before the Mermaid edge-cap fix, unless the
    # user explicitly passes --max-edges.
    cfg = _write_dense_repo_config(tmp_path, leaves=20)  # 20 + 20*19 = 400 edges
    with pytest.raises(SystemExit) as e:
        main(["kb", "graph", "--repo", "r", "--format", "json", "--config", str(cfg)])
    assert e.value.code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["edges"]) == 400  # uncapped


def test_cli_mermaid_format_is_edge_capped_by_default(tmp_path, capsys):
    cfg = _write_dense_repo_config(tmp_path, leaves=20)
    with pytest.raises(SystemExit) as e:
        main(["kb", "graph", "--repo", "r", "--format", "mermaid", "--config", str(cfg)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.count("-->") == 400  # the default max_edges cap for Mermaid formats


def test_cli_explicit_max_edges_overrides_default_for_any_format(tmp_path, capsys):
    # an explicit --max-edges is honored even for a format that doesn't default
    # to capping (json/html/dot) -- the user asked for it, so apply it.
    cfg = _write_dense_repo_config(tmp_path, leaves=20)
    with pytest.raises(SystemExit) as e:
        main(["kb", "graph", "--repo", "r", "--format", "json", "--max-edges", "10",
              "--config", str(cfg)])
    assert e.value.code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed["edges"]) == 10


def test_text_format_to_stdout_is_not_log_polluted_under_truncation(tmp_path, capsys):
    # Regression: the truncation warning (and any payload-building log) used to land
    # on stdout, corrupting a redirected `--format json|mermaid|classdiagram` payload.
    # With a repo bigger than --max-nodes, the warning fires; stdout must stay clean.
    kb = tmp_path / "kb"
    kb.mkdir()
    s = SqliteStore(kb / "index.sqlite")
    s.upsert_nodes("r", [_node(f"C{i}", kind="class") for i in range(5)])
    s.close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{kb.as_posix()}"\n')

    with pytest.raises(SystemExit) as e:
        main(["kb", "graph", "--repo", "r", "--format", "json", "--max-nodes", "1",
              "--config", str(cfg)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    # the payload must be valid JSON with nothing prepended (no timestamped log line)
    parsed = json.loads(out)
    assert "nodes" in parsed and out.lstrip().startswith("{")


def test_config_warning_does_not_corrupt_json_stdout(tmp_path, capsys):
    # Regression: an unknown-config-key WARNING logged while opening the store used
    # to land on stdout before use_stderr(), corrupting a --format json payload.
    kb = tmp_path / "kb"
    kb.mkdir()
    s = SqliteStore(kb / "index.sqlite")
    s.upsert_nodes("r", [_node("C", kind="class")])
    s.close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{kb.as_posix()}"\nbadkey = 1\n')  # triggers a warning

    with pytest.raises(SystemExit) as e:
        main(["kb", "graph", "--repo", "r", "--format", "json", "--config", str(cfg)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)  # must parse: no warning prepended
    assert "nodes" in parsed and out.lstrip().startswith("{")


# --- image export: PNG (canvas) + SVG (vector, card-aware) ----------------

def test_page_offers_both_a_png_and_an_svg_export(store):
    _hub(store, leaves=3)
    html = viz.to_html(_payload(store))
    assert 'id="png"' in html and 'id="svg"' in html
    # PNG still goes through cytoscape's own canvas renderer, with the same options…
    assert 'cy.png({ full:true, scale:2, bg:"#ffffff" })' in html
    # …wrapped so the dagre preview's card rendering is reverted for the capture only
    assert "withCanvasNodes" in html and 'cy.nodes(".cl-dom")' in html
    # SVG is hand-rolled: foreignObject for the cards, vector shapes otherwise
    assert "foreignObject" in html and "XMLSerializer" in html
    # both exporters are callable without going through a download
    assert "window.clExport" in html


def _chrome_binary():
    """A local Chrome/Chromium, or None — the export test needs a real renderer."""
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


_HARNESS = """
<script>
(function(){
  function out(id, txt){
    var d = document.createElement("div"); d.id = id; d.textContent = txt;
    document.body.appendChild(d);
  }
  function b64(s){ return btoa(unescape(encodeURIComponent(s))); }
  function grab(tag){
    var st = { render: document.body.dataset.render || "canvas",
               cards: cy.nodes(".cl-dom").length, restored: true };
    var png = window.clExport.pngDataUri();
    st.cardsAfter = cy.nodes(".cl-dom").length;
    cy.nodes(".cl-dom").forEach(function(n){
      var el = n.data("dom");
      if(!el || Math.abs(n.width() - el.offsetWidth) > 1
             || Math.abs(n.height() - el.offsetHeight) > 1){ st.restored = false; }
    });
    out("exp-" + tag + "-png", png);
    out("exp-" + tag + "-svg", b64(window.clExport.svgText()));
    out("exp-" + tag + "-state", JSON.stringify(st));
  }
  setTimeout(function(){
    grab("canvas");
    var sel = document.getElementById("layout");
    sel.value = "dagre";
    sel.dispatchEvent(new Event("change"));
    setTimeout(function(){ grab("cards"); }, 600);
  }, 300);
})();
</script>
</body>"""


def _dump_dom(chrome, page, profile):
    import subprocess
    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
         f"--user-data-dir={profile}", "--virtual-time-budget=20000", "--dump-dom",
         page.as_uri()],
        capture_output=True, text=True, timeout=300)
    return proc.stdout


def _grab(dom, div_id):
    m = re.search(rf'<div id="{div_id}">([^<]*)</div>', dom)
    assert m, f"{div_id} missing from the rendered page"
    return m.group(1)


@pytest.mark.skipif(_chrome_binary() is None,
                    reason="no Chrome/Chromium available to render the page")
def test_png_and_svg_exports_produce_real_output_in_both_render_modes(store, tmp_path):
    """Both exports, on a real browser render, in the canvas mode AND the card mode.

    The PNG assertion that matters is not "it decodes" but that card mode survives it:
    a canvas-only capture has to blank the cards' geometry and put it back.
    """
    import base64
    import struct
    import xml.etree.ElementTree as ET

    _hub(store, leaves=3)
    page = tmp_path / "graph.html"
    page.write_text(viz.to_html(_payload(store)).replace("</body>", _HARNESS),
                    encoding="utf-8")
    dom = _dump_dom(_chrome_binary(), page, tmp_path / "profile")

    svgns = "{http://www.w3.org/2000/svg}"
    for tag in ("canvas", "cards"):
        state = json.loads(_grab(dom, f"exp-{tag}-state"))
        # PNG: a real, non-empty image
        raw = base64.b64decode(_grab(dom, f"exp-{tag}-png").split(",", 1)[1])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", raw[16:24])
        assert w > 0 and h > 0 and len(raw) > 1000
        # SVG: well-formed XML carrying the graph's nodes and edges
        svg = base64.b64decode(_grab(dom, f"exp-{tag}-svg"))
        root = ET.fromstring(svg)
        assert root.tag == f"{svgns}svg"
        edges = root.findall(f".//{svgns}g[@class='edges']/{svgns}path")
        nodes = root.findall(f".//{svgns}g[@class='nodes']/{svgns}g[@class='node']")
        assert len(edges) == 3 and len(nodes) == 4        # the synthetic hub + 3 leaves
        cards = root.findall(f".//{svgns}foreignObject")

        if tag == "canvas":
            assert state["cards"] == 0 and state["render"] == "canvas"
            assert not cards                              # vector shapes, no HTML
            assert root.findall(f".//{svgns}ellipse")
        else:
            # card mode really engaged, and the PNG capture left it exactly as it was.
            # BOTH halves are load-bearing and were verified by sabotage: dropping the
            # class re-add gives cardsAfter == 0, dropping the size re-apply gives
            # restored is False. cardsAfter is not redundant — "restored" is computed by
            # iterating .cl-dom, so it stays vacuously true when the class is never
            # restored and nothing is iterated.
            assert state["render"] == "cards" and state["cards"] == 4
            assert state["cardsAfter"] == 4 and state["restored"] is True
            assert len(cards) == 4                        # every node kept its HTML card
            assert b"cl-card" in svg and b"box-shadow" in svg
