"""A shard write and a shard reindex each change durable state, and neither may be seen half done.

Two defects, one rule:

* ``write_shard`` wrote the file in place. A reader that opened it mid-write saw a
  truncated JSON document, and a crash left that truncated file as the repo's source of
  truth. It now writes a temp sibling, fsyncs it, and renames it over the shard.
* ``reindex_shard`` ran ``clear_repo``, ``upsert_nodes`` and ``upsert_edges`` as three
  commits. A reader on another connection saw the repo with 0 nodes, then with all of its
  nodes and no edges, and a failure part way left that state behind. It now runs the three
  inside ``SqliteStore.transaction()``.

The mechanism tests (the spy on ``os.replace``, the failing rename) are deterministic. The
reader threads show the harm itself, which the mechanism tests only imply.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import date

import pytest

from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.shards import (
    GraphShard,
    read_shard,
    reindex_shard,
    shard_path,
    write_shard,
)
from contextlake.kb.store.sqlite_store import SqliteStore

_PROV = Provenance(source_file="a.py", source_line=1, verified_at=date(2026, 6, 21))
REPO = "team/api"


def _shard(n_nodes: int, *, head="h1") -> GraphShard:
    """``n_nodes`` function nodes chained by ``n_nodes - 1`` call edges."""
    nodes = [Node(id=f"n{i}", repo=REPO, kind="function", name=f"fn{i}")
             for i in range(n_nodes)]
    edges = [Edge(src=f"n{i}", dst=f"n{i + 1}", relation="calls",
                  confidence=Confidence.EXTRACTED, provenance=_PROV)
             for i in range(n_nodes - 1)]
    return GraphShard(repo=REPO, head_commit=head, nodes=nodes, edges=edges)


# --- write_shard: temp sibling, fsync, rename ------------------------------------

def test_write_shard_replaces_the_file_with_a_rename_of_a_complete_temp_file(
        tmp_path, monkeypatch):
    write_shard(tmp_path, _shard(2, head="old"))
    dest = shard_path(tmp_path, REPO)
    old_bytes = dest.read_bytes()

    seen = []
    real_replace = os.replace

    def spy(src, dst, *a, **k):
        # The moment before the swap: the shard is still the old complete file, and the
        # temp file next to it is the new complete one.
        seen.append({
            "dst_is_dest": os.fspath(dst) == os.fspath(dest),
            "same_dir": os.path.dirname(src) == os.path.dirname(dest),
            "tmp_name_hides_from_json_globs": not os.fspath(src).endswith(".json"),
            "dest_still_old": dest.read_bytes() == old_bytes,
            "tmp_is_complete_new": json.loads(open(src, "rb").read())["head_commit"] == "new",
        })
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy)
    write_shard(tmp_path, _shard(3, head="new"))

    assert len(seen) == 1, "the shard must be swapped in with one os.replace"
    assert all(seen[0].values()), seen[0]
    assert read_shard(tmp_path, REPO).head_commit == "new"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["api.json"], "no temp file left"


def test_write_shard_fsyncs_the_data_before_the_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda *a, **k: (events.append("replace"), real_replace(*a, **k))[1])

    write_shard(tmp_path, _shard(2))

    # Without the fsync, a power loss can make the rename durable ahead of the bytes.
    assert events[:2] == ["fsync", "replace"], events


def test_a_failed_rename_leaves_the_old_shard_and_no_temp_file(tmp_path, monkeypatch):
    write_shard(tmp_path, _shard(2, head="old"))
    dest = shard_path(tmp_path, REPO)
    old_bytes = dest.read_bytes()

    def refuse(*a, **k):
        raise OSError("rename refused")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        write_shard(tmp_path, _shard(5, head="new"))

    assert dest.read_bytes() == old_bytes
    assert sorted(p.name for p in dest.parent.iterdir()) == ["api.json"]


def test_a_write_that_dies_before_the_data_is_flushed_leaves_the_old_shard(
        tmp_path, monkeypatch):
    write_shard(tmp_path, _shard(2, head="old"))
    dest = shard_path(tmp_path, REPO)
    old_bytes = dest.read_bytes()

    def disk_full(fd):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="no space"):
        write_shard(tmp_path, _shard(5, head="new"))

    assert dest.read_bytes() == old_bytes
    assert sorted(p.name for p in dest.parent.iterdir()) == ["api.json"]


def test_a_refused_id_creates_nothing_on_disk(tmp_path):
    with pytest.raises(ValueError):
        write_shard(tmp_path, GraphShard(repo="team/../other"))
    assert list(tmp_path.iterdir()) == []


def test_a_new_shard_keeps_the_mode_write_text_gave_it(tmp_path):
    """The temp file is made by ``os.open``, which would give 0600 if asked for it. Shards
    were never owner-only, and a tool that reads them as another user must keep working."""
    old = os.umask(0o022)
    try:
        write_shard(tmp_path, _shard(2))
    finally:
        os.umask(old)
    assert stat.S_IMODE(shard_path(tmp_path, REPO).stat().st_mode) == 0o644


def test_a_reader_never_sees_a_truncated_shard_while_it_is_rewritten(tmp_path):
    """The harm: a plain read of the file during a rewrite. In place, ``write_text``
    truncates first, so a reader landing in that window got an empty or partial document."""
    write_shard(tmp_path, _shard(2))
    dest = shard_path(tmp_path, REPO)
    big = _shard(20000)
    stop = threading.Event()
    bad: list[str] = []
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                raw = dest.read_bytes()
            except FileNotFoundError:
                bad.append("file missing")
                continue
            reads[0] += 1
            try:
                json.loads(raw)
            except ValueError:
                bad.append(f"unparseable, {len(raw)} bytes")

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for _ in range(8):
            write_shard(tmp_path, big)
    finally:
        stop.set()
        t.join(timeout=30)

    assert reads[0] > 0, "the reader must have run or this proves nothing"
    assert bad == []


# --- reindex_shard: clear + nodes + edges in ONE transaction ----------------------

class _PausesAfterNodes(SqliteStore):
    """Blocks the writer between its node write and its edge write, so a reader can
    look at the moment the three-commit version exposed a half-built repo."""

    reached = threading.Event()
    release = threading.Event()

    def upsert_nodes(self, repo_id, nodes):
        super().upsert_nodes(repo_id, nodes)
        type(self).reached.set()
        assert type(self).release.wait(timeout=20), "test never released the writer"


def test_a_concurrent_reader_sees_the_old_repo_until_the_reindex_commits(tmp_path):
    db = tmp_path / "kb.sqlite"
    seed = SqliteStore(db)
    write_shard(tmp_path, _shard(2))
    assert reindex_shard(seed, tmp_path, REPO) is True
    assert seed.repo_counts(REPO) == (2, 1)
    seed.close()

    # The reader connects BEFORE the writer's transaction exists: opening a store runs
    # its schema script and version stamp, which write, and would block on the held lock.
    reader = SqliteStore(db)
    writer = _PausesAfterNodes(db)
    _PausesAfterNodes.reached.clear()
    _PausesAfterNodes.release.clear()
    write_shard(tmp_path, _shard(5))
    errors: list[BaseException] = []

    def run():
        try:
            reindex_shard(writer, tmp_path, REPO)
        except BaseException as e:  # noqa: BLE001 - surfaced by the assert below
            errors.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert _PausesAfterNodes.reached.wait(timeout=20), "writer never reached the pause"
        mid = reader.repo_counts(REPO)
    finally:
        _PausesAfterNodes.release.set()
        t.join(timeout=30)

    assert errors == []
    # LOAD-BEARING: the three-commit version showed (5, 0) here, the new nodes with no edges.
    assert mid == (2, 1)
    assert reader.repo_counts(REPO) == (5, 4), "after the commit the reader sees the new repo"
    reader.close()
    writer.close()


def test_a_failing_edge_write_rolls_the_whole_reindex_back(tmp_path):
    class _EdgesFail(SqliteStore):
        def upsert_edges(self, repo_id, edges):
            raise OSError("disk went away")

    db = tmp_path / "kb.sqlite"
    healthy = SqliteStore(db)
    write_shard(tmp_path, _shard(2))
    assert reindex_shard(healthy, tmp_path, REPO) is True
    assert healthy.repo_counts(REPO) == (2, 1)
    healthy.close()

    write_shard(tmp_path, _shard(5))
    store = _EdgesFail(db)
    with pytest.raises(OSError, match="disk went away"):
        reindex_shard(store, tmp_path, REPO)

    # The previous graph is intact: same counts, old node present, new node absent.
    assert store.repo_counts(REPO) == (2, 1)
    assert store.get_node("n1") is not None
    assert store.get_node("n4") is None
    store.close()


def test_reindex_shard_still_works_on_a_store_with_no_transaction_method(tmp_path):
    """Other ``Store`` implementations and test fakes have no ``transaction``; they get the
    old sequence and no atomicity, and no AttributeError."""
    calls = []

    class _Bare:
        def clear_repo(self, repo_id):
            calls.append(("clear", repo_id))

        def upsert_nodes(self, repo_id, nodes):
            calls.append(("nodes", len(list(nodes))))

        def upsert_edges(self, repo_id, edges):
            calls.append(("edges", len(list(edges))))

    write_shard(tmp_path, _shard(3))
    assert reindex_shard(_Bare(), tmp_path, REPO) is True
    assert calls == [("clear", REPO), ("nodes", 3), ("edges", 2)]


# --- SqliteStore.transaction itself ------------------------------------------------

def test_transaction_commits_once_at_the_end_and_is_invisible_before(tmp_path):
    db = tmp_path / "kb.sqlite"
    writer = SqliteStore(db)
    reader = SqliteStore(db)
    with writer.transaction():
        writer.upsert_nodes(REPO, [Node(id="a", repo=REPO, kind="function", name="a")])
        writer.upsert_nodes(REPO, [Node(id="b", repo=REPO, kind="function", name="b")])
        assert reader.get_node("a") is None, "no commit yet"
    assert reader.get_node("a") is not None and reader.get_node("b") is not None
    writer.close()
    reader.close()


def test_transaction_rolls_back_every_write_on_an_exception(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    store.upsert_nodes(REPO, [Node(id="keep", repo=REPO, kind="function", name="keep")])
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.clear_repo(REPO)
            store.upsert_nodes(REPO, [Node(id="new", repo=REPO, kind="function", name="n")])
            raise RuntimeError("boom")
    assert store.get_node("keep") is not None
    assert store.get_node("new") is None
    store.close()


def test_a_nested_transaction_joins_the_outer_one(tmp_path):
    store = SqliteStore(tmp_path / "kb.sqlite")
    with pytest.raises(RuntimeError, match="outer fails"):
        with store.transaction():
            with store.transaction():
                store.upsert_nodes(REPO, [Node(id="in", repo=REPO, kind="function", name="i")])
            # The inner block ended cleanly, but it did not commit: the outer one owns that.
            raise RuntimeError("outer fails")
    assert store.get_node("in") is None
    store.close()


def test_the_real_connection_is_back_after_the_block_however_it_ended(tmp_path):
    import sqlite3

    store = SqliteStore(tmp_path / "kb.sqlite")
    with store.transaction():
        assert not isinstance(store.conn, sqlite3.Connection)
    assert isinstance(store.conn, sqlite3.Connection)
    with pytest.raises(RuntimeError):
        with store.transaction():
            raise RuntimeError("x")
    assert isinstance(store.conn, sqlite3.Connection)
    store.upsert_nodes(REPO, [Node(id="z", repo=REPO, kind="function", name="z")])
    store.close()
    again = SqliteStore(tmp_path / "kb.sqlite")
    assert again.get_node("z") is not None, "commits work again"
    again.close()
