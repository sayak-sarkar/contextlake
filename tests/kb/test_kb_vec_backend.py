"""Tests for the sqlite-vec ANN backend and the vector-store factory.

The live sqlite-vec tests are skipped when the optional dependency is absent, so
the kb CI job (which doesn't install it) stays green; the factory/fallback tests
always run.
"""

import pytest

from contextlake.kb.embeddings import store as store_mod
from contextlake.kb.embeddings.store import (
    SqliteVecStore,
    VectorStore,
    build_vector_store,
    chunk_key,
)

try:
    import sqlite_vec  # noqa: F401

    HAS_VEC = True
except ImportError:
    HAS_VEC = False

requires_vec = pytest.mark.skipif(not HAS_VEC, reason="sqlite-vec not installed")


class _Boom:
    def __init__(self, *a, **k):
        raise ImportError("sqlite_vec unavailable")


# --- factory (no native dep needed) ---------------------------------------

def test_factory_brute_forced(tmp_path):
    vs = build_vector_store(tmp_path / "e.sqlite", backend="brute")
    try:
        assert isinstance(vs, VectorStore) and vs.name == "brute"
    finally:
        vs.close()


def test_factory_auto_falls_back_when_vec_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "SqliteVecStore", _Boom)
    vs = build_vector_store(tmp_path / "e.sqlite", backend="auto")
    try:
        assert vs.name == "brute"
    finally:
        vs.close()


def test_factory_sqlite_vec_forced_raises_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "SqliteVecStore", _Boom)
    with pytest.raises(ImportError):
        build_vector_store(tmp_path / "e.sqlite", backend="sqlite-vec")


# --- live sqlite-vec backend ----------------------------------------------

@requires_vec
def test_factory_auto_picks_vec_when_available(tmp_path):
    vs = build_vector_store(tmp_path / "e.sqlite", backend="auto")
    try:
        assert vs.name == "sqlite-vec"
    finally:
        vs.close()


@requires_vec
def test_sqlite_vec_search_filter_replace_clear(tmp_path):
    s = SqliteVecStore(tmp_path / "v.sqlite")
    try:
        s.upsert([("a", "r1", [1.0, 0.0, 0.0]), ("b", "r1", [0.0, 1.0, 0.0]),
                  ("c", "r2", [0.9, 0.1, 0.0])])
        assert s.count() == 3

        hits = s.search([1.0, 0.05, 0.0], k=2)
        assert hits[0][0] == "a" and hits[0][1] > hits[1][1]  # similarity, high first
        assert {h[0] for h in s.search([1.0, 0.0, 0.0], k=5, repo="r1")} <= {"a", "b"}
        assert s.search([1.0, 0.0], k=3) == []  # dim mismatch -> empty

        s.upsert([("a", "r1", [0.0, 0.0, 1.0])])  # replace, not duplicate
        assert s.count() == 3
        assert s.search([0.0, 0.0, 1.0], k=1)[0][0] == "a"

        s.clear_repo("r1")
        assert s.count() == 1
    finally:
        s.close()


@requires_vec
def test_sqlite_vec_search_repo_filter_includes_connect_and_enrich_partitions(tmp_path):
    """Same repo-scope widening as the brute store: repo="r1" must also match
    "@connect:r1"/"@enrich:r1" rows, not just the literal "r1" shard.

    Deliberately makes the r1-family vectors *worse* cosine matches than a pile of
    decoy rows under an unrelated repo, so this only passes if vec0 pushes the
    ``repo_id IN (...)`` filter into the KNN scan itself (before LIMIT k) --
    if it were applied after LIMIT k, the global top-k would be all decoys and
    the r1-family rows would never surface."""
    s = SqliteVecStore(tmp_path / "v.sqlite")
    try:
        items = [
            ("code_node", "r1", [0.9, 0.1, 0.0]),
            ("connect_node", "@connect:r1", [0.85, 0.1, 0.0]),
            ("enrich_node", "@enrich:r1", [0.8, 0.1, 0.0]),
        ]
        # 200 decoys, closer to the query than every r1-family row above, under an
        # unrelated repo -- would win a post-limit filter outright.
        items += [(f"decoy_{i}", "other/repo", [0.99, 0.01, 0.0]) for i in range(200)]
        s.upsert(items)

        hits = {h[0] for h in s.search([1.0, 0.0, 0.0], k=3, repo="r1")}
        assert hits == {"code_node", "connect_node", "enrich_node"}
    finally:
        s.close()


@requires_vec
def test_sqlite_vec_chunk_size_clamped_and_usable(tmp_path):
    # vec0 needs a multiple of 8; a non-conforming value is clamped, not rejected.
    s = SqliteVecStore(tmp_path / "v.sqlite", chunk_size=20)
    try:
        assert s._chunk_size == 16            # 20 -> nearest lower multiple of 8
        s.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0])])
        assert s.search([1.0, 0.0], k=1)[0][0] == "a"  # custom chunk size still works
    finally:
        s.close()


@requires_vec
def test_sqlite_vec_chunk_size_floors_to_eight(tmp_path):
    s = SqliteVecStore(tmp_path / "v.sqlite", chunk_size=1)
    try:
        assert s._chunk_size == 8             # tiny values floor to the vec0 minimum
    finally:
        s.close()


@requires_vec
def test_factory_threads_chunk_size(tmp_path):
    vs = build_vector_store(tmp_path / "e.sqlite", backend="sqlite-vec", chunk_size=2048)
    try:
        assert vs._chunk_size == 2048
    finally:
        vs.close()


@requires_vec
def test_sqlite_vec_persists(tmp_path):
    p = tmp_path / "v.sqlite"
    s = SqliteVecStore(p)
    s.upsert([("a", "r", [1.0, 0.0])])
    s.close()
    s2 = SqliteVecStore(p)
    try:
        assert s2.count() == 1 and s2.search([1.0, 0.0], k=1)[0][0] == "a"
    finally:
        s2.close()


@requires_vec
def test_vec_store_survives_dim_written_without_table(tmp_path):
    """guard_store_identity writes vec_meta['dim'] independently of table creation, so
    'dim' must not be treated as a 'vec_items exists' sentinel: embedding a zero-node
    workspace then reopening previously raised 'no such table: vec_items'."""
    from contextlake.kb.embeddings.store import guard_store_identity

    path = tmp_path / "embeddings.sqlite"
    vs = SqliteVecStore(path)
    guard_store_identity(vs, "test-embedder", 8)   # writes dim, creates no vec_items
    vs.close()

    reopened = SqliteVecStore(path)                 # reads dim -> must not assume the table
    assert reopened.count() == 0                    # previously: sqlite3.OperationalError
    assert reopened.search([0.0] * 8) == []
    reopened.clear_repo("team/api")                 # previously raised
    # and it can still be populated normally afterwards
    assert reopened.upsert([("n1", "team/api", [0.1] * 8)]) == 1
    assert reopened.count() == 1
    reopened.close()


@requires_vec
def test_a_chunky_document_cannot_crowd_distinct_nodes_out_of_the_window(tmp_path):
    """The vec backend must over-fetch, or chunking silently shrinks every result set.

    This backend asks the KNN index for a fixed number of ROWS, then collapses chunk rows
    down to their nodes. Rows and nodes stopped being the same thing when chunking landed:
    one talkative document now owns many rows, and if the window is only `k` wide that one
    document can fill it entirely. The caller asked for k documents and gets one.

    `_CHUNK_OVERFETCH` is the widening factor, and nothing else measures it -- the brute
    store scores every vector, so the chunking suite cannot see this. Here the query sits
    almost on top of twelve chunks of a single document, with two other documents ranked
    just behind them. Unwidened, the top three ROWS are all the same document and this
    returns one node.
    """
    s = SqliteVecStore(tmp_path / "v.sqlite")
    try:
        items = [(chunk_key("chatty", i), "r1", [1.0, 0.001 * i, 0.0]) for i in range(12)]
        items += [(chunk_key("quiet_b", 0), "r1", [0.8, 0.6, 0.0]),
                  (chunk_key("quiet_c", 0), "r1", [0.7, 0.7, 0.0])]
        s.upsert(items)

        hits = s.search([1.0, 0.0, 0.0], k=3)

        # Node ids, never chunk keys: collapsing is what keeps chunking invisible.
        assert [h[0] for h in hits] == ["chatty", "quiet_b", "quiet_c"]
        assert len({h[0] for h in hits}) == 3
    finally:
        s.close()


# --- one contract for both backends ----------------------------------------

@pytest.mark.parametrize("backend", ["brute", pytest.param("sqlite-vec", marks=requires_vec)])
def test_a_batch_that_repeats_an_id_keeps_the_last_vector(tmp_path, backend):
    # `kb connect` stages every source's vector rows for a repo into ONE upsert (since
    # 9.5.0, so a failed source can leave the old partition in place). Two sources that
    # embed the same node put its id in the batch twice. vec0 has no upsert, and the
    # second INSERT broke its primary key, failing `kb connect` for every repo with a
    # scraped link. The brute store's INSERT OR REPLACE always kept the last row.
    vs = build_vector_store(tmp_path / "e.sqlite", backend=backend)
    try:
        vs.upsert([("n1", "r", [1.0, 0.0]), ("n2", "r", [0.5, 0.5]),
                   ("n1", "r", [0.0, 1.0])])
        assert vs.count_repo("r") == 2
        best_id, best_score = vs.search([0.0, 1.0], k=1)[0]
        assert best_id == "n1" and best_score > 0.99     # the LAST vector for n1 won
    finally:
        vs.close()


@requires_vec
def test_connect_staging_two_sources_writes_one_partition(tmp_path):
    # The path that failed in `kb connect`: two sources' rows staged for one repo, holding
    # the same node id, flushed as one batch onto the ANN backend.
    from contextlake.kb.cmds.connect import _StagedVectors

    real = build_vector_store(tmp_path / "e.sqlite", backend="sqlite-vec")
    try:
        staged = _StagedVectors(real)
        staged.upsert([("link:a", "@connect:r", [1.0, 0.0])])           # source one
        staged.upsert([("link:a", "@connect:r", [0.0, 1.0]),            # source two
                       ("link:b", "@connect:r", [0.6, 0.8])])
        staged.flush("@connect:r")
        assert real.count_repo("@connect:r") == 2
    finally:
        real.close()


@pytest.mark.parametrize("backend", ["brute", pytest.param("sqlite-vec", marks=requires_vec)])
def test_delete_except_keeps_the_written_ids_and_other_repos(tmp_path, backend):
    vs = build_vector_store(tmp_path / "e.sqlite", backend=backend)
    try:
        vs.upsert([("a", "r", [1.0, 0.0]), ("b", "r", [0.0, 1.0]), ("c", "r", [0.6, 0.8]),
                   ("x", "q", [1.0, 0.0])])
        assert vs.delete_except("r", {"a", "c"}) == 1
        assert vs.count_repo("r") == 2 and vs.count_repo("q") == 1
        assert {i for i, _ in vs.search([0.0, 1.0], k=5, repo="r")} == {"a", "c"}
    finally:
        vs.close()
