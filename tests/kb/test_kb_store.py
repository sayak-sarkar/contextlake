"""Tests for the SQLite cross-repo index store."""

from datetime import date

import pytest

from gitlab_sync.kb.model import Confidence, Edge, Node, Provenance, Repo
from gitlab_sync.kb.store.sqlite_store import SqliteStore


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
    assert store.get_meta("schema_version") == "1"


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


def test_search_finds_by_prefix(store):
    store.upsert_nodes("team/api", [
        _node("n1", name="OrderService"),
        _node("n2", name="OrderRepository"),
        _node("n3", name="PaymentGateway"),
    ])
    names = {n.name for n in store.search("order")}
    assert names == {"OrderService", "OrderRepository"}
    # kind + repo filters
    assert store.search("payment", kind="class") == []
    assert {n.name for n in store.search("payment", repo="team/api")} == {"PaymentGateway"}


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


def test_reopen_existing_db(tmp_path):
    p = tmp_path / "kb.sqlite"
    s1 = SqliteStore(p)
    s1.upsert_nodes("r", [_node("x", repo="r")])
    s1.close()
    s2 = SqliteStore(p)
    assert s2.get_node("x") is not None
    s2.close()
