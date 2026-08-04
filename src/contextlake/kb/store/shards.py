"""Per-repo graph shards — the durable source of truth, one file per repo.

The SQLite store (sqlite_store.py) is a rebuildable index over these shards. A
shard is a self-contained JSON document of a single repo's nodes + edges, so a
repo can be re-indexed in isolation and shards stay small (sidestepping the
single-global-graph size ceiling that a monolithic graph would hit at scale).
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from pathlib import Path

from pydantic import BaseModel, Field

from ...logging_setup import log
from ..model import Edge, Node
from .base import Store

# Soft per-shard size warning; a single repo should never approach this.
_SHARD_WARN_BYTES = 50 * 1024 * 1024

# In-memory cache of parsed shards, so a long-lived process (the dashboard
# server, above all -- every ``/api/repo/<id>`` request used to re-read and
# re-``model_validate_json`` the entire shard from scratch, which is the
# dominant, size-scaling cost of that endpoint on a large repo) doesn't pay
# the JSON-parse + pydantic-validation cost again for a shard it already has.
# Keyed by the resolved shard path, validated on every read against the
# file's current (mtime_ns, size) so a shard rewritten by a *different*
# process (a `contextlake index` run while `dashboard --serve` stays up) is
# still picked up -- never served stale just because it's cached.
# ``write_shard`` additionally evicts its own entry synchronously, so a
# same-process write is reflected on the very next read regardless of the
# filesystem's mtime resolution.
#
# Bounded by *estimated resident bytes*, not entry count: a shard's parsed
# pydantic objects (each Node/Edge, its nested Provenance, enum, several
# strings) cost far more resident memory than its on-disk JSON -- measured at
# ~13x on a real 20k-node/97k-edge shard (27.6 MB on disk -> ~363 MB resident,
# via `resource.getrusage(...).ru_maxrss` before/after a single `read_shard`).
# An entry-count cap alone (e.g. 256) would happily pin dozens of large repos'
# shards in memory before ever firing -- a real OOM risk on the multi-hundred-
# repo fleets this is meant to serve, trading a latency bug for a memory one.
# `_RESIDENT_SIZE_MULTIPLIER` estimates resident cost from the cheap-to-get
# on-disk size (rounded up from the measured ratio for safety margin); a
# single shard that alone would exceed the whole budget is simply never
# cached (still parsed and returned correctly -- it just loses the cross-request
# speedup, which is the right trade-off for a single pathological outlier).
_RESIDENT_SIZE_MULTIPLIER = 15
# ~2 GiB of *estimated* resident bytes -- sized to comfortably cache at least
# one repo at the originally-reported scale (a 75 MB on-disk shard estimates
# to ~1.1 GiB resident at this multiplier) while still bounding total growth
# to roughly that many large repos at once, not dozens.
_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_CACHE_MAX_SHARDS = 128  # secondary bound: caps overhead if many small shards are hot
_shard_cache: OrderedDict[str, tuple[int, int, GraphShard]] = OrderedDict()
_shard_cache_bytes = 0  # running total of estimated resident bytes currently cached
_shard_cache_lock = threading.Lock()


# Caches held *elsewhere* that are derived from a parsed shard and keyed on the
# same (path, mtime_ns, size) identity -- ``wiki.generate``'s ``repo_brief``
# aggregation is the one in tree. They need the same same-process invalidation
# ``write_shard`` gives this cache, and for the same reason: a rewrite whose
# (mtime_ns, size) happens to match the previous file's (a re-index at an
# unchanged commit producing byte-identical-length output, on a filesystem whose
# real mtime resolution is coarser than a nanosecond) is invisible to an
# identity check alone. Without this, a derived cache could pair a stale
# aggregation with the freshly-read shard's ``head_commit`` -- exactly the
# split-observation mismatch ``read_shard_with_identity`` exists to prevent.
_derived_invalidators: list = []


def register_shard_invalidator(fn) -> None:
    """Register ``fn(path_key)``, called whenever this process rewrites a shard,
    so a cache derived from that shard drops its entries for it too."""
    _derived_invalidators.append(fn)


def _cache_evict(path_key: str) -> None:
    global _shard_cache_bytes
    with _shard_cache_lock:
        cached = _shard_cache.pop(path_key, None)
        if cached is not None:
            _shard_cache_bytes -= cached[1] * _RESIDENT_SIZE_MULTIPLIER
    # Outside the lock: an invalidator takes its own lock, and nesting this one
    # around it would be the only place the two are ever held together.
    for fn in _derived_invalidators:
        fn(path_key)


def _cache_get(path_key: str, mtime_ns: int, size: int) -> GraphShard | None:
    with _shard_cache_lock:
        cached = _shard_cache.get(path_key)
        if cached is None or cached[0] != mtime_ns or cached[1] != size:
            return None
        _shard_cache.move_to_end(path_key)
        return cached[2]


def _cache_put(path_key: str, mtime_ns: int, size: int, shard: GraphShard) -> None:
    global _shard_cache_bytes
    est_bytes = size * _RESIDENT_SIZE_MULTIPLIER
    if est_bytes > _CACHE_MAX_BYTES:
        return  # too big to cache at all -- caller still gets the parsed shard back
    with _shard_cache_lock:
        old = _shard_cache.pop(path_key, None)
        if old is not None:
            _shard_cache_bytes -= old[1] * _RESIDENT_SIZE_MULTIPLIER
        _shard_cache[path_key] = (mtime_ns, size, shard)
        _shard_cache_bytes += est_bytes
        # Oldest-first eviction (this entry is always last, being just-inserted)
        # until back under budget. The `len > 1` guard means a single entry that
        # alone fits the budget (guaranteed by the check above) is never evicted
        # by this loop -- only entries older than it are.
        while (_shard_cache_bytes > _CACHE_MAX_BYTES or len(_shard_cache) > _CACHE_MAX_SHARDS) \
                and len(_shard_cache) > 1:
            _, evicted = _shard_cache.popitem(last=False)
            _shard_cache_bytes -= evicted[1] * _RESIDENT_SIZE_MULTIPLIER


class GraphShard(BaseModel):
    repo: str
    head_commit: str | None = None
    parser_version: str | None = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


def shard_path(store_dir: str | Path, repo_id: str) -> Path:
    """Path of a repo's shard; the repo's namespace nests as directories.

    repo_id legitimately contains ``/`` (namespace nesting), but it can arrive from an
    untrusted caller (e.g. an MCP tool argument), so reject any value that would escape
    the ``graph/`` directory (``..`` traversal, absolute paths) rather than reading an
    arbitrary file off disk.
    """
    base = (Path(store_dir) / "graph").resolve()
    p = (base / f"{repo_id}.json").resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"invalid repo id (path escape): {repo_id!r}")
    return p


def write_shard(store_dir: str | Path, shard: GraphShard) -> Path:
    p = shard_path(store_dir, shard.repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = shard.model_dump_json(indent=2)
    p.write_text(data, encoding="utf-8")
    if len(data.encode("utf-8")) > _SHARD_WARN_BYTES:
        log(f"WARNING: shard for {shard.repo} exceeds {_SHARD_WARN_BYTES // (1024 * 1024)} MiB")
    _cache_evict(str(p))  # this process's own view must never read back stale
    return p


def read_shard_with_identity(
    store_dir: str | Path, repo_id: str,
) -> tuple[GraphShard | None, tuple[str, int, int] | None]:
    """Like :func:`read_shard`, but also returns the exact ``(path, mtime_ns,
    size)`` identity this particular shard was read/validated against.

    For a caller that derives its OWN cache from the shard (``wiki.generate``'s
    ``repo_brief``): re-``stat()``-ing the file a second time to build that
    cache's key would open a race window the width of the parse itself -- a
    shard rewritten by another process in between would make the second stat
    key a derived cache entry under the *new* file's identity while its content
    was actually computed from the *old* shard this call returned, silently
    mismatching one field (e.g. ``head``) against another (e.g. ``node_count``)
    on every subsequent cache hit until the next rewrite. Passing this same
    identity through instead means there is only ever one observation of the
    file per call, so no such window exists.
    """
    try:
        p = shard_path(store_dir, repo_id)
    except ValueError:
        return None, None  # traversal / invalid id -> treat as "no such shard"
    try:
        st = p.stat()
    except OSError:
        return None, None
    key = str(p)
    identity = (key, st.st_mtime_ns, st.st_size)
    cached = _cache_get(key, st.st_mtime_ns, st.st_size)
    if cached is not None:
        return cached, identity
    shard = GraphShard.model_validate_json(p.read_text(encoding="utf-8"))
    _cache_put(key, st.st_mtime_ns, st.st_size, shard)
    return shard, identity


def read_shard(store_dir: str | Path, repo_id: str) -> GraphShard | None:
    shard, _ = read_shard_with_identity(store_dir, repo_id)
    return shard


# ``model_dump_json(indent=2)`` writes fields in model-declaration order, so
# ``parser_version`` sits in the first few lines, ahead of the nodes/edges that
# make up essentially the whole file. Anchored to the two-space top-level indent
# so it can only match the shard's own field, never a string inside a node.
_PARSER_VERSION_HEAD_BYTES = 8192
_PARSER_VERSION_RE = re.compile(rb'\n  "parser_version":\s*"([^"]*)"')


def peek_parser_version(store_dir: str | Path, repo_id: str) -> str | None:
    """The shard's ``parser_version`` read from the head of the file, or None if
    it cannot be answered that cheaply (no shard, or the field is not where the
    writer puts it).

    Exists because the alternative -- ``read_shard`` -- parses and validates the
    entire shard, and the caller asking this question is a fleet-wide incremental
    pass that asks it once per repo. On a several-hundred-repo store with shards
    running to tens of MB, paying a full parse of every shard to read one short
    string would turn a cheap pre-filter into the most expensive part of the run.
    A None answer is never *wrong*, only uninformative: callers fall back to
    ``read_shard``.
    """
    try:
        p = shard_path(store_dir, repo_id)
        with p.open("rb") as fh:
            head = fh.read(_PARSER_VERSION_HEAD_BYTES)
    except (OSError, ValueError):
        return None
    m = _PARSER_VERSION_RE.search(head)
    return m.group(1).decode("utf-8", "replace") if m else None


# --- bi-temporal history: snapshot each indexed shard by commit ------------

def history_path(store_dir: str | Path, repo_id: str, commit: str) -> Path:
    return Path(store_dir) / "history" / repo_id / f"{commit}.json"


def archive_shard(store_dir: str | Path, shard: GraphShard) -> Path | None:
    """Snapshot a shard under history/<repo>/<commit>.json for 'as of' queries.

    Returns None when the shard has no commit to key on. Snapshots are immutable
    once written: a repo re-indexed at the same commit, *by the same
    ``parse.PARSER_VERSION``, on the same machine*, overwrites identically.

    Both qualifiers are load-bearing; the unqualified claim this docstring used to
    make was false. Until ``parse._sorted_captures`` landed, tree-sitter
    capture-order entropy meant one unchanged repo produced different shard bytes
    on *every* index, so this overwrote with different content each time. A
    PARSER_VERSION bump then deliberately changes shard bytes for the same commit,
    so the invariant can only ever hold within one version. And file nodes are
    emitted in ``os.walk`` order, which is filesystem-dependent -- so the bytes are
    reproducible for a re-index of the same checkout, but are NOT a
    machine-independent function of (repo, commit).

    Which is to say: this is safe to rely on for "did this local store's snapshot
    change?", and is not a basis for content-addressing a shard across versions or
    across machines (e.g. comparing hashes between CI runners).
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
