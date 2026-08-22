"""A minimal local vector store: SQLite-backed, pure-Python cosine search.

Vectors are persisted as packed float32 blobs alongside a precomputed norm;
search is brute-force cosine. This is dependency-free and correct — good enough to
ship the semantic tier. A native ANN index (e.g. sqlite-vec) can replace the
search path later behind this same interface, without touching callers.
"""

from __future__ import annotations

import array
import logging
import math
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1


def _pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


#: Separates a node id from its chunk index in a stored vector key. See `chunk_key`.
CHUNK_SEP = "\x1f"

#: How many rows to pull before collapsing chunks back to nodes. Several chunks of one
#: document can occupy the top of the ranking, so asking the backend for exactly k rows
#: would routinely return fewer than k distinct documents. Eight is generous enough that a
#: document would have to own the entire neighbourhood to shorten the result, and small
#: enough that the extra rows cost nothing measurable.
_CHUNK_OVERFETCH = 8


def _norm(vec) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def _repo_scope(repo_id: str) -> list[str]:
    """Expand a ``repo=`` filter to the repo's own shard plus its linked
    connector/enrichment partitions.

    ``connect``/``enrich`` deliberately write into separate ``@connect:<repo>``/
    ``@enrich:<repo>`` partitions (see connectors/orchestrate.py, connectors/enrich.py)
    so re-indexing a repo's code never clobbers connector output and vice versa.
    That isolation is a *write*-side concern; it must not leak into *search* --
    a caller filtering by repo id expects everything the repo's graph node links
    to, not just the literal code shard. Imported lazily to keep the connectors'
    (heavier) dependency chain out of every basic vector-store import; both
    ``connect_partition``/``enrich_partition`` are pure string formatting with no
    further imports of their own, so this only ever pulls in their defining
    modules. Falls back to the literal repo id alone if that import fails (e.g. a
    partial install without the connectors' own optional deps), so a broken
    environment degrades to the old exact-match behavior instead of crashing
    every repo-scoped search."""
    try:
        from ..connectors.enrich import enrich_partition
        from ..connectors.orchestrate import connect_partition
    except ImportError:
        return [repo_id]

    return [repo_id, connect_partition(repo_id), enrich_partition(repo_id)]


class VectorStore:
    """Persisted node embeddings with cosine top-k search."""

    name = "brute"

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        _ = self.conn  # eagerly open + init schema on the constructing thread

    @property
    def conn(self) -> sqlite3.Connection:
        """A connection scoped to the calling thread -- see SqliteStore.conn's
        docstring for why a server-lifetime store can't share one connection
        across threads (contextlake serve's MCP tool dispatch)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            self._init_schema()
        return conn

    def _init_schema(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "node_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, dim INTEGER NOT NULL, "
            "norm REAL NOT NULL, vec BLOB NOT NULL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emb_repo ON embeddings(repo_id)"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vec_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def upsert(self, items) -> int:
        """Insert/replace ``(node_id, repo_id, vector)`` triples. Returns the count."""
        rows = [
            (node_id, repo_id, len(vec), _norm(vec), _pack(vec))
            for node_id, repo_id, vec in items
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings(node_id, repo_id, dim, norm, vec) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def clear_repo(self, repo_id: str) -> None:
        self.conn.execute("DELETE FROM embeddings WHERE repo_id=?", (repo_id,))
        self.conn.commit()

    def count_repo(self, repo_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE repo_id=?", (repo_id,)).fetchone()[0]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def has_any(self, repo_ids=None) -> bool:
        """Whether any vector exists (optionally within ``repo_ids``).

        A probe, not a count: this runs before EVERY semantic query only to tell an
        empty index from a populated one, and that question is answered by the first
        row. `COUNT(*)` over a virtual table is not guaranteed cheap on the ANN
        backend, which is exactly the store where the extra work would be felt.
        """
        if repo_ids is None:
            row = self.conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
        else:
            ids = list(repo_ids)
            if not ids:
                return False
            sql = (f"SELECT 1 FROM embeddings "  # noqa: S608 - placeholders only
                   f"WHERE repo_id IN ({','.join('?' * len(ids))}) LIMIT 1")
            row = self.conn.execute(sql, tuple(ids)).fetchone()
        return row is not None

    def search(self, query, k: int = 10, repo: str | None = None) -> list[tuple[str, float]]:
        """Return the ``k`` nearest node_ids to ``query`` by cosine, high score first."""
        qnorm = _norm(query)
        qlen = len(query)
        sql = "SELECT node_id, dim, norm, vec FROM embeddings"
        params: tuple = ()
        if repo:
            ids = _repo_scope(repo)
            sql += f" WHERE repo_id IN ({','.join('?' * len(ids))})"
            params = tuple(ids)
        scored: list[tuple[str, float]] = []
        for node_id, dim, norm, blob in self.conn.execute(sql, params):
            if dim != qlen:
                continue
            dot = sum(a * b for a, b in zip(query, _unpack(blob), strict=True))
            scored.append((node_id, dot / (qnorm * norm)))
        scored.sort(key=lambda t: t[1], reverse=True)
        # Collapse before trimming, not after: trimming first would drop a document whose
        # only high-ranking chunk sat behind several chunks of a different one.
        return _collapse_chunks(scored, k)

    def close(self) -> None:
        self.conn.close()


class SqliteVecStore:
    """Vector store backed by the sqlite-vec extension (native cosine KNN).

    Same interface as ``VectorStore`` but with an ANN index, for large workspaces.
    Requires the optional ``sqlite-vec`` package and a Python ``sqlite3`` that
    permits extension loading; ``build_vector_store`` falls back to ``VectorStore``
    when either is missing.
    """

    name = "sqlite-vec"

    def __init__(self, path: str | Path, *, chunk_size: int = 1024):
        self.path = str(path)
        # vec0 requires the chunk size to be a positive multiple of 8.
        self._chunk_size = max(8, (int(chunk_size) // 8) * 8)
        self._local = threading.local()
        self._dim: int | None = None
        self._has_table: bool | None = None  # None until the first connection sets it
        _ = self.conn  # eagerly open + load the extension on the constructing thread

    @property
    def conn(self) -> sqlite3.Connection:
        """A connection scoped to the calling thread -- see SqliteStore.conn's
        docstring for why a server-lifetime store can't share one connection
        across threads (contextlake serve's MCP tool dispatch). The sqlite-vec
        extension is loaded per-connection (it's not a file-level property), so
        every thread's first connection loads it independently; _dim/_has_table
        reflect the file itself, computed once and cached on the instance."""
        import sqlite_vec  # optional dependency; ImportError -> factory fallback

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            self._local.conn = conn
            if self._has_table is None:
                row = conn.execute("SELECT value FROM vec_meta WHERE key='dim'").fetchone()
                self._dim = int(row[0]) if row else None
                # 'dim' can be written by guard_store_identity independently of table
                # creation, so it is NOT a reliable "vec_items exists" sentinel; probe
                # sqlite_master for the real state. (Otherwise: guard a fresh store,
                # embed a zero-node workspace so upsert/_ensure_table never runs,
                # reopen -> _dim set but no table -> count/search/clear raise 'no such
                # table'.)
                self._has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_items'"
                ).fetchone() is not None
        return conn

    def _ensure_table(self, dim: int) -> None:
        self._dim = dim
        if self._has_table:
            return
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
            "node_id TEXT PRIMARY KEY, repo_id TEXT, "
            f"embedding FLOAT[{dim}] distance_metric=cosine, chunk_size={self._chunk_size})"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_meta(key, value) VALUES('dim', ?)", (str(dim),)
        )
        self.conn.commit()
        self._has_table = True

    def upsert(self, items) -> int:
        items = list(items)
        if not items:
            return 0
        self._ensure_table(len(items[0][2]))
        ids = [(it[0],) for it in items]
        # vec0 has no UPSERT, so delete any existing ids then insert.
        self.conn.executemany("DELETE FROM vec_items WHERE node_id=?", ids)
        self.conn.executemany(
            "INSERT INTO vec_items(node_id, repo_id, embedding) VALUES (?, ?, ?)",
            [(nid, rid, _pack(vec)) for nid, rid, vec in items],
        )
        self.conn.commit()
        return len(items)

    def clear_repo(self, repo_id: str) -> None:
        if not self._has_table:
            return
        self.conn.execute("DELETE FROM vec_items WHERE repo_id=?", (repo_id,))
        self.conn.commit()

    def count_repo(self, repo_id: str) -> int:
        if not self._has_table:
            return 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM vec_items WHERE repo_id=?", (repo_id,)).fetchone()[0]

    def count(self) -> int:
        if not self._has_table:
            return 0
        return self.conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0]

    def has_any(self, repo_ids=None) -> bool:
        """Whether any vector exists (optionally within ``repo_ids``). See
        `VectorStore.has_any`: a probe rather than a count, because `vec_items` is a
        virtual table and `COUNT(*)` over one carries no cheapness guarantee."""
        if not self._has_table:
            return False
        if repo_ids is None:
            row = self.conn.execute("SELECT 1 FROM vec_items LIMIT 1").fetchone()
        else:
            ids = list(repo_ids)
            if not ids:
                return False
            sql = (f"SELECT 1 FROM vec_items "  # noqa: S608 - placeholders only
                   f"WHERE repo_id IN ({','.join('?' * len(ids))}) LIMIT 1")
            row = self.conn.execute(sql, tuple(ids)).fetchone()
        return row is not None

    def search(self, query, k: int = 10, repo: str | None = None) -> list[tuple[str, float]]:
        if not self._has_table or self._dim is None or len(query) != self._dim:
            return []
        q = _pack(query)
        if repo:
            ids = _repo_scope(repo)
            sql = ("SELECT node_id, distance FROM vec_items "  # noqa: S608 - placeholders only
                   f"WHERE embedding MATCH ? AND repo_id IN ({','.join('?' * len(ids))}) "
                   "ORDER BY distance LIMIT ?")
            params: tuple = (q, *ids, k * _CHUNK_OVERFETCH)
        else:
            sql = ("SELECT node_id, distance FROM vec_items "
                   "WHERE embedding MATCH ? ORDER BY distance LIMIT ?")
            params = (q, k * _CHUNK_OVERFETCH)
        # vec0 returns cosine distance; convert to similarity to match VectorStore.
        rows = [(node_id, 1.0 - dist) for node_id, dist in self.conn.execute(sql, params)]
        return _collapse_chunks(rows, k)

    def close(self) -> None:
        self.conn.close()


def guard_store_identity(store, identity: str, dim: int) -> None:
    """Refuse to mix embedders / vector dimensions within one store.

    The brute search silently skips dimension-mismatched rows, so re-embedding an
    existing store with a different model (or dimension) would quietly degrade
    results. On an empty/pre-guard store this records the embedder identity and
    vector dim in ``vec_meta``; on a populated store it raises ``ValueError`` if
    either changed, telling the user to re-embed from scratch.
    """
    conn = store.conn

    def _get(key: str):
        row = conn.execute("SELECT value FROM vec_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    cur_dim, cur_id = _get("dim"), _get("embedder_identity")
    if cur_dim is not None and int(cur_dim) != dim:
        raise ValueError(
            f"this embedding store was built with dimension {cur_dim} but the "
            f"current embedder produces {dim}. Re-embed from scratch (delete the "
            f"store's embeddings.sqlite) or keep the original embedder."
        )
    if cur_id is not None and cur_id != identity:
        raise ValueError(
            f"this embedding store was built with embedder {cur_id!r} but the "
            f"current embedder is {identity!r}. Re-embed from scratch (delete the "
            f"store's embeddings.sqlite) or keep the original embedder."
        )
    conn.execute(
        "INSERT OR REPLACE INTO vec_meta(key, value) VALUES('embedder_identity', ?)",
        (identity,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO vec_meta(key, value) VALUES('dim', ?)", (str(dim),)
    )
    conn.commit()


def _put_or_clear(store, key: str, value: str | None) -> None:
    """Store ``value`` under ``key``, or DELETE the key when ``value`` is None.

    The single discipline the commit-keyed markers below follow: *absent* is the
    absence of a row, never a sentinel empty string. A marker written as ``""``
    used to read back as None, so "nobody has recorded this yet" and "what was
    recorded is the empty string" were the same answer -- and both markers exist
    only to be compared for equality against whatever is current, so collapsing
    them decides a staleness question wrongly rather than merely losing detail.
    The empty string is reachable: an imported shard (``kb index --source
    <shard>.json``) carries whatever ``head_commit``/``parser_version`` the file
    holds, straight through to the ``repos`` row and to these markers.
    """
    if value is None:
        store.conn.execute("DELETE FROM vec_meta WHERE key=?", (key,))
    else:
        store.conn.execute(
            "INSERT OR REPLACE INTO vec_meta(key, value) VALUES(?, ?)", (key, value)
        )
    store.conn.commit()


def get_embedded_head(store, repo_id: str) -> str | None:
    """The head commit a repo was last embedded at, or None if never embedded.

    Returns the stored string verbatim, ``""`` included -- absence is a missing
    row (see :func:`_put_or_clear`), so this does not have to guess which of the
    two an empty value meant.
    """
    row = store.conn.execute(
        "SELECT value FROM vec_meta WHERE key=?", (f"head:{repo_id}",)
    ).fetchone()
    return row[0] if row else None


def set_embedded_head(store, repo_id: str, head: str | None) -> None:
    """Record the head commit a repo was just embedded at (for incremental embed).

    ``None`` -- no head could be established -- clears the marker rather than
    writing ``""``; see :func:`_put_or_clear` for why the two must stay distinct.
    """
    _put_or_clear(store, f"head:{repo_id}", head)


def get_embedded_parser_version(store, repo_id: str) -> str | None:
    """Which parser built the graph a repo's vectors were embedded from, or None
    when nothing was recorded.

    Its one caller (``cmds/embed.py``) compares this for equality against
    ``state.indexed_parser_version`` and skips the repo on a match. All four
    quadrants of that comparison are deliberate:

    * both known and equal -> skip; the vectors describe the graph on disk.
    * stored known, current known but different -> re-embed; the parser moved
      under an unchanged commit, so node ids and text moved with it.
    * exactly one side known -> re-embed; nothing establishes that they agree.
    * both unknown (None) -> **skip**. This is the case that looks wrong and is
      not. Unknown here means the shard itself carries no version, so a re-embed
      cannot record one either -- demanding a match would re-embed that repo on
      every single run, forever, instead of once. ``cmds/wiki.py`` chose the same
      answer for the same reason; see
      ``tests/kb/test_parser_staleness_reach.py::test_an_unstamped_shard_falls_back_to_the_commit_question``.

    So None is not "treated as stale" unconditionally -- it is treated as
    *unknown*, which only matches another unknown. ``""`` is a different answer
    from None (a recorded empty version) and compares as itself.
    """
    row = store.conn.execute(
        "SELECT value FROM vec_meta WHERE key=?", (f"parser:{repo_id}",)
    ).fetchone()
    return row[0] if row else None


def set_embedded_parser_version(store, repo_id: str, parser_version: str | None) -> None:
    """Record which parser built the graph these vectors came from.

    Stored beside the head rather than folded into it, so neither value has to be
    parsed back out of a composite string and an existing head stamp keeps its
    meaning. ``None`` clears the marker instead of writing ``""`` -- see
    :func:`_put_or_clear`.
    """
    _put_or_clear(store, f"parser:{repo_id}", parser_version)


def get_content_version(store) -> int:
    """The node->text mapping version the store's vectors were built with.

    0 means the store predates version tracking (name-only vectors)."""
    row = store.conn.execute(
        "SELECT value FROM vec_meta WHERE key='content_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def set_content_version(store, version: int) -> None:
    """Record the node->text mapping version after a full, clean embed pass."""
    store.conn.execute(
        "INSERT OR REPLACE INTO vec_meta(key, value) VALUES('content_version', ?)",
        (str(version),),
    )
    store.conn.commit()


def chunk_key(node_id: str, index: int) -> str:
    """The stored key for chunk ``index`` of ``node_id``.

    A document is one NODE and several VECTORS, and both backends key their vector row on
    `node_id` as a PRIMARY KEY -- sqlite-vec's virtual table cannot take a composite one --
    so the chunk index has to live inside the key.

    The separator is ASCII Unit Separator (0x1f), a control character. It cannot occur in a
    normalized node id (`ids.normalize_id` maps every non-word character to `_`) and cannot
    occur in a path on any real filesystem, so splitting on it is unambiguous. A printable
    separator like `#` would not be: a document id is a relative path, and a file may legally
    contain one.

    Index 0 of a single-chunk document still gets a key, deliberately. Making the first chunk
    indistinguishable from an unchunked vector would leave two shapes in the store meaning the
    same thing, and every reader would have to handle both forever.
    """
    return f"{node_id}{CHUNK_SEP}{int(index)}"


def base_node_id(key: str) -> str:
    """The node id a stored key belongs to. Unchunked keys pass through unchanged."""
    return key.split(CHUNK_SEP, 1)[0]


def _collapse_chunks(scored, k: int):
    """`[(key, score)]` -> `[(node_id, best score)]`, ranked, at most ``k``.

    A document's chunks compete with each other, and a caller asked for k NODES. Keeping the
    best-scoring chunk per node is what makes chunking invisible to every consumer: the four
    call sites of `search()` still receive node ids and never learn that chunks exist.

    Fewer than k results is possible. For the brute-force store that means what it says: every
    vector was scored, so there really are not k distinct nodes. For sqlite-vec it does not --
    that backend sees a window of `k * _CHUNK_OVERFETCH` rows, and a document chunky enough to
    fill the window can hide distinct nodes that sit just past its edge. Widening the window is
    the lever if that is ever observed.
    """
    best: dict[str, float] = {}
    for key, score in scored:
        node_id = base_node_id(key)
        if score > best.get(node_id, float("-inf")):
            best[node_id] = score
    return sorted(best.items(), key=lambda t: t[1], reverse=True)[:k]


def build_vector_store(path: str | Path, *, backend: str = "auto", chunk_size: int = 1024):
    """Return a vector store. ``backend``: ``auto`` | ``sqlite-vec`` | ``brute``.

    ``auto`` uses sqlite-vec when it imports and loads, else the pure-Python store.
    ``sqlite-vec`` forces it (raising if unavailable); ``brute`` forces the fallback.
    ``chunk_size`` tunes the sqlite-vec vec0 KNN chunk size (ignored by the brute store).
    """
    if backend in ("sqlite-vec", "auto"):
        try:
            return SqliteVecStore(path, chunk_size=chunk_size)
        except Exception as e:  # noqa: BLE001 - any load failure falls back to brute
            if backend == "sqlite-vec":
                raise
            # auto: degrade to the pure-Python store, but say so -- an operator
            # otherwise has no idea search silently dropped to O(n) brute force.
            from ...logging_setup import log
            log(f"sqlite-vec unavailable ({e}); using slower brute-force vector "
                "search. Install the 'kb-vec' extra for ANN.", level=logging.WARNING)
    return VectorStore(path)


def unpopulated_reason(vector_store, repo: str | None = None) -> str | None:
    """Why a semantic search cannot answer, or ``None`` when it genuinely can.

    A nearest-neighbour index over an EMPTY table returns ``[]``. That is
    indistinguishable, at the call site, from a populated index that found nothing --
    and the two mean opposite things. The first means "this search never ran"; the
    second means "it ran and the graph holds nothing like your query". Reporting the
    first as the second is how `kb query --retriever semantic` came to print
    "No matches" on every freshly-initialised workspace, because `contextlake init`
    writes `[embeddings] enabled = true` while no vectors exist until `kb embed` runs.

    The repo-scoped count deliberately uses the SAME scope expansion `search` uses
    (``_repo_scope``: the repo's own shard plus its connector and enrichment
    partitions). Counting only the literal repo id would report "no vectors" for a
    repo whose vectors all live in a linked partition -- a false alarm produced by a
    count and a query disagreeing about what the filter means.

    Both cases are distinguished when a filter is in play, because "none here, some
    elsewhere" and "none anywhere" call for different fixes.

    Two existence probes rather than two counts. This runs before every semantic query,
    and the question it asks is "is there anything at all", which the first row answers.
    An exact `COUNT(*)` would buy a number for the message at the price of a scan the
    ANN backend cannot promise is cheap -- on precisely the large stores where a
    per-query cost is felt.
    """
    if not vector_store.has_any():
        return ("no vectors are stored, so semantic search has nothing to search "
                "-- run `contextlake kb embed` first")
    if repo and not vector_store.has_any(_repo_scope(repo)):
        return (f"no vectors are stored for repo {repo!r}, though other repositories "
                f"have them -- run `contextlake kb embed {repo}`")
    return None
