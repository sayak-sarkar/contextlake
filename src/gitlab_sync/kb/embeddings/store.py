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
