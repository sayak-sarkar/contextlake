"""Tests for index state + staleness helpers."""

import sqlite3

import pytest

from contextlake.kb.model import Repo
from contextlake.kb.state import (
    check_schema,
    indexed_parser_version,
    mark_repo_indexed,
    needs_reindex,
)
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SCHEMA_VERSION, SqliteStore


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


def test_indexed_parser_version_prefers_the_stamp(store, tmp_path):
    store.upsert_repo(Repo(id="team/api", path="/w/team/api"))
    mark_repo_indexed(store, "team/api", "abc123", "2")
    # No shard on disk at all: the stamp alone answers it, which is the whole
    # point (a fleet-wide pass must not have to parse anything to ask).
    assert indexed_parser_version(store, tmp_path, "team/api") == "2"


def test_indexed_parser_version_falls_back_to_the_shard_when_unstamped(store, tmp_path):
    """A store written before the column existed carries no stamp, so the shard,
    which is the source of truth, answers instead."""
    store.upsert_repo(Repo(id="team/api", path="/w/team/api"))
    mark_repo_indexed(store, "team/api", "abc123")  # no parser_version -> NULL
    write_shard(tmp_path, GraphShard(repo="team/api", parser_version="1"))
    assert indexed_parser_version(store, tmp_path, "team/api") == "1"


def test_indexed_parser_version_reads_only_the_head_of_the_shard(store, tmp_path, monkeypatch):
    """The unstamped path must not parse whole shards: on a large fleet that would
    make the cheap pre-filter the most expensive part of an incremental index."""
    from contextlake.kb.store import shards as shards_mod

    store.upsert_repo(Repo(id="team/api", path="/w/team/api"))
    mark_repo_indexed(store, "team/api", "abc123")
    write_shard(tmp_path, GraphShard(repo="team/api", parser_version="1"))

    def boom(*a, **kw):
        raise AssertionError("read_shard must not be called on the cheap path")

    monkeypatch.setattr(shards_mod, "read_shard", boom)
    assert indexed_parser_version(store, tmp_path, "team/api") == "1"


def test_peek_parser_version_declines_rather_than_guessing(tmp_path):
    """A shard the cheap read cannot answer for returns None, so the caller falls
    back to a full parse instead of inventing a version."""
    from contextlake.kb.store.shards import peek_parser_version, shard_path

    p = shard_path(tmp_path, "team/api")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"repo": "team/api", "parser_version": null, "nodes": []}')
    assert peek_parser_version(tmp_path, "team/api") is None
    assert peek_parser_version(tmp_path, "team/nope") is None  # no shard at all


def test_indexed_parser_version_is_unknown_with_no_stamp_and_no_shard(store, tmp_path):
    """Unknown provenance must read as stale, never as current."""
    store.upsert_repo(Repo(id="team/api", path="/w/team/api"))
    mark_repo_indexed(store, "team/api", "abc123")
    assert indexed_parser_version(store, tmp_path, "team/api") is None


def test_parser_version_column_is_added_to_a_pre_v3_store(tmp_path):
    """The migration widens an existing store without destroying its contents."""
    db = tmp_path / "kb.sqlite"
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE repos (repo_id TEXT PRIMARY KEY, path TEXT, host TEXT, "
        "default_branch TEXT, head_commit TEXT, indexed_at TEXT, lang_stats TEXT)"
    )
    legacy.execute("INSERT INTO repos(repo_id, path, head_commit) VALUES('team/api','/w','h1')")
    legacy.commit()
    legacy.close()

    s = SqliteStore(db)
    try:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(repos)")}
        assert "parser_version" in cols
        repo = s.get_repo("team/api")  # the pre-existing row survived intact
        assert repo is not None and repo.path == "/w" and repo.head_commit == "h1"
        assert s.get_repo_parser_version("team/api") is None
    finally:
        s.close()


def test_check_schema_accepts_current(store):
    check_schema(store)  # no raise on the version we wrote


def test_check_schema_rejects_newer(store):
    store._set_meta("schema_version", "999")
    store.conn.commit()
    with pytest.raises(RuntimeError, match="schema v999"):
        check_schema(store)


def test_reopening_preserves_a_future_schema_stamp(tmp_path):
    """The downgrade this closes: ``SqliteStore`` stamped its own schema version
    on every open, *before* any caller could read what was there. A store written
    by a newer contextlake was therefore re-stamped to this build's number on the
    first open, and ``check_schema`` -- which runs after construction -- then read
    back this build's own number and saw nothing wrong. Reopen is the case that
    matters: the in-process test above never crosses a constructor.
    """
    db = tmp_path / "kb.sqlite"
    first = SqliteStore(db)
    first._set_meta("schema_version", "999")   # stand-in for "written by a later build"
    first.close()

    reopened = SqliteStore(db)
    try:
        assert reopened.get_meta("schema_version") == "999"   # not silently re-stamped
        with pytest.raises(RuntimeError) as excinfo:
            check_schema(reopened)
    finally:
        reopened.close()
    msg = str(excinfo.value)
    assert "999" in msg and str(SCHEMA_VERSION) in msg   # both versions named
    assert "pip install -U contextlake" in msg           # ...and what to do


def test_reopening_an_older_store_still_migrates_it_forward(tmp_path):
    """The other direction is not a refusal: an older stamp is overwritten,
    because the store has just been migrated forward on open."""
    db = tmp_path / "kb.sqlite"
    first = SqliteStore(db)
    first._set_meta("schema_version", "1")
    first.close()

    reopened = SqliteStore(db)
    try:
        assert reopened.get_meta("schema_version") == str(SCHEMA_VERSION)
        check_schema(reopened)  # no raise
    finally:
        reopened.close()


def test_check_schema_refuses_an_unreadable_stamp(tmp_path):
    """A stamp that is not a version number is preserved and reported, not
    normalised away -- and not surfaced as a bare ``int()`` ValueError."""
    db = tmp_path / "kb.sqlite"
    first = SqliteStore(db)
    first._set_meta("schema_version", "not-a-version")
    first.close()

    reopened = SqliteStore(db)
    try:
        assert reopened.get_meta("schema_version") == "not-a-version"
        with pytest.raises(RuntimeError, match="unrecognised schema stamp"):
            check_schema(reopened)
    finally:
        reopened.close()
