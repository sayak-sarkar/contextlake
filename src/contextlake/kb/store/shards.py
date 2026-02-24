"""Per-repo graph shards — the durable source of truth, one file per repo.

The SQLite store (sqlite_store.py) is a rebuildable index over these shards. A
shard is a self-contained JSON document of a single repo's nodes + edges, so a
repo can be re-indexed in isolation and shards stay small (sidestepping the
single-global-graph size ceiling that a monolithic graph would hit at scale).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ...logging_setup import log
from ..model import Edge, Node
from .base import Store

# Soft per-shard size warning; a single repo should never approach this.
_SHARD_WARN_BYTES = 50 * 1024 * 1024


class GraphShard(BaseModel):
    repo: str
    head_commit: str | None = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


def shard_path(store_dir: str | Path, repo_id: str) -> Path:
    """Path of a repo's shard; the repo's namespace nests as directories."""
    return Path(store_dir) / "graph" / f"{repo_id}.json"


def write_shard(store_dir: str | Path, shard: GraphShard) -> Path:
    p = shard_path(store_dir, shard.repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = shard.model_dump_json(indent=2)
    p.write_text(data, encoding="utf-8")
    if len(data.encode("utf-8")) > _SHARD_WARN_BYTES:
        log(f"WARNING: shard for {shard.repo} exceeds {_SHARD_WARN_BYTES // (1024 * 1024)} MiB")
    return p


def read_shard(store_dir: str | Path, repo_id: str) -> GraphShard | None:
    p = shard_path(store_dir, repo_id)
    if not p.exists():
        return None
    return GraphShard.model_validate_json(p.read_text(encoding="utf-8"))


# --- bi-temporal history: snapshot each indexed shard by commit ------------

def history_path(store_dir: str | Path, repo_id: str, commit: str) -> Path:
    return Path(store_dir) / "history" / repo_id / f"{commit}.json"


def archive_shard(store_dir: str | Path, shard: GraphShard) -> Path | None:
    """Snapshot a shard under history/<repo>/<commit>.json for 'as of' queries.

    Returns None when the shard has no commit to key on. Snapshots are immutable
    once written: a repo re-indexed at the same commit overwrites identically.
    """
    if not shard.head_commit:
        return None
    p = history_path(store_dir, shard.repo, shard.head_commit)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(shard.model_dump_json(indent=2), encoding="utf-8")
    return p


def read_shard_at(store_dir: str | Path, repo_id: str, commit: str) -> GraphShard | None:
    """The repo's snapshot at ``commit`` (from history, or the current shard if it
    is at that commit), or None if that commit was never indexed."""
    p = history_path(store_dir, repo_id, commit)
    if p.exists():
        return GraphShard.model_validate_json(p.read_text(encoding="utf-8"))
    current = read_shard(store_dir, repo_id)
    if current is not None and current.head_commit == commit:
        return current
    return None


def list_indexed_commits(store_dir: str | Path, repo_id: str) -> list[str]:
    d = Path(store_dir) / "history" / repo_id
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def reindex_shard(store: Store, store_dir: str | Path, repo_id: str) -> bool:
    """Load a repo's shard and (re)index it into the store. Returns False if absent."""
    shard = read_shard(store_dir, repo_id)
    if shard is None:
        return False
    store.clear_repo(repo_id)
    store.upsert_nodes(repo_id, shard.nodes)
    store.upsert_edges(repo_id, shard.edges)
    return True
