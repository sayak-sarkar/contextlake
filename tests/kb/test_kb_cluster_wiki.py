"""Tests for the fleet / namespace-level cluster wiki."""

from datetime import date

from contextlake.kb.ids import make_id
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.cluster import (
    cluster_fingerprint,
    cluster_page_name,
    generate_cluster_page,
    members,
    namespace_brief,
    render_cluster_prompt,
    split_edges,
)

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


def test_members_filters_by_namespace_prefix(tmp_path):
    s = _seed(tmp_path)
    try:
        assert members(s, "acme/pay") == ["acme/pay/api", "acme/pay/core", "acme/pay/web"]
        assert "acme/ship/api" not in members(s, "acme/pay")
    finally:
        s.close()


def test_split_edges_internal_vs_boundary():
    member_set = {"acme/pay/api", "acme/pay/web", "acme/pay/core"}
    edges = [
        {"src": "acme/pay/web", "dst": "acme/pay/api", "flavor": "http"},   # internal
        {"src": "acme/pay/api", "dst": "acme/pay/core", "flavor": "depends"},  # internal
        {"src": "acme/ship/api", "dst": "acme/pay/web", "flavor": "http"},  # boundary
        {"src": "x/a", "dst": "x/b", "flavor": "http"},                     # neither
    ]
    internal, boundary = split_edges(edges, member_set)
    assert len(internal) == 2 and len(boundary) == 1
    assert boundary[0]["src"] == "acme/ship/api"


def test_namespace_brief_composes_members_and_edges(tmp_path):
    s = _seed(tmp_path)
    try:
        brief = namespace_brief(s, tmp_path, "acme/pay")
    finally:
        s.close()
    assert brief is not None
    assert brief["namespace"] == "acme/pay" and brief["member_count"] == 3
    assert {r["repo"] for r in brief["repos"]} == {"acme/pay/api", "acme/pay/core", "acme/pay/web"}
    # the web->api HTTP flow is internal; the ship/api->web flow is boundary
    internal = {(e["src"], e["dst"]) for e in brief["internal_edges"]}
    assert ("acme/pay/web", "acme/pay/api") in internal
    assert any(e["src"] == "acme/ship/api" for e in brief["boundary_edges"])


def test_cluster_page_name_and_fingerprint(tmp_path):
    assert cluster_page_name("acme/pay") == "_ns__acme__pay.md"
    assert cluster_page_name("delivery/dcs/") == "_ns__delivery__dcs.md"
    fp1 = cluster_fingerprint({"heads": {"a": "1", "b": "2"}})
    fp2 = cluster_fingerprint({"heads": {"b": "2", "a": "1"}})  # order-independent
    assert fp1 == fp2 and len(fp1) == 12


def test_namespace_brief_none_for_empty_namespace(tmp_path):
    s = _seed(tmp_path)
    try:
        assert namespace_brief(s, tmp_path, "nope/missing") is None
    finally:
        s.close()


class _FakeLlm:
    name = "fake"

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:            # council reviewer -> accept
            return '{"score": 0.95, "issues": []}'
        return "## Overview\nThe pay cluster.\n"


def test_render_cluster_prompt_phrases_internal_and_boundary():
    brief = {
        "namespace": "acme/pay", "member_count": 3, "truncated": False,
        "repos": [{"repo": "acme/pay/api", "langs": {"csharp": 3}, "top": ["OrderSvc"]},
                  {"repo": "acme/pay/web", "langs": {"typescript": 2}, "top": ["App"]}],
        "internal_edges": [{"src": "acme/pay/web", "dst": "acme/pay/api",
                            "flavor": "http", "weight": 2}],
        "boundary_edges": [{"src": "acme/ship/api", "dst": "acme/pay/web",
                            "flavor": "http", "weight": 1}],
        "heads": {"acme/pay/api": "a1", "acme/pay/web": "w1"},
    }
    p = render_cluster_prompt(brief)
    assert "acme/pay/web calls acme/pay/api over HTTP" in p
    assert "Couples to repositories outside this namespace" in p
    assert "do not speculate or invent any coupling not listed" in p


def test_render_cluster_prompt_no_coupling_fallback():
    brief = {"namespace": "acme/pay", "member_count": 2, "truncated": False,
             "repos": [{"repo": "acme/pay/a", "langs": {}, "top": []}],
             "internal_edges": [], "boundary_edges": [], "heads": {"acme/pay/a": "x"}}
    p = render_cluster_prompt(brief)
    assert "not detected" in p and "Do NOT invent" in p


def test_generate_cluster_page_has_body_and_fingerprint_footer():
    brief = {"namespace": "acme/pay", "member_count": 2, "truncated": False,
             "repos": [{"repo": "acme/pay/api", "langs": {}, "top": []}],
             "internal_edges": [], "boundary_edges": [],
             "heads": {"acme/pay/api": "a1", "acme/pay/web": "w1"}}
    page = generate_cluster_page(_FakeLlm(), brief)
    assert page.startswith("# acme/pay (cluster)")
    assert "The pay cluster." in page
    assert "cluster-commits:" in page and "`acme/pay/api`" in page


# --- command wiring -------------------------------------------------------

def _ns_args(tmp_path, **over):
    from argparse import Namespace
    base = dict(config=str(tmp_path / "kb.toml"), namespace=None, namespaces=False,
                depth=None, force=False, llm=None, llm_model=None,
                workspace=None, source=None, args=[])
    base.update(over)
    return Namespace(**base)


def _setup_cluster_store(tmp_path, monkeypatch):
    import contextlake.kb.llm as llm_pkg
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    _seed(store_dir).close()
    cfg = (f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n'
           '[llm]\nenabled = true\nprovider = "ollama"\n')
    (tmp_path / "kb.toml").write_text(cfg)
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: _FakeLlm())
    return store_dir


def test_cmd_wiki_namespace_writes_and_skips(tmp_path, monkeypatch):
    from contextlake.kb.commands import cmd_wiki
    store_dir = _setup_cluster_store(tmp_path, monkeypatch)
    assert cmd_wiki(_ns_args(tmp_path, namespace="acme/pay")) == 0
    page = store_dir / "wiki" / "_ns__acme__pay.md"
    assert page.exists()
    txt = page.read_text()
    assert "# acme/pay (cluster)" in txt and "cluster-commits:" in txt
    # second run, unchanged fingerprint -> skipped (page not rewritten)
    mtime = page.stat().st_mtime
    assert cmd_wiki(_ns_args(tmp_path, namespace="acme/pay")) == 0
    assert page.stat().st_mtime == mtime


def test_cmd_wiki_namespaces_depth_generates_per_namespace(tmp_path, monkeypatch):
    from contextlake.kb.commands import cmd_wiki
    store_dir = _setup_cluster_store(tmp_path, monkeypatch)
    assert cmd_wiki(_ns_args(tmp_path, namespaces=True, depth=2)) == 0
    wiki = store_dir / "wiki"
    assert (wiki / "_ns__acme__pay.md").exists()
    assert (wiki / "_ns__acme__ship.md").exists()
