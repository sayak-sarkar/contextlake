"""SQLite cross-repo index — a rebuildable index over per-repo graph shards.

Nodes/edges from every repo's shard are denormalized here for fast cross-repo
queries and FTS5 symbol search. The shards remain the source of truth; this DB
can be dropped and rebuilt at any time.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from ..model import Confidence, Edge, Node, Provenance, Repo
from .base import Stats, Store

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY, path TEXT, host TEXT, default_branch TEXT,
    head_commit TEXT, indexed_at TEXT, lang_stats TEXT, parser_version TEXT);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY, repo_id TEXT, kind TEXT, name TEXT, qualified_name TEXT,
    file TEXT, line_start INTEGER, line_end INTEGER, lang TEXT, attrs TEXT);
CREATE TABLE IF NOT EXISTS edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT, src TEXT, dst TEXT,
    relation TEXT, confidence TEXT, context TEXT, source_file TEXT, source_line INTEGER,
    verified_at TEXT, weight REAL, cross_repo INTEGER DEFAULT 0, attrs TEXT);
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
    """Build a safe FTS5 prefix query from arbitrary user text.

    Each token is double-quoted so FTS5 boolean keywords (AND/OR/NOT/NEAR) and
    other reserved syntax are treated as literal terms, not operators.
    """
    tokens = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE).split()
    return " ".join(f'"{t}"*' for t in tokens)


class SqliteStore(Store):
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        _ = self.conn  # eagerly open + migrate on the constructing thread

    @property
    def conn(self) -> sqlite3.Connection:
        """A connection scoped to the calling thread.

        A single shared sqlite3.Connection can't safely cross threads (raises
        "SQLite objects created in a thread can only be used in that same
        thread"), and `contextlake serve`'s MCP server dispatches every
        synchronous tool call to a worker-thread pool (`anyio.to_thread.run_sync`
        in the mcp SDK, unconditional, no opt-out) -- so a store that outlives
        a single request/call must hand out one connection per thread, not one
        connection total. WAL mode (set below) is exactly the mode SQLite
        recommends for this: concurrent connections to the same file, each
        used only by the thread that opened it.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._local.conn = conn
            self._migrate_additive_columns()
            self._set_meta("schema_version", str(SCHEMA_VERSION))
            conn.commit()
        return conn

    def _migrate_additive_columns(self) -> None:
        """Add columns introduced after a store already exists on disk.

        ``CREATE TABLE IF NOT EXISTS`` above is a no-op against an existing
        table -- it never widens one -- so a pre-v2.52 store opened by this
        build would otherwise fail the very first ``INSERT`` that names a
        column it doesn't have (``edges.attrs``, added for C1 system-context
        edges). ``ALTER TABLE ... ADD COLUMN`` is safe/cheap in SQLite for a
        nullable column: existing rows just read back NULL for it, and no
        existing row or column is rewritten, so an established store survives
        the migration intact.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(edges)")}
        if "attrs" not in cols:
            self.conn.execute("ALTER TABLE edges ADD COLUMN attrs TEXT")
        # repos.parser_version (v3): which parser built this repo's graph, so the
        # "does this need re-indexing?" decision can consider more than the repo's
        # HEAD. NULL means "indexed before this column existed" -- deliberately
        # distinguishable from any real version string, because callers answer it
        # by reading the shard rather than by guessing.
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(repos)")}
        if "parser_version" not in cols:
            self.conn.execute("ALTER TABLE repos ADD COLUMN parser_version TEXT")

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

    def mark_indexed(self, repo_id: str, head_commit: str | None, indexed_at: str,
                     parser_version: str | None = None) -> None:
        self.conn.execute(
            "UPDATE repos SET head_commit=?, indexed_at=?, parser_version=? WHERE repo_id=?",
            (head_commit, indexed_at, parser_version, repo_id),
        )
        self.conn.commit()

    def get_repo_parser_version(self, repo_id: str) -> str | None:
        """The parser version stamped on the repo's last index, or None when it
        carries no stamp (indexed before the column existed, or never indexed).

        Kept off :class:`~contextlake.kb.model.Repo` on purpose: ``upsert_repo``
        does not write this column (only ``mark_indexed`` does, atomically with
        the head it belongs to), so a field on the model would be writable-looking
        but silently read-only on that path.
        """
        row = self.conn.execute(
            "SELECT parser_version FROM repos WHERE repo_id=?", (repo_id,)
        ).fetchone()
        return row["parser_version"] if row else None

    def list_repos(self) -> list[Repo]:
        rows = self.conn.execute("SELECT * FROM repos ORDER BY repo_id").fetchall()
        return [
            Repo(id=r["repo_id"], path=r["path"], host=r["host"],
                 default_branch=r["default_branch"], head_commit=r["head_commit"])
            for r in rows
        ]

    # -- nodes ----------------------------------------------------------------
    def upsert_nodes(self, repo_id: str, nodes: Iterable[Node]) -> None:
        """Insert/update ``nodes`` from the ``repo_id`` shard's indexing pass.

        ``repo_id`` names which shard produced this batch; the ``repo_id``
        *column* is taken from each node's own ``.repo`` instead, not this
        parameter. They agree for ordinary per-repo nodes (every extractor sets
        ``repo=repo_id``), but a shared node (``module``/``endpoint``/``topic``/
        ``package``/connector nodes) carries a stable sentinel repo of its own
        (Finding #10) -- stamping it with whichever shard happened to produce
        this batch would make it flip repos on every reindex and let
        ``clear_repo`` on one repo delete a node another repo still uses.
        """
        nodes = list(nodes)
        if not nodes:
            return
        cur = self.conn.cursor()
        # Refresh the FTS rows for these nodes with ONE set-based delete per chunk,
        # not a DELETE per node. ``node_fts`` is an FTS5 table with no index on
        # node_id, so a per-row ``DELETE ... WHERE node_id=?`` scans the entire
        # (global, ever-growing) table each time -> O(nodes x store) per repo, which
        # is what made indexing the 600th repo take minutes. One IN-delete is a
        # single scan regardless of how many of this repo's nodes it removes.
        ids = [n.id for n in nodes]
        for i in range(0, len(ids), 900):  # stay under SQLite's bound-variable limit
            chunk = ids[i:i + 900]
            cur.execute(
                f"DELETE FROM node_fts WHERE node_id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        cur.executemany(
            "INSERT INTO nodes(node_id, repo_id, kind, name, qualified_name, file, "
            "line_start, line_end, lang, attrs) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET repo_id=excluded.repo_id, "
            "kind=excluded.kind, name=excluded.name, qualified_name=excluded.qualified_name, "
            "file=excluded.file, line_start=excluded.line_start, line_end=excluded.line_end, "
            "lang=excluded.lang, attrs=excluded.attrs",
            [(n.id, n.repo, n.kind, n.name, n.qualified_name, n.file,
              n.line_start, n.line_end, n.lang, json.dumps(n.attrs)) for n in nodes],
        )
        cur.executemany(
            "INSERT INTO node_fts(node_id, name, qualified_name, file) VALUES(?,?,?,?)",
            [(n.id, n.name, n.qualified_name or "", n.file or "") for n in nodes],
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
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            # Resilience: never let a search crash the caller. But distinguish an
            # expected malformed-FTS-query (quiet) from a real DB problem -- a locked
            # db / missing table / I/O error must not masquerade silently as "no hits".
            msg = str(e).lower()
            expected = "fts5" in msg or "syntax error" in msg or "malformed match" in msg
            from ...logging_setup import log
            log(f"search query failed ({e}); returning no results",
                level=logging.DEBUG if expected else logging.WARNING)
            return []
        return [self._row_to_node(r) for r in rows]

    def nodes_by_name(
        self, name: str, kind: str | None = None, repo: str | None = None
    ) -> list[Node]:
        sql = "SELECT * FROM nodes WHERE name = ?"
        params: list[object] = [name]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if repo:
            sql += " AND repo_id = ?"
            params.append(repo)
        return [self._row_to_node(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- edges ----------------------------------------------------------------
    def _repos_of(self, node_ids: set[str]) -> dict[str, str]:
        """repo_id for each given node, in batched queries (not one SELECT per node)."""
        out: dict[str, str] = {}
        ids = list(node_ids)
        for i in range(0, len(ids), 500):   # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                f"SELECT node_id, repo_id FROM nodes WHERE node_id IN ({marks})", chunk
            ):
                out[row["node_id"]] = row["repo_id"]
        return out

    def upsert_edges(self, repo_id: str, edges: Iterable[Edge]) -> None:
        edges = list(edges)
        if not edges:
            return
        # Resolve every endpoint's repo in a few batched queries, then insert in one
        # executemany. Previously this issued two SELECTs *per edge* plus an unbatched
        # INSERT, which dominated index time at fleet scale.
        repo_of = self._repos_of({e.src for e in edges} | {e.dst for e in edges})
        rows = []
        for e in edges:
            src_repo, dst_repo = repo_of.get(e.src), repo_of.get(e.dst)
            cross = int(bool(src_repo and dst_repo and src_repo != dst_repo))
            rows.append(
                (repo_id, e.src, e.dst, e.relation, e.confidence.value, e.context,
                 e.provenance.source_file, e.provenance.source_line,
                 e.provenance.verified_at.isoformat(), e.weight, cross,
                 json.dumps(e.attrs) if e.attrs else None)
            )
        self.conn.executemany(
            "INSERT INTO edges(repo_id, src, dst, relation, confidence, context, "
            "source_file, source_line, verified_at, weight, cross_repo, attrs) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        return Edge(
            src=row["src"], dst=row["dst"], relation=row["relation"],
            confidence=Confidence(row["confidence"]), context=row["context"],
            weight=row["weight"], attrs=json.loads(row["attrs"]) if row["attrs"] else {},
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

    def delete_repo(self, repo_id: str) -> None:
        self.clear_repo(repo_id)
        self.conn.execute("DELETE FROM repos WHERE repo_id=?", (repo_id,))
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
