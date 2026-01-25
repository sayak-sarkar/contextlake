"""SQLite cross-repo index — a rebuildable index over per-repo graph shards.

Nodes/edges from every repo's shard are denormalized here for fast cross-repo
queries and FTS5 symbol search. The shards remain the source of truth; this DB
can be dropped and rebuilt at any time.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from ..model import Confidence, Edge, Node, Provenance, Repo
from .base import Stats, Store

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY, path TEXT, host TEXT, default_branch TEXT,
    head_commit TEXT, indexed_at TEXT, lang_stats TEXT);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY, repo_id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
    file TEXT, line_start INTEGER, line_end INTEGER, lang TEXT, attrs TEXT);
CREATE TABLE IF NOT EXISTS edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT, src TEXT, dst TEXT,
    relation TEXT, confidence TEXT, context TEXT, source_file TEXT, source_line INTEGER,
    verified_at TEXT, weight REAL, cross_repo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS external (
    ext_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, source_type TEXT,
    external_key TEXT, repo_id TEXT, relation TEXT, title TEXT, url TEXT,
    fetched_at TEXT, payload_ref TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    node_id UNINDEXED, name, qualified_name, file);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS ix_edges_cross ON edges(cross_repo);
CREATE INDEX IF NOT EXISTS ix_nodes_repo ON nodes(repo_id);
CREATE INDEX IF NOT EXISTS ix_nodes_kind ON nodes(kind);
"""


def _fts_query(text: str) -> str:
    """Build a safe FTS5 prefix query from arbitrary user text."""
    tokens = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE).split()
    return " ".join(f"{t}*" for t in tokens)


class SqliteStore(Store):
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # -- meta -----------------------------------------------------------------
    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kb_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # -- repos ----------------------------------------------------------------
    def upsert_repo(self, repo: Repo) -> None:
        self.conn.execute(
            "INSERT INTO repos(repo_id, path, host, default_branch, head_commit) "
            "VALUES(?,?,?,?,?) ON CONFLICT(repo_id) DO UPDATE SET "
            "path=excluded.path, host=excluded.host, "
            "default_branch=excluded.default_branch, head_commit=excluded.head_commit",
            (repo.id, repo.path, repo.host, repo.default_branch, repo.head_commit),
        )
        self.conn.commit()

    def get_repo(self, repo_id: str) -> Repo | None:
        row = self.conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
        if not row:
            return None
        return Repo(
            id=row["repo_id"], path=row["path"], host=row["host"],
            default_branch=row["default_branch"], head_commit=row["head_commit"],
        )

    def mark_indexed(self, repo_id: str, head_commit: str | None, indexed_at: str) -> None:
        self.conn.execute(
            "UPDATE repos SET head_commit=?, indexed_at=? WHERE repo_id=?",
            (head_commit, indexed_at, repo_id),
        )
        self.conn.commit()

    def list_repos(self) -> list[Repo]:
        rows = self.conn.execute("SELECT * FROM repos ORDER BY repo_id").fetchall()
        return [
            Repo(id=r["repo_id"], path=r["path"], host=r["host"],
                 default_branch=r["default_branch"], head_commit=r["head_commit"])
            for r in rows
        ]

    # -- nodes ----------------------------------------------------------------
    def upsert_nodes(self, repo_id: str, nodes: Iterable[Node]) -> None:
        cur = self.conn.cursor()
        for n in nodes:
            cur.execute(
                "INSERT INTO nodes(node_id, repo_id, kind, name, qualified_name, file, "
                "line_start, line_end, lang, attrs) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET repo_id=excluded.repo_id, "
                "kind=excluded.kind, name=excluded.name, qualified_name=excluded.qualified_name, "
                "file=excluded.file, line_start=excluded.line_start, line_end=excluded.line_end, "
                "lang=excluded.lang, attrs=excluded.attrs",
                (n.id, repo_id, n.kind, n.name, n.qualified_name, n.file,
                 n.line_start, n.line_end, n.lang, json.dumps(n.attrs)),
            )
            cur.execute("DELETE FROM node_fts WHERE node_id=?", (n.id,))
            cur.execute(
                "INSERT INTO node_fts(node_id, name, qualified_name, file) VALUES(?,?,?,?)",
                (n.id, n.name, n.qualified_name or "", n.file or ""),
            )
        self.conn.commit()

    def get_node(self, node_id: str) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> Node:
        return Node(
            id=row["node_id"], repo=row["repo_id"], kind=row["kind"], name=row["name"],
            qualified_name=row["qualified_name"], file=row["file"],
            line_start=row["line_start"], line_end=row["line_end"], lang=row["lang"],
            attrs=json.loads(row["attrs"]) if row["attrs"] else {},
        )

    def search(
        self, query: str, kind: str | None = None, repo: str | None = None, limit: int = 20
    ) -> list[Node]:
        fts = _fts_query(query)
        if not fts:
            return []
        sql = (
            "SELECT n.* FROM node_fts f JOIN nodes n ON n.node_id = f.node_id "
            "WHERE node_fts MATCH ?"
        )
        params: list[object] = [fts]
        if kind:
            sql += " AND n.kind = ?"
            params.append(kind)
        if repo:
            sql += " AND n.repo_id = ?"
            params.append(repo)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    # -- edges ----------------------------------------------------------------
    def _repo_of(self, node_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT repo_id FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return row["repo_id"] if row else None

    def upsert_edges(self, repo_id: str, edges: Iterable[Edge]) -> None:
        cur = self.conn.cursor()
        for e in edges:
            src_repo, dst_repo = self._repo_of(e.src), self._repo_of(e.dst)
            cross = int(bool(src_repo and dst_repo and src_repo != dst_repo))
            cur.execute(
                "INSERT INTO edges(repo_id, src, dst, relation, confidence, context, "
                "source_file, source_line, verified_at, weight, cross_repo) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (repo_id, e.src, e.dst, e.relation, e.confidence.value, e.context,
                 e.provenance.source_file, e.provenance.source_line,
                 e.provenance.verified_at.isoformat(), e.weight, cross),
            )
        self.conn.commit()

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        return Edge(
            src=row["src"], dst=row["dst"], relation=row["relation"],
            confidence=Confidence(row["confidence"]), context=row["context"],
            weight=row["weight"],
            provenance=Provenance(
                source_file=row["source_file"], source_line=row["source_line"],
                verified_at=date.fromisoformat(row["verified_at"]),
            ),
        )

    def neighbors(
        self, node_id: str, relation: str | None = None, direction: str = "both"
    ) -> list[Edge]:
        clauses = []
        if direction in ("out", "both"):
            clauses.append("src = ?")
        if direction in ("in", "both"):
            clauses.append("dst = ?")
        if not clauses:
            raise ValueError(f"invalid direction: {direction!r}")
        sql = "SELECT * FROM edges WHERE (" + " OR ".join(clauses) + ")"
        params: list[object] = [node_id] * len(clauses)
        if relation:
            sql += " AND relation = ?"
            params.append(relation)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_edge(r) for r in rows]

    # -- maintenance ----------------------------------------------------------
    def clear_repo(self, repo_id: str) -> None:
        self.conn.execute(
            "DELETE FROM node_fts WHERE node_id IN (SELECT node_id FROM nodes WHERE repo_id=?)",
            (repo_id,),
        )
        self.conn.execute("DELETE FROM nodes WHERE repo_id=?", (repo_id,))
        self.conn.execute("DELETE FROM edges WHERE repo_id=?", (repo_id,))
        self.conn.commit()

    def stats(self) -> Stats:
        repos = self.conn.execute("SELECT COUNT(*) c FROM repos").fetchone()["c"]
        nodes = self.conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        edges = self.conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        by_conf = {
            r["confidence"]: r["c"]
            for r in self.conn.execute(
                "SELECT confidence, COUNT(*) c FROM edges GROUP BY confidence"
            ).fetchall()
        }
        return Stats(repos=repos, nodes=nodes, edges=edges, by_confidence=by_conf)
