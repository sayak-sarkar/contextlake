"""Tests for the query-driven `enrich` engine: build codebase query terms,
dispatch them to connected sources, and store the resulting documents in an
isolated `@enrich:<repo>` partition."""

from datetime import date

import contextlake.kb.connectors.enrich as enrich
from contextlake.kb.config import KbConfig, SourceCfg
from contextlake.kb.connectors.enrich import (
    build_terms,
    enrich_partition,
    run_enrich_repo,
    search_source,
)
from contextlake.kb.ids import make_id
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
    forecast_service = Node(id="n1", repo=REPO, kind="class", name="ForecastService",
                          file="app/forecast.py")
    reading_fn = Node(id="n2", repo=REPO, kind="function", name="readSensor",
                      file="app/readings.py")
    a_module = Node(id="n3", repo=REPO, kind="module", name="app.main", file="app/main.py")
    a_file = Node(id="n4", repo=REPO, kind="file", name="app/main.py", file="app/main.py")
    nodes = [forecast_service, reading_fn, a_module, a_file]
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
    assert "ForecastService" in terms
    assert "readSensor" in terms
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
    nodes = [Node(id="n1", repo="group/ForecastService", kind="class", name="ForecastService")]
    write_shard(tmp_path, GraphShard(repo="group/ForecastService", head_commit="c", nodes=nodes))
    terms = build_terms(tmp_path, "group/ForecastService")
    assert terms == ["ForecastService"]


# --- search_source dispatch ---------------------------------------------------

def test_search_source_tool_cfg_delegates_to_mcp_tool_query(monkeypatch):
    known = [Document(id="1", title="T", text="body", uri="u")]
    monkeypatch.setattr(enrich, "mcp_tool_query", lambda cfg, terms, timeout=None: known)
    cfg = {"tool": "search", "command": "srv"}
    assert search_source(cfg, ["Reading"]) == known


def test_search_source_atlassian_cfg_normalizes_documents(monkeypatch):
    class _Stub:
        def __init__(self, *a, **k):
            pass

        def search(self, query):
            return [{"title": "Runbook", "url": "https://x/1", "text": "how to page"}]

    import contextlake.kb.connectors.atlassian as atlassian_mod
    monkeypatch.setattr(atlassian_mod, "AtlassianConnector", _Stub)

    cfg = SourceCfg(type="atlassian", name="site-a")
    docs = search_source(cfg, ["Reading"])
    assert len(docs) == 1
    assert docs[0].title == "Runbook"
    assert docs[0].uri == "https://x/1"
    assert docs[0].attrs["source"] == "atlassian"
    assert "tool" not in docs[0].attrs  # mcp_query's normalizer tag is stripped


def test_search_source_unknown_type_returns_empty():
    cfg = SourceCfg(type="figma", name="design")
    assert search_source(cfg, ["Reading"]) == []


def test_search_source_raising_source_returns_empty(monkeypatch):
    def boom(cfg, terms, timeout=None):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(enrich, "mcp_tool_query", boom)
    cfg = {"tool": "search", "command": "srv"}
    assert search_source(cfg, ["Reading"]) == []


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

        counts = run_enrich_repo(store, store_dir, cfg, REPO)
        assert counts.documents == 2

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
        assert stored.attrs.get("snippet") == "how to page"  # doc body persisted for wiki grounding
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

        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 2
        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 2

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

        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 1
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

        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 0
        assert called == []
    finally:
        store.close()


def test_enrich_repo_no_sources_clears_partition_returns_zero(tmp_path):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        cfg = KbConfig(sources=[])
        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 0
        shard = read_shard(store_dir, enrich_partition(REPO))
        assert shard is not None
        assert shard.nodes == []
    finally:
        store.close()


def test_enrich_repo_links_documents_to_the_symbols_they_mention(tmp_path, monkeypatch):
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        # the matcher reads symbols out of the index, not the shard
        store.upsert_nodes(REPO, [
            Node(id="n2", repo=REPO, kind="function", name="readSensor", file="app/readings.py"),
        ])
        docs = [
            Document(id="d1", title="Runbook", text="readSensor retries twice", uri="https://x/1"),
            Document(id="d2", title="Offsite", text="lunch is at noon", uri="https://x/2"),
        ]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])

        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 2

        part = enrich_partition(REPO)
        shard = read_shard(store_dir, part)
        assert any(e.src == "n2" and e.dst == f"{part}:d1" and e.relation == "documented_by"
                   for e in shard.edges)
        # a result mentioning nothing gets no edges at all, not even the repo-level one
        assert not any(e.dst == f"{part}:d2" for e in shard.edges)
        assert [e.dst for e in store.neighbors("n2", direction="out")] == [f"{part}:d1"]
        # ...but a result that DOES mention something keeps its repo-level edge:
        # an enrichment hit is genuinely third-party knowledge about the repo, so
        # it belongs in the "external knowledge" surfaces (`get_repo_links` /
        # the dashboard's `_links_for`). Deliberately unlike the wiki, whose
        # pages are contextlake's own output and are symbol-linked only.
        assert [e.dst for e in store.neighbors(make_id("repo", REPO), direction="out")
                if e.relation == "documented_by"] == [f"{part}:d1"]

        # a re-run replaces the partition's edges rather than accumulating them
        assert run_enrich_repo(store, store_dir, cfg, REPO).documents == 2
        assert [e.dst for e in store.neighbors("n2", direction="out")] == [f"{part}:d1"]
    finally:
        store.close()


def test_enrich_repo_no_terms_returns_zero_without_touching_store(tmp_path):
    store_dir = tmp_path / "kbstore"
    store = _store(store_dir)
    try:
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])
        assert run_enrich_repo(store, store_dir, cfg, "group/missing") == (0, 0, 0)
        assert read_shard(store_dir, enrich_partition("group/missing")) is None
    finally:
        store.close()


def test_run_enrich_repo_returns_the_edge_count_beside_the_document_count(
        tmp_path, monkeypatch):
    """The edge count was computed, stored, and then discarded by `return len(nodes)`.

    Every caller could therefore report only documents stored, and a document with no
    edge to any symbol is invisible to a question about the code while reading as a
    success. Both numbers now come back, and they answer different questions.
    """
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        # the matcher reads symbols out of the index, not the shard
        store.upsert_nodes(REPO, [
            Node(id="n2", repo=REPO, kind="function", name="readSensor", file="app/readings.py"),
        ])
        docs = [
            Document(id="d1", title="Runbook", text="readSensor retries twice", uri="https://x/1"),
            Document(id="d2", title="Offsite", text="lunch is at noon", uri="https://x/2"),
        ]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])

        counts = run_enrich_repo(store, store_dir, cfg, REPO)

        # d1 names readSensor: one symbol edge plus the repo-level fallback edge.
        # d2 names nothing, so it contributes neither. The two documents give the
        # same document count under both the old and the new code; only the edge
        # count can tell a run that reached the code from one that did not.
        assert counts.documents == 2
        assert counts.edges == 2
        assert counts.terms == 3  # repo name plus its two embeddable symbols
        # The number returned is the number stored, not a separate guess.
        assert len(read_shard(store_dir, enrich_partition(REPO)).edges) == counts.edges
    finally:
        store.close()


def test_run_enrich_repo_reports_zero_edges_for_documents_that_name_no_symbol(
        tmp_path, monkeypatch):
    """Documents came back and attached to nothing. That is a correct outcome, and it
    has to be visible: the document count alone is identical to the attached case."""
    store_dir = tmp_path / "kbstore"
    _seed_shard(store_dir)
    store = _store(store_dir)
    try:
        store.upsert_nodes(REPO, [
            Node(id="n2", repo=REPO, kind="function", name="readSensor", file="app/readings.py"),
        ])
        docs = [
            Document(id="d1", title="Q3 plan", text="the team owns this service",
                     uri="https://x/1"),
            Document(id="d2", title="Offsite", text="lunch is at noon", uri="https://x/2"),
        ]
        monkeypatch.setattr(enrich, "search_source", lambda src, terms, timeout=None: docs)
        cfg = KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])

        counts = run_enrich_repo(store, store_dir, cfg, REPO)

        assert counts.documents == 2
        assert counts.edges == 0
    finally:
        store.close()


# --- build_terms: the cap must be spent on symbols, not on files -------------

def _seed_cap_starved_shard(store_dir, *, embeddable_pairs: int = 6):
    """A shard shaped like a real repo whose graph is dominated by non-symbols.

    18 high-degree `file`/`config_key` nodes sit in front of ``embeddable_pairs``
    * 2 low-degree `function`/`class` nodes. 30 nodes at the default, so
    `_grounding_cap` is 15 and `top_symbols` is filled by the non-embeddable
    kinds plus one floor slot per missing embeddable kind. Returns the distinct
    embeddable names seeded.
    """
    nodes, edges = [], []
    for i in range(9):
        nodes.append(Node(id=f"f{i}", repo=REPO, kind="file", name=f"src/mod{i}.py",
                          file=f"src/mod{i}.py"))
        nodes.append(Node(id=f"c{i}", repo=REPO, kind="config_key", name=f"setting.{i}",
                          file="app.ini"))
    names = []
    for i in range(embeddable_pairs):
        nodes.append(Node(id=f"fn{i}", repo=REPO, kind="function", name=f"handle_request_{i}",
                          file=f"src/mod{i}.py"))
        nodes.append(Node(id=f"cl{i}", repo=REPO, kind="class", name=f"WidgetService{i}",
                          file=f"src/mod{i}.py"))
        names += [f"handle_request_{i}", f"WidgetService{i}"]
    # Every non-embeddable node outranks every embeddable one by degree.
    for i in range(9):
        for j in range(9):
            if i != j:
                edges.append(Edge(src=f"f{i}", dst=f"c{j}", relation="contains",
                                  confidence=Confidence.EXTRACTED, provenance=_prov()))
    for i in range(embeddable_pairs):
        edges.append(Edge(src=f"fn{i}", dst=f"cl{i}", relation="calls",
                          confidence=Confidence.EXTRACTED, provenance=_prov()))
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="cafe1",
                                      nodes=nodes, edges=edges))
    return names


def test_build_terms_is_not_starved_by_high_degree_non_embeddable_nodes(tmp_path):
    """The term cap must be spent AFTER the kind filter, not before it.

    `build_terms` used to read `top_symbols`, which ranks every node and then
    caps, so files/packages/modules/config keys consumed the cap before one
    searchable symbol was considered. This fixture holds 12 embeddable symbols
    behind 18 higher-degree non-embeddable nodes, which is the shape measured
    on the real store.
    """
    from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
    from contextlake.kb.wiki.generate import repo_brief

    seeded = _seed_cap_starved_shard(tmp_path)

    # The fixture contains the case: `top_symbols` really is starved. 2 of its
    # 15 rows are embeddable (one per-kind floor slot each for function and
    # class), so the old path could only ever have produced 1 + 2 = 3 terms.
    brief = repo_brief(tmp_path, REPO)
    assert len(brief["top_symbols"]) == 15
    assert sum(1 for t in brief["top_symbols"] if t["kind"] in EMBEDDABLE_KINDS) == 2

    terms = build_terms(tmp_path, REPO)
    assert len(terms) == 10          # was 3 while the cap ran before the filter
    assert terms[0] == "app"         # the repo name still leads
    # The filter itself still holds: no file or config-key name became a term,
    # even though those nodes outrank every symbol here.
    assert set(terms[1:]) <= set(seeded)
    assert not any(t.startswith("src/") or t.startswith("setting.") for t in terms)


def test_build_terms_is_bounded_by_the_briefs_own_symbol_cap(tmp_path):
    """Terms are bounded by `max_terms` AND by how many symbols the brief carries.

    A caller raising `max_terms` past `wiki.generate._TERM_SYMBOL_CAP` gets the
    cap, not a silent truncation nobody can see.
    """
    from contextlake.kb.wiki.generate import _TERM_SYMBOL_CAP

    _seed_cap_starved_shard(tmp_path, embeddable_pairs=40)  # 80 distinct names
    terms = build_terms(tmp_path, REPO, max_terms=100)
    assert len(terms) == _TERM_SYMBOL_CAP + 1  # the repo name plus the capped symbols
    assert len(set(terms)) == len(terms)       # and every one of them distinct
