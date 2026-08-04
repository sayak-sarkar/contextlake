"""Index state + staleness helpers.

Tracks, per repo, the commit it was indexed at, when, and by which parser — so a
re-sync can skip unchanged repos (incremental indexing, Phase 2.6) — and gates
the store's schema version so an older binary refuses to operate on a newer
database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .store.base import Store
from .store.sqlite_store import SCHEMA_VERSION


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_repo_indexed(store: Store, repo_id: str, head_commit: str | None,
                      parser_version: str | None = None) -> None:
    """Stamp a repo as indexed at its current commit, now, by ``parser_version``.

    Callers pass the *shard's* stamp so the row mirrors the file on disk.
    """
    store.mark_indexed(repo_id, head_commit, utcnow_iso(), parser_version)


def needs_reindex(store: Store, repo_id: str, current_head: str | None) -> bool:
    """True if the repo was never indexed or its HEAD moved since last index.

    Deliberately HEAD-only. A graph can also be stale because the *parser* moved
    on while the commit stayed put; that is a separate question with a different
    cost profile, asked via :func:`indexed_parser_version`, so that each caller
    opts into it rather than inheriting it.
    """
    repo = store.get_repo(repo_id)
    if repo is None or repo.head_commit is None:
        return True
    return repo.head_commit != current_head


def indexed_parser_version(store: Store, store_dir: str | Path,
                           repo_id: str) -> str | None:
    """Which parser built this repo's graph, or None if that can't be established.

    Prefers the ``repos`` row's stamp: one indexed lookup, which is what lets a
    fleet-wide incremental pass ask this per repo without parsing anything. A
    store written before that column existed carries no stamp, so fall back to
    the shard, which is the source of truth and carries its own
    ``parser_version``.

    That fallback reads the head of the shard file rather than the whole thing.
    It cannot be assumed to fire only once: a repo is stamped when it is
    *indexed*, so a store whose shards are already current but whose rows predate
    the column (anyone who ran ``kb index --force`` on the previous release, i.e.
    exactly what its notes told them to do) has nothing to re-index and therefore
    stays unstamped. Those repos ask this on every run, so the cheap read is the
    difference between a negligible per-repo cost and re-parsing every shard in
    the fleet forever. Only when the cheap read cannot answer does this parse the
    shard in full.

    None means "unknown": no stamp and no readable shard. Callers must treat that
    as stale, since a graph whose provenance cannot be established is exactly the
    kind an agent should not be citing from.
    """
    stamped = store.get_repo_parser_version(repo_id)
    if stamped is not None:
        return stamped
    from .store.shards import peek_parser_version, read_shard

    peeked = peek_parser_version(store_dir, repo_id)
    if peeked is not None:
        return peeked
    shard = read_shard(store_dir, repo_id)
    return shard.parser_version if shard is not None else None


def check_schema(store: Store) -> None:
    """Refuse to operate on a database newer than this build understands."""
    raw = store.get_meta("schema_version")
    if raw is not None and int(raw) > SCHEMA_VERSION:
        raise RuntimeError(
            f"knowledge-base schema v{raw} is newer than supported v{SCHEMA_VERSION}; "
            "upgrade contextlake"
        )
