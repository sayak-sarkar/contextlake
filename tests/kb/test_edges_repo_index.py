"""`repo_counts` counts edges by `repo_id`, so `edges(repo_id)` needs an index.

`list_repos` calls `repo_counts` once per repository. With no index each call was a full
`SCAN edges`, so the orientation tool cost repos x edges (measured by the audit: 2.9 s over 200
repos and 600k edges, 0.012 s with the index). The schema is applied on every connection
open, so an existing store gains the index on its next open with no migration step.
"""

import sqlite3

from contextlake.kb.store.sqlite_store import SqliteStore


def _plan(db, repo="r"):
    c = sqlite3.connect(str(db))
    try:
        rows = c.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM edges WHERE repo_id=?", (repo,)).fetchall()
    finally:
        c.close()
    return " | ".join(r[3] for r in rows)


def test_a_new_store_counts_edges_through_an_index(tmp_path):
    db = tmp_path / "k.sqlite"
    SqliteStore(db).close()
    plan = _plan(db)
    assert "ix_edges_repo" in plan, plan
    assert "SCAN" not in plan, plan


def test_an_existing_store_without_the_index_gains_it_on_open(tmp_path):
    db = tmp_path / "k.sqlite"
    SqliteStore(db).close()
    c = sqlite3.connect(str(db))
    c.execute("DROP INDEX ix_edges_repo")
    c.commit()
    c.close()
    # Precondition: this is the state of every store written before the index existed.
    assert "SCAN" in _plan(db), _plan(db)
    SqliteStore(db).close()
    plan = _plan(db)
    assert "ix_edges_repo" in plan and "SCAN" not in plan, plan
