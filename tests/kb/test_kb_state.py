"""Tests for index state + staleness helpers."""

import pytest

from contextlake.kb.model import Repo
from contextlake.kb.state import check_schema, mark_repo_indexed, needs_reindex
from contextlake.kb.store.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "kb.sqlite")
    yield s
    s.close()


def test_needs_reindex_when_absent(store):
    assert needs_reindex(store, "team/api", "abc") is True


def test_mark_indexed_then_unchanged(store):
    store.upsert_repo(Repo(id="team/api", path="/w/team/api"))
    mark_repo_indexed(store, "team/api", "abc123")
    assert needs_reindex(store, "team/api", "abc123") is False
    assert needs_reindex(store, "team/api", "def456") is True  # HEAD moved
    # indexed_at recorded
    assert store.conn.execute(
        "SELECT indexed_at FROM repos WHERE repo_id='team/api'"
    ).fetchone()["indexed_at"]


def test_check_schema_accepts_current(store):
    check_schema(store)  # no raise on the version we wrote


def test_check_schema_rejects_newer(store):
    store._set_meta("schema_version", "999")
    store.conn.commit()
    with pytest.raises(RuntimeError, match="newer than supported"):
        check_schema(store)
