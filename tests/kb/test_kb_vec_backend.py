"""Tests for the sqlite-vec ANN backend and the vector-store factory.

The live sqlite-vec tests are skipped when the optional dependency is absent, so
the kb CI job (which doesn't install it) stays green; the factory/fallback tests
always run.
"""

import pytest

from gitlab_sync.kb.embeddings import store as store_mod
from gitlab_sync.kb.embeddings.store import SqliteVecStore, VectorStore, build_vector_store

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
