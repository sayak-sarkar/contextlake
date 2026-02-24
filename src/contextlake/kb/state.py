"""Index state + staleness helpers.

Tracks, per repo, the commit it was indexed at and when — so a re-sync can skip
unchanged repos (incremental indexing, Phase 2.6) — and gates the store's schema
version so an older binary refuses to operate on a newer database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .store.base import Store
from .store.sqlite_store import SCHEMA_VERSION


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_repo_indexed(store: Store, repo_id: str, head_commit: str | None) -> None:
    """Stamp a repo as indexed at its current commit, now."""
    store.mark_indexed(repo_id, head_commit, utcnow_iso())


def needs_reindex(store: Store, repo_id: str, current_head: str | None) -> bool:
    """True if the repo was never indexed or its HEAD moved since last index."""
    repo = store.get_repo(repo_id)
    if repo is None or repo.head_commit is None:
        return True
    return repo.head_commit != current_head


def check_schema(store: Store) -> None:
    """Refuse to operate on a database newer than this build understands."""
    raw = store.get_meta("schema_version")
    if raw is not None and int(raw) > SCHEMA_VERSION:
        raise RuntimeError(
            f"knowledge-base schema v{raw} is newer than supported v{SCHEMA_VERSION}; "
            "upgrade contextlake"
        )
