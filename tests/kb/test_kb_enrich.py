"""Tests for the query-driven `enrich` engine: build codebase query terms,
dispatch them to connected sources, and store the resulting documents in an
isolated `@enrich:<repo>` partition."""

from datetime import date

import contextlake.kb.connectors.enrich as enrich
from contextlake.kb.config import KbConfig, SourceCfg
from contextlake.kb.connectors.enrich import (
    build_terms,
    enrich_partition,
    enrich_repo,
    search_source,
)
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.sources.base import Document
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, read_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "group/app"


def _prov():
    return Provenance(source_file="app/main.py", verified_at=date.today())


def _seed_shard(store_dir):
    """A shard with a mix of embeddable and non-embeddable nodes, wired up with
    edges so degree-ranking has something real to rank."""
    order_service = Node(id="n1", repo=REPO, kind="class", name="OrderService",
                          file="app/order.py")
    charge_fn = Node(id="n2", repo=REPO, kind="function", name="chargeCard",
                      file="app/billing.py")
    a_module = Node(id="n3", repo=REPO, kind="module", name="app.main", file="app/main.py")
    a_file = Node(id="n4", repo=REPO, kind="file", name="app/main.py", file="app/main.py")
    nodes = [order_service, charge_fn, a_module, a_file]
    edges = [
        Edge(src="n1", dst="n2", relation="calls", confidence=Confidence.EXTRACTED,
             provenance=_prov()),
        Edge(src="n1", dst="n3", relation="imports", confidence=Confidence.EXTRACTED,
             provenance=_prov()),
        Edge(src="n2", dst="n3", relation="imports", confidence=Confidence.EXTRACTED,
             provenance=_prov()),
    ]
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="abc123", nodes=nodes, edges=edges))


# --- enrich_partition --------------------------------------------------------

def test_enrich_partition_name():
    assert enrich_partition(REPO) == "@enrich:group/app"


# --- build_terms --------------------------------------------------------------

def test_build_terms_returns_repo_name_and_embeddable_symbols(tmp_path):
    _seed_shard(tmp_path)
    terms = build_terms(tmp_path, REPO)
    assert terms[0] == "app"  # repo name leads
    assert "OrderService" in terms
    assert "chargeCard" in terms
    # non-embeddable kinds (module, file) never make the cut
    assert "app.main" not in terms
    assert "app/main.py" not in terms


def test_build_terms_capped_at_max_terms(tmp_path):
    _seed_shard(tmp_path)
    terms = build_terms(tmp_path, REPO, max_terms=2)
    assert len(terms) == 2
    assert terms[0] == "app"


def test_build_terms_no_shard_returns_empty(tmp_path):
    assert build_terms(tmp_path, "group/missing") == []


def test_build_terms_dedupes_and_preserves_order(tmp_path):
    # repo name collides with a symbol name -- must not appear twice
    nodes = [Node(id="n1", repo="group/OrderService", kind="class", name="OrderService")]
    write_shard(tmp_path, GraphShard(repo="group/OrderService", head_commit="c", nodes=nodes))
    terms = build_terms(tmp_path, "group/OrderService")
    assert terms == ["OrderService"]


# --- search_source dispatch ---------------------------------------------------

def test_search_source_tool_cfg_delegates_to_mcp_tool_query(monkeypatch):
    known = [Document(id="1", title="T", text="body", uri="u")]
    monkeypatch.setattr(enrich, "mcp_tool_query", lambda cfg, terms, timeout=None: known)
    cfg = {"tool": "search", "command": "srv"}
    assert search_source(cfg, ["Order"]) == known


def test_search_source_atlassian_cfg_normalizes_documents(monkeypatch):
    class _Stub:
        def __init__(self, *a, **k):
            pass

        def search(self, query):
            return [{"title": "Runbook", "url": "https://x/1", "text": "how to page"}]

    import contextlake.kb.connectors.atlassian as atlassian_mod
    monkeypatch.setattr(atlassian_mod, "AtlassianConnector", _Stub)

    cfg = SourceCfg(type="atlassian", name="site-a")
    docs = search_source(cfg, ["Order"])
    assert len(docs) == 1
    assert docs[0].title == "Runbook"
    assert docs[0].uri == "https://x/1"
    assert docs[0].attrs["source"] == "atlassian"
    assert "tool" not in docs[0].attrs  # mcp_query's normalizer tag is stripped


def test_search_source_unknown_type_returns_empty():
    cfg = SourceCfg(type="figma", name="design")
    assert search_source(cfg, ["Order"]) == []


def test_search_source_raising_source_returns_empty(monkeypatch):
    def boom(cfg, terms, timeout=None):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(enrich, "mcp_tool_query", boom)
    cfg = {"tool": "search", "command": "srv"}
    assert search_source(cfg, ["Order"]) == []


# --- enrich_repo ---------------------------------------------------------------

def _store(store_dir):
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    return store


def test_enrich_repo_stores_documents_with_provenance(tmp_path, monkeypatch):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        docs = [
            Document(id="d1", title="Runbook", text="how to page", uri="https://x/1"),
            Document(id="d2", title="Design doc", text="architecture notes", uri="https://x/2"),
        ]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])

        n = enrich_repo(store, store_dir, cfg, REPO)
        assert n == 2

        part = enrich_partition(REPO)
        shard = read_shard(store_dir, part)
        assert shard is not None
        assert len(shard.nodes) == 2
        for node in shard.nodes:
            assert node.kind == "document"
            assert node.file  # uri carried through
            assert node.attrs.get("source") == "atlassian"
        stored = store.get_node(f"{part}:d1")
        assert stored is not None
        assert stored.attrs.get("source") == "atlassian"
    finally:
        store.close()


def test_enrich_repo_rerun_is_idempotent_not_cumulative(tmp_path, monkeypatch):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        docs = [
            Document(id="d1", title="Runbook", text="how to page", uri="https://x/1"),
            Document(id="d2", title="Design doc", text="architecture notes", uri="https://x/2"),
        ]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])

        assert enrich_repo(store, store_dir, cfg, REPO) == 2
        assert enrich_repo(store, store_dir, cfg, REPO) == 2

        part = enrich_partition(REPO)
        shard = read_shard(store_dir, part)
        assert len(shard.nodes) == 2
    finally:
        store.close()


def test_enrich_repo_dedupes_documents_across_sources(tmp_path, monkeypatch):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        docs = [Document(id="d1", title="Runbook", text="how to page", uri="https://x/1")]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[
            SourceCfg(type="atlassian", name="site-a"),
            SourceCfg(type="atlassian", name="site-b"),
        ])

        n = enrich_repo(store, store_dir, cfg, REPO)
        assert n == 1
    finally:
        store.close()


def test_enrich_repo_skips_disabled_sources(tmp_path, monkeypatch):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        called = []

        def fake_search(src, terms, timeout=None):
            called.append(src)
            return []

        monkeypatch.setattr(enrich, "search_source", fake_search)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a", enabled=False)])

        assert enrich_repo(store, store_dir, cfg, REPO) == 0
        assert called == []
    finally:
        store.close()


def test_enrich_repo_no_sources_clears_partition_returns_zero(tmp_path):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        cfg = KbConfig(sources=[])
        assert enrich_repo(store, store_dir, cfg, REPO) == 0
        shard = read_shard(store_dir, enrich_partition(REPO))
        assert shard is not None
        assert shard.nodes == []
    finally:
        store.close()


def test_enrich_repo_no_terms_returns_zero_without_touching_store(tmp_path):
    store_dir = tmp_path / "kbstore"
    store = _store(store_dir)
    try:
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])
        assert enrich_repo(store, store_dir, cfg, "group/missing") == 0
        assert read_shard(store_dir, enrich_partition("group/missing")) is None
    finally:
        store.close()
