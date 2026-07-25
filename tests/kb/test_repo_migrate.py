"""repo_id migration: clearing stale (pre-canonicalization) repo rows."""

import subprocess

from contextlake.kb.model import Repo
from contextlake.kb.repo_migrate import migrate_stale_repo_ids
from contextlake.kb.store.shards import GraphShard, shard_path, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


def _git_repo(path, *, remote):
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "a@b.c")
    run("config", "user.name", "a")
    (path / "f.txt").write_text("x")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    run("remote", "add", "origin", remote)


def _store(tmp_path):
    return SqliteStore(tmp_path / "kb" / "index.sqlite"), tmp_path / "kb"


def test_migrate_clears_a_stale_row_entirely(tmp_path):
    repo_dir = tmp_path / "some" / "old" / "workspace" / "path"
    _git_repo(repo_dir, remote="https://example.com/acme/widgets.git")
    store, store_dir = _store(tmp_path)
    try:
        old_id = "some/old/workspace/path"   # the pre-canonicalization scheme
        store.upsert_repo(Repo(id=old_id, path=str(repo_dir)))
        store.upsert_nodes(old_id, [])
        write_shard(store_dir, GraphShard(repo=old_id, nodes=[], edges=[]))

        result = migrate_stale_repo_ids(store, store_dir)

        assert result.cleared == [(old_id, "example.com/acme/widgets")]
        assert store.get_repo(old_id) is None          # row gone, not just emptied
        assert not shard_path(store_dir, old_id).exists()
    finally:
        store.close()


def test_migrate_is_a_noop_for_already_canonical_rows(tmp_path):
    repo_dir = tmp_path / "clone"
    _git_repo(repo_dir, remote="https://example.com/acme/widgets.git")
    store, store_dir = _store(tmp_path)
    try:
        store.upsert_repo(Repo(id="example.com/acme/widgets", path=str(repo_dir)))
        result = migrate_stale_repo_ids(store, store_dir)
        assert result.cleared == []
        assert store.get_repo("example.com/acme/widgets") is not None
    finally:
        store.close()


def test_migrate_skips_a_repo_whose_path_no_longer_exists(tmp_path):
    store, store_dir = _store(tmp_path)
    try:
        store.upsert_repo(Repo(id="old/path", path=str(tmp_path / "gone")))
        result = migrate_stale_repo_ids(store, store_dir)
        assert result.cleared == []
        assert result.skipped_missing_path == ["old/path"]
        assert store.get_repo("old/path") is not None   # left as-is, not guessed at
    finally:
        store.close()


def test_migrate_clears_stale_repo_vectors_from_a_real_vector_store(tmp_path):
    """Not just the file-absent no-op path: vectors survive a repo's node ids
    changing (they're keyed by the old ids) unless explicitly cleared, so this
    exercises clear_repo against a real embeddings.sqlite -- whichever backend
    (sqlite-vec or the pure-Python fallback) this environment resolves to."""
    from contextlake.kb.embeddings.store import build_vector_store

    repo_dir = tmp_path / "old-path"
    _git_repo(repo_dir, remote="https://example.com/acme/widgets.git")
    store, store_dir = _store(tmp_path)
    try:
        old_id = "old-path"
        store.upsert_repo(Repo(id=old_id, path=str(repo_dir)))

        vs = build_vector_store(store_dir / "embeddings.sqlite")
        vs.upsert([("n1", old_id, [0.1, 0.2, 0.3]), ("n2", "other-repo", [0.4, 0.5, 0.6])])
        vs.close()

        migrate_stale_repo_ids(store, store_dir)

        vs2 = build_vector_store(store_dir / "embeddings.sqlite")
        try:
            assert vs2.count() == 1   # old_id's vector cleared, other-repo's kept
            assert vs2.search([0.4, 0.5, 0.6], k=5)[0][0] == "n2"
        finally:
            vs2.close()
    finally:
        store.close()


def test_migrate_skips_list_repos_once_a_store_is_known_clean(tmp_path, monkeypatch):
    """`index --watch` calls migrate_stale_repo_ids every tick -- once a store has
    nothing left to migrate, later calls in the same process must not re-query
    list_repos() (a git subprocess spawn per row on a large fleet, every tick,
    forever, for no reason)."""
    import contextlake.kb.repo_migrate as repo_migrate_mod

    monkeypatch.setattr(repo_migrate_mod, "_clean_store_dirs", set())
    repo_dir = tmp_path / "clone"
    _git_repo(repo_dir, remote="https://example.com/acme/widgets.git")
    store, store_dir = _store(tmp_path)
    try:
        store.upsert_repo(Repo(id="example.com/acme/widgets", path=str(repo_dir)))
        calls = []
        real_list_repos = store.list_repos
        store.list_repos = lambda: (calls.append(1) or real_list_repos())

        migrate_stale_repo_ids(store, store_dir)   # nothing stale -> marks the dir clean
        assert len(calls) == 1
        migrate_stale_repo_ids(store, store_dir)   # must short-circuit before list_repos()
        assert len(calls) == 1
    finally:
        store.close()


def test_migrate_is_idempotent_no_ghost_row_on_second_run(tmp_path):
    repo_dir = tmp_path / "old-path"
    _git_repo(repo_dir, remote="https://example.com/acme/widgets.git")
    store, store_dir = _store(tmp_path)
    try:
        store.upsert_repo(Repo(id="old-path", path=str(repo_dir)))
        migrate_stale_repo_ids(store, store_dir)
        second = migrate_stale_repo_ids(store, store_dir)
        assert second.cleared == []   # nothing left to migrate -- not re-cleared forever
    finally:
        store.close()
