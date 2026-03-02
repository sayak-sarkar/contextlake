"""A minimal local vector store: SQLite-backed, pure-Python cosine search.

Vectors are persisted as packed float32 blobs alongside a precomputed norm;
search is brute-force cosine. This is dependency-free and correct — good enough to
ship the semantic tier. A native ANN index (e.g. sqlite-vec) can replace the
search path later behind this same interface, without touching callers.
"""

from __future__ import annotations

import array
import math
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def _pack(vec) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def _norm(vec) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


class VectorStore:
    """Persisted node embeddings with cosine top-k search."""

    name = "brute"

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

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

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def search(self, query, k: int = 10, repo: str | None = None) -> list[tuple[str, float]]:
        """Return the ``k`` nearest node_ids to ``query`` by cosine, high score first."""
        qnorm = _norm(query)
        qlen = len(query)
        sql = "SELECT node_id, dim, norm, vec FROM embeddings"
        params: tuple = ()
        if repo:
            sql += " WHERE repo_id=?"
            params = (repo,)
        scored: list[tuple[str, float]] = []
        for node_id, dim, norm, blob in self.conn.execute(sql, params):
            if dim != qlen:
                continue
            dot = sum(a * b for a, b in zip(query, _unpack(blob)))
            scored.append((node_id, dot / (qnorm * norm)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

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

    def __init__(self, path: str | Path):
        import sqlite_vec  # optional dependency; ImportError -> factory fallback

        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.commit()
        row = self.conn.execute("SELECT value FROM vec_meta WHERE key='dim'").fetchone()
        self._dim = int(row[0]) if row else None

    def _ensure_table(self, dim: int) -> None:
        if self._dim is not None:
            return
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
            "node_id TEXT PRIMARY KEY, repo_id TEXT, "
            f"embedding FLOAT[{dim}] distance_metric=cosine)"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_meta(key, value) VALUES('dim', ?)", (str(dim),)
        )
        self.conn.commit()
        self._dim = dim

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
        if self._dim is None:
            return
        self.conn.execute("DELETE FROM vec_items WHERE repo_id=?", (repo_id,))
        self.conn.commit()

    def count(self) -> int:
        if self._dim is None:
            return 0
        return self.conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0]

    def search(self, query, k: int = 10, repo: str | None = None) -> list[tuple[str, float]]:
        if self._dim is None or len(query) != self._dim:
            return []
        q = _pack(query)
        if repo:
            sql = ("SELECT node_id, distance FROM vec_items "
                   "WHERE embedding MATCH ? AND repo_id = ? ORDER BY distance LIMIT ?")
            params: tuple = (q, repo, k)
        else:
            sql = ("SELECT node_id, distance FROM vec_items "
                   "WHERE embedding MATCH ? ORDER BY distance LIMIT ?")
            params = (q, k)
        # vec0 returns cosine distance; convert to similarity to match VectorStore.
        return [(node_id, 1.0 - dist) for node_id, dist in self.conn.execute(sql, params)]

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


def build_vector_store(path: str | Path, *, backend: str = "auto"):
    """Return a vector store. ``backend``: ``auto`` | ``sqlite-vec`` | ``brute``.

    ``auto`` uses sqlite-vec when it imports and loads, else the pure-Python store.
    ``sqlite-vec`` forces it (raising if unavailable); ``brute`` forces the fallback.
    """
    if backend in ("sqlite-vec", "auto"):
        try:
            return SqliteVecStore(path)
        except Exception:
            if backend == "sqlite-vec":
                raise
    return VectorStore(path)
