"""One-time migration off the old workspace-relative ``repo_id`` scheme.

Before this, ``repo_id`` was a repo's path relative to ``--workspace`` (see
``repo_identity.py`` for why that was ambiguous). This clears a stale row's
data under its old id; the normal incremental index loop that runs right
after (in ``commands._index_workspace``) then re-derives it fresh under the
canonical id it now discovers -- the same code path a brand-new repo goes
through, not a parallel re-implementation of indexing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import log
from .repo_identity import describe_gitdir_mismatch, is_own_gitdir, resolve_repo_id
from .store.shards import history_path, shard_path


@dataclass
class MigrationResult:
    cleared: list[tuple[str, str]] = field(default_factory=list)  # (old_id, new_id)
    skipped_missing_path: list[str] = field(default_factory=list)


# Once a store has nothing left to clear, remember it for this process's lifetime
# so `index --watch` (which calls migrate_stale_repo_ids on every tick) doesn't
# pay a list_repos() + resolve_repo_id()-per-row cost every interval forever --
# on a large fleet that's hundreds of git subprocess spawns per tick for no reason
# once the one-time migration is done. Not persisted: a fresh process re-checks
# once, which is correct and cheap.
_clean_store_dirs: set[str] = set()


def _clear_shard_files(store_dir, repo_id: str) -> None:
    try:
        p = shard_path(store_dir, repo_id)
    except ValueError:
        return
    p.unlink(missing_ok=True)
    hist = Path(history_path(store_dir, repo_id, "x")).parent
    if hist.is_dir():
        shutil.rmtree(hist, ignore_errors=True)


def _clear_vectors(store_dir, repo_id: str) -> None:
    """Vectors are keyed by node id, which changes with repo_id -- clear this
    repo's embeddings so nothing stale lingers under an id that no longer
    resolves to anything. `contextlake embed` rebuilds them on the next run."""
    vec_path = Path(store_dir) / "embeddings.sqlite"
    if not vec_path.exists():
        return
    try:
        from .embeddings.store import build_vector_store

        vs = build_vector_store(vec_path)
        try:
            vs.clear_repo(repo_id)
        finally:
            vs.close()
    except Exception as e:  # noqa: BLE001 - a migration must not fail on the optional vector tier
        log(f"  note: could not clear stale embeddings for {repo_id}: {e}")


def migrate_stale_repo_ids(store, store_dir) -> MigrationResult:
    """Clear any existing repo whose stored id no longer matches the canonical
    id its path resolves to today. Idempotent: a store with nothing stale is a
    fast no-op (one ``list_repos`` + one canonical-id resolve per row).

    Does not re-index -- the caller's normal discovery+incremental-index loop
    does that immediately after, since the cleared repo now has no row under
    any id and is picked up like any other repo needing a first index.
    """
    key = str(store_dir)
    if key in _clean_store_dirs:
        return MigrationResult()

    result = MigrationResult()
    for repo in store.list_repos():
        path = Path(repo.path)
        if not path.exists():
            # Can't resolve a canonical id without the checkout; leave the old
            # row as-is rather than guess -- re-add the repo to migrate it.
            result.skipped_missing_path.append(repo.id)
            continue
        if not is_own_gitdir(repo.path):
            # .git no longer resolves to this exact directory since indexing --
            # either git can't find it at all, or (the dangerous case) it silently
            # resolves an ancestor repo's identity instead. Trusting either here
            # would delete this repo's real data on a false "id changed" signal
            # and never correctly re-index it. Leave the row as-is, same as a
            # missing checkout.
            log(f"  note: {repo.id} at {repo.path}: {describe_gitdir_mismatch(repo.path)}; "
                "skipping id migration for it")
            result.skipped_missing_path.append(repo.id)
            continue
        new_id = resolve_repo_id(repo.path)
        if new_id == repo.id:
            continue  # already canonical
        store.delete_repo(repo.id)  # drops the repos row too, not just its content --
        # clear_repo alone would leave a zero-node ghost row whose id still
        # mismatches its path, so migration would "re-migrate" it forever.
        _clear_shard_files(store_dir, repo.id)
        _clear_vectors(store_dir, repo.id)
        result.cleared.append((repo.id, new_id))

    if result.cleared:
        log(f"repo_id migration: {len(result.cleared)} repo(s) moving to canonical ids "
            "(re-indexing now, same as a first index):")
        for old_id, new_id in result.cleared:
            log(f"  {old_id} -> {new_id}")
    if result.skipped_missing_path:
        log(f"repo_id migration: {len(result.skipped_missing_path)} repo(s) skipped -- "
            "stored path no longer exists; re-add them to migrate.")
    if not result.cleared:
        _clean_store_dirs.add(key)
    return result
