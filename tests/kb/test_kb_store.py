"""Tests for the SQLite cross-repo index store."""

from datetime import date

import pytest

from contextlake.kb.model import SHARED_REPO, Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.store.sqlite_store import SqliteStore


def _node(nid, repo="team/api", kind="function", name=None, **kw):
    return Node(id=nid, repo=repo, kind=kind, name=name or nid, **kw)


def _edge(src, dst, relation="calls", conf=Confidence.EXTRACTED):
    return Edge(
        src=src, dst=dst, relation=relation, confidence=conf,
        provenance=Provenance(source_file="src/a.py", source_line=1, verified_at=date(2026, 6, 21)),
    )


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def test_schema_version_recorded(store):
    from contextlake.kb.store.sqlite_store import SCHEMA_VERSION
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_repo_round_trip(store):
    store.upsert_repo(Repo(id="team/api", path="/w/team/api", head_commit="abc"))
    r = store.get_repo("team/api")
    assert r.head_commit == "abc"
    assert [x.id for x in store.list_repos()] == ["team/api"]


def test_node_upsert_get_and_update(store):
    store.upsert_nodes("team/api", [_node("n1", name="handle", file="a.py")])
    n = store.get_node("n1")
    assert n.name == "handle" and n.file == "a.py"
    # upsert again updates in place
    store.upsert_nodes("team/api", [_node("n1", name="handle2")])
    assert store.get_node("n1").name == "handle2"
    assert store.get_node("missing") is None


def test_reupsert_refreshes_fts_without_duplicates(store):
    # The batched FTS refresh (one set-based delete + executemany, replacing the
    # old O(N^2) per-row delete) must still drop the stale row and never duplicate.
    store.upsert_nodes("team/api", [_node("n1", name="OldName")])
    store.upsert_nodes("team/api", [_node("n1", name="NewName")])
    assert {n.name for n in store.search("oldname")} == set()  # stale entry gone
    assert {n.name for n in store.search("newname")} == {"NewName"}
    fts = store.conn.execute("SELECT count(*) FROM node_fts WHERE node_id='n1'").fetchone()[0]
    assert fts == 1  # exactly one FTS row, not two


def test_upsert_nodes_batches_across_chunk_boundary(store):
    # More nodes than the 900-id delete chunk: every node must be searchable and
    # the FTS row count must match the node count (no rows lost at a chunk seam).
    nodes = [_node(f"n{i}", name=f"Sym{i}") for i in range(2000)]
    store.upsert_nodes("team/api", nodes)
    assert store.stats().nodes == 2000
    assert store.conn.execute("SELECT count(*) FROM node_fts").fetchone()[0] == 2000
    assert {n.id for n in store.nodes_by_name("Sym1999")} == {"n1999"}


def test_search_finds_by_prefix(store):
    store.upsert_nodes("team/api", [
        _node("n1", name="CatalogService"),
        _node("n2", name="CatalogRepository"),
        _node("n3", name="PaymentGateway"),
    ])
    names = {n.name for n in store.search("catalog")}
    assert names == {"CatalogService", "CatalogRepository"}
    # kind + repo filters
    assert store.search("payment", kind="class") == []
    assert {n.name for n in store.search("payment", repo="team/api")} == {"PaymentGateway"}


def test_nodes_by_name_is_exact(store):
    store.upsert_nodes("team/api", [_node("n1", name="Foo"), _node("n2", name="Foobar")])
    assert {n.id for n in store.nodes_by_name("Foo")} == {"n1"}  # exact, not prefix
    assert store.nodes_by_name("Foo", kind="class") == []  # kind filter
    assert {n.id for n in store.nodes_by_name("Foo", repo="team/api")} == {"n1"}


def test_search_handles_fts_operator_words(store):
    # FTS5 keywords (AND/OR/NOT/NEAR) are common identifiers; they must not crash.
    store.upsert_nodes("team/api", [_node("n1", name="and_then"), _node("n2", name="payload")])
    assert {n.name for n in store.search("and")} == {"and_then"}
    assert store.search("or") == []
    assert store.search("not") == []
    assert store.search("near") == []


def test_neighbors_direction_and_relation(store):
    store.upsert_nodes("team/api", [_node("a"), _node("b"), _node("c")])
    store.upsert_edges("team/api", [_edge("a", "b", "calls"), _edge("a", "c", "imports")])
    out = store.neighbors("a", direction="out")
    assert {e.dst for e in out} == {"b", "c"}
    assert {e.dst for e in store.neighbors("a", relation="calls")} == {"b"}
    assert {e.src for e in store.neighbors("b", direction="in")} == {"a"}
    # provenance + confidence survive the round trip
    assert out[0].provenance.verified_at == date(2026, 6, 21)
    assert out[0].confidence is Confidence.EXTRACTED


def test_edge_attrs_round_trip(store):
    store.upsert_nodes("team/api", [_node("a"), _node("b")])
    e = Edge(src="a", dst="b", relation="calls_http", confidence=Confidence.INFERRED,
             attrs={"raw_host": "api.example.com"},
             provenance=Provenance(source_file="src/a.py", source_line=1,
                                   verified_at=date(2026, 6, 21)))
    store.upsert_edges("team/api", [e])
    out = store.neighbors("a", direction="out")
    assert out[0].attrs == {"raw_host": "api.example.com"}


def test_edge_attrs_defaults_to_empty_dict_when_absent(store):
    store.upsert_nodes("team/api", [_node("a"), _node("b")])
    store.upsert_edges("team/api", [_edge("a", "b")])
    out = store.neighbors("a", direction="out")
    assert out[0].attrs == {}


def test_additive_column_migration_on_a_pre_v2_52_store(tmp_path):
    """A store created before edges.attrs existed must not crash on open or on
    the first write -- CREATE TABLE IF NOT EXISTS is a no-op against a table
    that already exists, so opening an old store needs an explicit ALTER TABLE,
    not just a bumped SCHEMA_VERSION constant."""
    path = tmp_path / "old.sqlite"
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE edges (edge_id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT, "
        "src TEXT, dst TEXT, relation TEXT, confidence TEXT, context TEXT, "
        "source_file TEXT, source_line INTEGER, verified_at TEXT, weight REAL, "
        "cross_repo INTEGER DEFAULT 0);"
        "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, repo_id TEXT, kind TEXT, "
        "name TEXT, qualified_name TEXT, file TEXT, line_start INTEGER, "
        "line_end INTEGER, lang TEXT, attrs TEXT);"
        "CREATE TABLE kb_meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE repos (repo_id TEXT PRIMARY KEY, path TEXT, host TEXT, "
        "default_branch TEXT, head_commit TEXT, indexed_at TEXT, lang_stats TEXT);"
    )
    conn.commit()
    conn.close()

    s = SqliteStore(path)  # must not raise
    try:
        s.upsert_nodes("team/api", [_node("a"), _node("b")])
        s.upsert_edges("team/api", [_edge("a", "b")])  # must not raise
        assert s.neighbors("a", direction="out")[0].attrs == {}
    finally:
        s.close()


def test_cross_repo_flag_and_stats(store):
    store.upsert_nodes("team/api", [_node("a", repo="team/api")])
    store.upsert_nodes("team/web", [_node("b", repo="team/web")])
    store.upsert_edges("team/api", [_edge("a", "b", "depends_on")])
    st = store.stats()
    assert st.nodes == 2 and st.edges == 1 and st.repos == 0
    assert st.by_confidence == {"EXTRACTED": 1}
    row = store.conn.execute("SELECT cross_repo FROM edges").fetchone()
    assert row["cross_repo"] == 1  # a (team/api) -> b (team/web)


def test_clear_repo(store):
    store.upsert_nodes("team/api", [_node("a")])
    store.upsert_edges("team/api", [_edge("a", "a")])
    store.clear_repo("team/api")
    assert store.get_node("a") is None
    assert store.stats().nodes == 0 and store.stats().edges == 0
    assert store.search("a") == []  # fts cleared too


def test_shared_node_keeps_sentinel_repo_regardless_of_index_order(store):
    """A module/endpoint/topic node's id doesn't encode a repo (Finding #10): two
    repos that both import "requests" produce the exact same node id. Before the
    SHARED_REPO fix, whichever repo upserted it last silently "won" the repo_id
    column. It must now stay pinned to the sentinel no matter which repo goes
    second."""
    shared = _node("module_requests", repo=SHARED_REPO, kind="module", name="requests")
    store.upsert_nodes("team/api", [shared])
    assert store.get_node("module_requests").repo == SHARED_REPO
    store.upsert_nodes("team/web", [shared])
    assert store.get_node("module_requests").repo == SHARED_REPO


def test_clear_repo_does_not_delete_a_shared_node_another_repo_still_uses(store):
    """The bug Finding #10's fix also closes: clear_repo on the repo that happened
    to last-write a shared node used to delete it out from under every other repo
    still holding an edge to it (a self-inflicted dangling edge). A sentinel repo_id
    that never equals a real repo_id means clear_repo's `WHERE repo_id=?` can never
    match the shared node at all."""
    shared = _node("module_requests", repo=SHARED_REPO, kind="module", name="requests")
    store.upsert_nodes("team/api", [_node("api_file", kind="file"), shared])
    store.upsert_edges("team/api", [_edge("api_file", "module_requests", relation="imports")])
    store.upsert_nodes("team/web", [_node("web_file", kind="file"), shared])
    store.upsert_edges("team/web", [_edge("web_file", "module_requests", relation="imports")])

    store.clear_repo("team/api")

    assert store.get_node("module_requests") is not None  # still there for team/web
    assert store.get_node("api_file") is None              # team/api's own node is gone
    remaining = store.neighbors("module_requests", direction="in")
    assert {e.src for e in remaining} == {"web_file"}       # team/api's edge is gone too


def test_clear_repo_does_not_delete_a_package_node_another_repo_depends_on(store):
    """Same bug, the ("(packages)") sentinel that predates this fix: upsert_nodes
    used to stamp EVERY node in a shard's batch with that shard's own repo_id,
    ignoring each Node's own .repo entirely -- so a "(packages)" package node
    was just as vulnerable to clear_repo deleting it out from under another
    repo's depends_on edge as the new module/endpoint/topic sentinel is."""
    pkg = _node("pkg_requests", repo="(packages)", kind="package", name="requests")
    store.upsert_nodes("team/api", [pkg])
    store.upsert_edges("team/api", [_edge("team_api_mod", "pkg_requests", relation="depends_on")])
    store.upsert_nodes("team/web", [pkg])
    store.upsert_edges("team/web", [_edge("team_web_mod", "pkg_requests", relation="depends_on")])

    store.clear_repo("team/api")

    assert store.get_node("pkg_requests") is not None
    assert store.get_node("pkg_requests").repo == "(packages)"


def test_reopen_existing_db(tmp_path):
    p = tmp_path / "kb.sqlite"
    s1 = SqliteStore(p)
    s1.upsert_nodes("r", [_node("x", repo="r")])
    s1.close()
    s2 = SqliteStore(p)
    assert s2.get_node("x") is not None
    s2.close()
