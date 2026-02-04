"""Tests for the local vector store (cosine search, persistence, isolation)."""

from gitlab_sync.kb.embeddings.store import VectorStore


def _store(tmp_path):
    return VectorStore(tmp_path / "embeddings.sqlite")


def test_upsert_and_count(tmp_path):
    s = _store(tmp_path)
    try:
        n = s.upsert([
            ("a", "repo1", [1.0, 0.0, 0.0]),
            ("b", "repo1", [0.0, 1.0, 0.0]),
        ])
        assert n == 2 and s.count() == 2
    finally:
        s.close()


def test_search_ranks_by_cosine(tmp_path):
    s = _store(tmp_path)
    try:
        s.upsert([
            ("x_axis", "r", [1.0, 0.0, 0.0]),
            ("y_axis", "r", [0.0, 1.0, 0.0]),
            ("xy_diag", "r", [0.9, 0.9, 0.0]),
        ])
        hits = s.search([1.0, 0.1, 0.0], k=2)
        assert [h[0] for h in hits] == ["x_axis", "xy_diag"]  # nearest first
        assert hits[0][1] > hits[1][1]
        # cosine is scale-invariant: a longer query in the same direction ranks the same
        assert s.search([10.0, 0.0, 0.0], k=1)[0][0] == "x_axis"
    finally:
        s.close()


def test_search_repo_filter_and_dim_skip(tmp_path):
    s = _store(tmp_path)
    try:
        s.upsert([
            ("a", "r1", [1.0, 0.0]),
            ("b", "r2", [1.0, 0.0]),
            ("wrongdim", "r1", [1.0, 0.0, 0.0]),  # different dim -> skipped
        ])
        hits = s.search([1.0, 0.0], k=10, repo="r1")
        assert [h[0] for h in hits] == ["a"]  # r2 excluded, wrongdim skipped
    finally:
        s.close()


def test_clear_repo(tmp_path):
    s = _store(tmp_path)
    try:
        s.upsert([("a", "r1", [1.0]), ("b", "r2", [1.0])])
        s.clear_repo("r1")
        assert s.count() == 1
        assert [h[0] for h in s.search([1.0], k=5)] == ["b"]
    finally:
        s.close()


def test_upsert_replaces_and_persists(tmp_path):
    path = tmp_path / "embeddings.sqlite"
    s = VectorStore(path)
    s.upsert([("a", "r", [1.0, 0.0])])
    s.upsert([("a", "r", [0.0, 1.0])])  # replace same id
    s.close()

    s2 = VectorStore(path)  # reopen -> data persisted
    try:
        assert s2.count() == 1
        assert s2.search([0.0, 1.0], k=1)[0][0] == "a"
    finally:
        s2.close()
