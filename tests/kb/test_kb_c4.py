"""Tests for the C4 namespace-boundary data model (kb.c4) and its CLI wiring
(``contextlake graph --c4``)."""

import json
import re
from datetime import date

import pytest

from contextlake.cli import main
from contextlake.kb import c4
from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.visualize import _CDN_URL

_PROV = Provenance(source_file="x", source_line=1, verified_at=date(2026, 6, 21))


def _fnode(rid, name):
    return Node(id=make_id(rid, name), repo=rid, kind="file", name=name)


def _edge(rid, name, dst, relation):
    return Edge(src=make_id(rid, name), dst=dst, relation=relation,
                confidence=Confidence.INFERRED, provenance=_PROV)


def _seed(store_dir):
    """3 repos under acme/pay + 1 outside; web->api HTTP (internal),
    ship/api->web HTTP (boundary), web-> an unresolved external call (C1, off
    by default -- c1=False callers never see it). Each repo also gets a shard
    for its brief."""
    s = SqliteStore(store_dir / "index.sqlite")
    for rid in ("acme/pay/api", "acme/pay/web", "acme/pay/core", "acme/ship/api"):
        s.upsert_repo(Repo(id=rid, path=f"/repos/{rid}"))
    ep_e = make_id("endpoint", "/orders")
    ep_f = make_id("endpoint", "/ship")
    ep_x = make_id("endpoint", "/v1/charges")  # never exposed by anyone
    # api exposes /orders
    s.upsert_nodes("acme/pay/api",
                   [_fnode("acme/pay/api", "ctrl"),
                    Node(id=ep_e, repo="acme/pay/api", kind="endpoint", name="/orders")])
    s.upsert_edges("acme/pay/api", [_edge("acme/pay/api", "ctrl", ep_e, "exposes")])
    # web calls /orders (-> internal web->api), exposes /ship, and calls an
    # external system (Stripe) that nothing in the fleet exposes
    s.upsert_nodes("acme/pay/web",
                   [_fnode("acme/pay/web", "app"),
                    Node(id=ep_f, repo="acme/pay/web", kind="endpoint", name="/ship")])
    s.upsert_edges("acme/pay/web",
                   [_edge("acme/pay/web", "app", ep_e, "calls_http"),
                    _edge("acme/pay/web", "app", ep_f, "exposes"),
                    Edge(src=make_id("acme/pay/web", "app"), dst=ep_x, relation="calls_http",
                         confidence=Confidence.INFERRED, provenance=_PROV,
                         attrs={"raw_host": "api.stripe.com"})])
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


def test_c4_model_c1_off_by_default_has_no_systems(tmp_path):
    """The seed data includes an unresolved external call (web -> Stripe), but
    c1 defaults to False, so ordinary --c4 output must stay identical to
    before this feature existed."""
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)
    assert model.systems == []
    assert not any(e.flavor == "external" for e in model.edges)
    assert model.meta["system_count"] == 0


def test_c4_model_c1_true_adds_the_external_system(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2, c1=True)
    assert len(model.systems) == 1
    sysbox = model.systems[0]
    assert sysbox.label == "api.stripe.com"
    ext_edges = [e for e in model.edges if e.flavor == "external"]
    assert len(ext_edges) == 1
    e = ext_edges[0]
    assert e.src == "acme/pay/web" and e.dst == sysbox.system_id
    assert e.boundary is True  # a system is never in any namespace
    assert e.confidence == "INFERRED" and e.weight == 1
    assert model.meta["system_count"] == 1


def test_c4_model_c1_excludes_a_repo_filtered_out_by_repos(tmp_path):
    store = _seed(tmp_path)
    # scope to acme/ship only -- the caller of the external system (acme/pay/web)
    # is excluded, so the system must not appear either
    model = c4.c4_model(store, group_depth=2, repos=["acme/ship/api"], c1=True)
    assert model.systems == []


def test_c4_boundary_repo_that_is_the_exact_namespace_joins_it(tmp_path):
    """A repo literally named "acme" (the same namespace "acme/pay/api" sits
    under) used to fall into a meaningless shared "(ungrouped)" bucket instead
    of the "acme" boundary its own child repo joins -- so a real edge between
    them wrongly rendered as crossing a namespace boundary."""
    s = SqliteStore(tmp_path / "index.sqlite")
    for rid in ("acme", "acme/pay/api"):
        s.upsert_repo(Repo(id=rid, path=f"/repos/{rid}"))
    ep = make_id("endpoint", "/x")
    s.upsert_nodes("acme", [_fnode("acme", "root"),
                            Node(id=ep, repo="acme", kind="endpoint", name="/x")])
    s.upsert_edges("acme", [_edge("acme", "root", ep, "exposes")])
    s.upsert_nodes("acme/pay/api", [_fnode("acme/pay/api", "cl")])
    s.upsert_edges("acme/pay/api", [_edge("acme/pay/api", "cl", ep, "calls_http")])
    s.close()

    store = SqliteStore(tmp_path / "index.sqlite")
    model = c4.c4_model(store, group_depth=1)
    ns = {b.namespace: {c.repo_id for c in b.containers} for b in model.boundaries}
    assert ns.get("acme") == {"acme", "acme/pay/api"}
    edge = next(e for e in model.edges if e.flavor == "http")
    assert edge.boundary is False


def test_c4_boundary_two_unrelated_ungrouped_repos_are_not_one_namespace(tmp_path):
    """Two repos that only coincidentally share no real namespace (both single-
    segment, both shallower than group_depth) used to be lumped into one shared
    "(ungrouped)" bucket for boundary purposes -- so a real edge between two
    entirely unrelated repos rendered as internal, identical to a genuinely
    related same-namespace edge."""
    s = SqliteStore(tmp_path / "index.sqlite")
    for rid in ("solo1", "solo2", "acme/pay/api", "acme/billing/svc"):
        s.upsert_repo(Repo(id=rid, path=f"/repos/{rid}"))
    solo_ep = make_id("endpoint", "/solo")
    acme_ep = make_id("endpoint", "/acme")
    s.upsert_nodes("solo1", [_fnode("solo1", "cl")])
    s.upsert_edges("solo1", [_edge("solo1", "cl", solo_ep, "calls_http")])
    s.upsert_nodes("solo2", [_fnode("solo2", "root"),
                             Node(id=solo_ep, repo="solo2", kind="endpoint", name="/solo")])
    s.upsert_edges("solo2", [_edge("solo2", "root", solo_ep, "exposes")])
    s.upsert_nodes("acme/pay/api", [_fnode("acme/pay/api", "cl")])
    s.upsert_edges("acme/pay/api", [_edge("acme/pay/api", "cl", acme_ep, "calls_http")])
    s.upsert_nodes("acme/billing/svc",
                   [_fnode("acme/billing/svc", "root"),
                    Node(id=acme_ep, repo="acme/billing/svc", kind="endpoint", name="/acme")])
    s.upsert_edges("acme/billing/svc", [_edge("acme/billing/svc", "root", acme_ep, "exposes")])
    s.close()

    store = SqliteStore(tmp_path / "index.sqlite")
    model = c4.c4_model(store, group_depth=1)
    by_endpoints = {(e.src, e.dst): e for e in model.edges}
    assert by_endpoints[("solo1", "solo2")].boundary is True
    assert by_endpoints[("acme/pay/api", "acme/billing/svc")].boundary is False


def test_to_c4_dot_emits_clusters_and_labeled_edges(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2)
    dot = c4.to_c4_dot(model)
    assert dot.startswith("digraph")
    assert "subgraph cluster_" in dot          # boundaries drawn as clusters
    assert 'label="acme/pay"' in dot           # boundary label present
    assert "http x1" in dot                    # full "<flavor> x<weight>" edge label
    assert "style=dashed" in dot               # INFERRED edges render dashed
    # deterministic: two independent model builds from the same store render
    # identically -- this catches upstream nondeterminism (e.g. dict-iteration
    # order before sorting) that re-rendering the same model object can't.
    model_again = c4.c4_model(store, group_depth=2)
    assert c4.to_c4_dot(model_again) == dot


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


def test_to_c4_dot_c1_draws_the_system_outside_any_cluster(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2, c1=True)
    dot = c4.to_c4_dot(model)
    assert 'label="api.stripe.com", style=dashed' in dot
    # the system's node declaration line must sit OUTSIDE every cluster block,
    # i.e. before the first "subgraph cluster_" or after its matching close --
    # simplest robust check: it's not indented at the 4-space cluster-body
    # level the way a container node declaration is.
    sys_line = next(ln for ln in dot.splitlines() if "api.stripe.com" in ln)
    assert not sys_line.startswith("    ")
    assert "external x1" in dot  # the calls_external edge's "<flavor> x<weight>" label


def test_c4_payload_c1_system_node_has_no_parent(tmp_path):
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2, c1=True)
    payload = c4.c4_payload(model)
    systems = [n for n in payload["nodes"] if n.get("kind") == "system"]
    assert len(systems) == 1
    assert systems[0]["parent"] is None
    assert systems[0]["name"] == "api.stripe.com"


def test_c4_payload_c1_edge_join_invariant(tmp_path):
    """Same invariant as test_c4_payload_edge_join_invariant, with c1 on --
    the system node id must exactly match what the calls_external edge's dst
    resolves to after c4_payload's own sanitize_label pass (idempotency)."""
    store = _seed(tmp_path)
    model = c4.c4_model(store, group_depth=2, c1=True)
    payload = c4.c4_payload(model)
    node_ids = {n["id"] for n in payload["nodes"]}
    ext_edges = [e for e in payload["edges"] if e["context"] == "external"]
    assert len(ext_edges) == 1
    assert ext_edges[0]["dst"] in node_ids


# --- CLI wiring: `contextlake graph --c4` --------------------------------

def _seed_and_configure(tmp_path):
    """Seed the fixture store and write a kb.toml pointing at it, mirroring the
    `contextlake graph` CLI-invocation idiom in tests/kb/test_graph_command.py."""
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    _seed(store_dir).close()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    return cfg


def test_graph_c4_dot_writes_cluster_subgraph(tmp_path):
    cfg = _seed_and_configure(tmp_path)
    out = tmp_path / "c4.dot"
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2", "--format", "dot",
              "--output", str(out), "--config", str(cfg)])
    assert e.value.code == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("digraph")
    assert "subgraph cluster_" in text


def test_graph_c4_repos_filter_scopes_namespaces(tmp_path):
    """--c4 --repos scopes the model to matching repos: fnmatch("acme/pay*")
    matches all three acme/pay/* repos (the "*" wildcard also matches "/") but
    not acme/ship/api, so only the acme/pay namespace should be emitted."""
    cfg = _seed_and_configure(tmp_path)
    out = tmp_path / "c4-scoped.dot"
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2", "--repos", "acme/pay*",
              "--format", "dot", "--output", str(out), "--config", str(cfg)])
    assert e.value.code == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("digraph")
    # the matched namespace's cluster is present, with its containers
    assert "subgraph cluster_acme_pay {" in text
    assert 'label="acme/pay"' in text
    assert "acme_pay_api" in text
    # the excluded namespace (and its repo) never appear anywhere in the model
    assert "acme_ship" not in text
    assert "acme/ship" not in text


def test_graph_c4_default_html_has_vendored_cytoscape_and_parent_nodes(tmp_path):
    cfg = _seed_and_configure(tmp_path)
    out = tmp_path / "c4.html"
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2",
              "--output", str(out), "--config", str(cfg)])
    assert e.value.code == 0
    html = out.read_text(encoding="utf-8")
    # vendored (offline) cytoscape lib is inlined, not CDN-referenced -- a
    # distinctive string from the vendored asset's license header, plus the
    # sheer size, are a real signal (not just "ns:"/"parent" substrings that
    # also occur inside cytoscape.min.js/app.js boilerplate on ANY graph).
    assert _CDN_URL not in html
    assert "The Cytoscape Consortium" in html
    assert len(html) > 100_000

    # The actual C4 structure check has to look at the embedded cytoscape
    # elements payload, not the whole page -- "ns:" and '"parent"' both occur
    # in the vendored JS even for a plain, non-C4 graph, so asserting them
    # against `html` would pass whether or not the --c4 branch emitted any
    # namespace/parent structure at all. Pull out the actual `ELEMENTS` array
    # the page's inline <script> assigns (see `to_html` in
    # contextlake/kb/visualize.py, which fills the `__ELEMENTS__` template
    # placeholder with `json.dumps(_cytoscape_elements(payload))`) and parse it.
    match = re.search(r"var ELEMENTS = (.*?);\s*\n\s*var COLORS = ", html, re.DOTALL)
    assert match, "could not locate `var ELEMENTS = [...];` in the rendered HTML"
    elements = json.loads(match.group(1))

    namespace_nodes = [
        el for el in elements
        if "source" not in el["data"]
        and (str(el["data"].get("id", "")).startswith("ns:")
             or el["data"].get("kind") == "namespace")
    ]
    assert namespace_nodes, "expected at least one ns: namespace compound node"

    container_nodes = [
        el for el in elements
        if "source" not in el["data"] and el["data"].get("parent")
    ]
    namespace_ids = {n["data"]["id"] for n in namespace_nodes}
    assert any(n["data"]["parent"] in namespace_ids for n in container_nodes), (
        "expected a container node whose data.parent joins a namespace node id"
    )


def test_graph_c4_default_output_filename_is_c4_html(tmp_path):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2", "--config", str(cfg)])
    assert e.value.code == 0
    default_path = tmp_path / "kb" / "graphs" / "c4.html"
    assert default_path.exists()


def test_graph_c4_mermaid_rejected(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    diagrams_before = set((tmp_path / "kb" / "graphs").glob("*")) \
        if (tmp_path / "kb" / "graphs").exists() else set()
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--format", "mermaid", "--config", str(cfg)])
    assert e.value.code != 0
    diagrams_after = set((tmp_path / "kb" / "graphs").glob("*")) \
        if (tmp_path / "kb" / "graphs").exists() else set()
    assert diagrams_after == diagrams_before  # no diagram file written
    err = capsys.readouterr().err
    assert "not supported for --c4" in err


def test_graph_c4_erdiagram_rejected(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--format", "erdiagram", "--config", str(cfg)])
    assert e.value.code != 0
    err = capsys.readouterr().err
    assert "not supported for --c4" in err


def test_graph_c4_deployment_rejected(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--format", "deployment", "--config", str(cfg)])
    assert e.value.code != 0
    err = capsys.readouterr().err
    assert "not supported for --c4" in err


def test_graph_c4_classdiagram_rejected(tmp_path):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--format", "classdiagram", "--config", str(cfg)])
    assert e.value.code != 0


def test_graph_c4_json_format(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2", "--format", "json",
              "--config", str(cfg)])
    assert e.value.code == 0
    import json
    parsed = json.loads(capsys.readouterr().out)
    assert "nodes" in parsed and "edges" in parsed
    assert any(n.get("kind") == "namespace" for n in parsed["nodes"])


def test_graph_c1_without_c4_is_rejected(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c1", "--config", str(cfg)])
    assert e.value.code != 0
    err = capsys.readouterr().err
    assert "--c1" in err and "--c4" in err


def test_graph_c4_c1_json_includes_the_system(tmp_path, capsys):
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--c1", "--group-depth", "2", "--format", "json",
              "--config", str(cfg)])
    assert e.value.code == 0
    import json
    parsed = json.loads(capsys.readouterr().out)
    assert any(n.get("kind") == "system" and n.get("name") == "api.stripe.com"
              for n in parsed["nodes"])


def test_graph_c4_without_c1_json_has_no_system(tmp_path, capsys):
    """The seed data's unresolved external call must not leak into ordinary
    --c4 output when --c1 isn't passed."""
    cfg = _seed_and_configure(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["graph", "--c4", "--group-depth", "2", "--format", "json",
              "--config", str(cfg)])
    assert e.value.code == 0
    import json
    parsed = json.loads(capsys.readouterr().out)
    assert not any(n.get("kind") == "system" for n in parsed["nodes"])
