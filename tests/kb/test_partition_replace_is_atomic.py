"""A synthetic partition is replaced in one transaction, with its vectors cleared first.

`kb ingest`, `kb enrich` and `kb connect` each replaced a partition as separate commits: clear
the rows, upsert the nodes, upsert the edges. A failure part way left the partition empty or
half-written. Ingest and enrich also swept the vectors AFTER the rows, so a stop between the
two left vectors answering queries for documents the partition no longer held.

All three now go through `store_partition` (the transaction `reindex_shard` already gave a
code repo), and ingest and enrich clear vectors before anything else, then write the shard,
then the rows: the order `kb forget` and `kb index` use.
"""

from __future__ import annotations

import pytest

import contextlake.kb.cmds.ingest as ingest_cmd
import contextlake.kb.connectors.enrich as enrich
from contextlake.cli import main
from contextlake.kb import embeddings
from contextlake.kb.config import KbConfig, SourceCfg
from contextlake.kb.connectors.enrich import enrich_partition, run_enrich_repo
from contextlake.kb.embeddings import store as vector_store_mod
from contextlake.kb.model import Node
from contextlake.kb.sources.base import Document
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, store_partition, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

REPO = "group/app"


def _ids(db, part):
    store = SqliteStore(db)
    try:
        rows = store.conn.execute(
            "SELECT node_id FROM nodes WHERE repo_id = ? ORDER BY node_id", (part,)).fetchall()
        return [r[0] for r in rows]
    finally:
        store.close()


def _boom(self, repo_id, edges):
    raise RuntimeError("disk full")


# --- the helper, which `kb connect` calls ------------------------------------------------

def test_store_partition_rolls_back_when_a_write_fails(tmp_path, monkeypatch):
    db = tmp_path / "index.sqlite"
    store = SqliteStore(db)
    try:
        check_schema(store)
        store_partition(store, "@connect:x", [Node(id="old", repo="@connect:x", kind="mr",
                                                   name="old")], [])
        monkeypatch.setattr(SqliteStore, "upsert_edges", _boom)
        with pytest.raises(RuntimeError):
            store_partition(store, "@connect:x", [Node(id="new", repo="@connect:x",
                                                       kind="mr", name="new")], [])
    finally:
        store.close()
    assert _ids(db, "@connect:x") == ["old"]


# --- kb ingest ----------------------------------------------------------------------------

def _docs(tmp_path, names):
    d = tmp_path / "docs"
    d.mkdir(exist_ok=True)
    for f in d.iterdir():
        f.unlink()
    for n in names:
        (d / f"{n}.md").write_text(f"# {n}\n{n} body\n")
    return d


def _ingest(tmp_path, docs, embeddings_on=False):
    cfg = tmp_path / "kb.toml"
    body = f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n'
    if not embeddings_on:
        body += "[embeddings]\nenabled = false\n"
    cfg.write_text(body)
    with pytest.raises(SystemExit) as e:
        main(["kb", "ingest", "--path", str(docs), "--config", str(cfg)])
    return e.value.code


def test_a_failed_ingest_write_keeps_the_previous_partition(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _ingest(tmp_path, _docs(tmp_path, ["guide", "faq"])) == 0
    db = tmp_path / "kb" / "index.sqlite"
    before = _ids(db, "@ingest:cli")
    assert len(before) == 2

    monkeypatch.setattr(SqliteStore, "upsert_edges", _boom)
    assert _ingest(tmp_path, _docs(tmp_path, ["guide", "faq", "changelog"])) != 0

    # Three commits would have left the clear and the new nodes behind.
    assert _ids(db, "@ingest:cli") == before


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, float(i)] for i, _ in enumerate(texts)]


def test_ingest_clears_vectors_before_the_shard_and_the_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: _FakeEmbedder())

    real_build = vector_store_mod.build_vector_store

    def spying_build(*a, **k):
        vs = real_build(*a, **k)
        real_clear = vs.clear_repo
        vs.clear_repo = lambda part: (order.append("vectors"), real_clear(part))[1]
        return vs

    monkeypatch.setattr(vector_store_mod, "build_vector_store", spying_build)
    real_write = ingest_cmd.write_shard
    monkeypatch.setattr(ingest_cmd, "write_shard",
                        lambda *a, **k: (order.append("shard"), real_write(*a, **k))[1])
    real_rows = SqliteStore.clear_repo
    monkeypatch.setattr(SqliteStore, "clear_repo",
                        lambda self, part: (order.append("rows"), real_rows(self, part))[1])

    assert _ingest(tmp_path, _docs(tmp_path, ["guide", "faq"]), embeddings_on=True) == 0
    assert order[:3] == ["vectors", "shard", "rows"], order


# --- kb enrich ----------------------------------------------------------------------------

def _seed(store_dir):
    write_shard(store_dir, GraphShard(repo=REPO, head_commit="abc", nodes=[
        Node(id="n1", repo=REPO, kind="class", name="ForecastService", file="app/f.py")],
        edges=[]))
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    return store


def _cfg():
    return KbConfig(sources=[SourceCfg(type="atlassian", name="site-a")])


def _found(docs):
    return lambda src, terms, timeout=None: docs


def test_a_failed_enrich_write_keeps_the_previous_partition(tmp_path, monkeypatch):
    store_dir = tmp_path / "kb"
    store = _seed(store_dir)
    try:
        monkeypatch.setattr(enrich, "search_source", _found(
            [Document(id="d1", title="Runbook", text="page", uri="https://x/1")]))
        assert run_enrich_repo(store, store_dir, _cfg(), REPO).documents == 1
        part = enrich_partition(REPO)
        before = _ids(store_dir / "index.sqlite", part)
        assert len(before) == 1

        monkeypatch.setattr(enrich, "search_source", _found(
            [Document(id="d2", title="Design", text="notes", uri="https://x/2")]))
        monkeypatch.setattr(SqliteStore, "upsert_edges", _boom)
        with pytest.raises(RuntimeError):
            run_enrich_repo(store, store_dir, _cfg(), REPO)
    finally:
        store.close()
    assert _ids(store_dir / "index.sqlite", part) == before


class _SpyVectors:
    def __init__(self, order):
        self.order = order

    def clear_repo(self, part):
        self.order.append("vectors")


def test_enrich_clears_vectors_before_the_shard_and_the_rows(tmp_path, monkeypatch):
    store_dir = tmp_path / "kb"
    store = _seed(store_dir)
    order: list[str] = []
    try:
        monkeypatch.setattr(enrich, "search_source", _found(
            [Document(id="d1", title="Runbook", text="page", uri="https://x/1")]))
        real_write = enrich.write_shard
        monkeypatch.setattr(enrich, "write_shard",
                            lambda *a, **k: (order.append("shard"), real_write(*a, **k))[1])
        real_rows = SqliteStore.clear_repo
        monkeypatch.setattr(SqliteStore, "clear_repo",
                            lambda self, part: (order.append("rows"), real_rows(self, part))[1])
        run_enrich_repo(store, store_dir, _cfg(), REPO, vector_store=_SpyVectors(order))
    finally:
        store.close()
    assert order == ["vectors", "shard", "rows"], order


# --- kb wiki ------------------------------------------------------------------------------

def test_a_wiki_page_replace_that_fails_keeps_the_old_partition(tmp_path, monkeypatch):
    """`kb wiki --force` replaced a page's `@wiki` partition in three commits: a reader saw
    it at 0 rows, then nodes with no edges, and an interrupt between them left a module
    page's partition gone until the next run."""
    from contextlake.kb.cmds.wiki import _store_wiki_partition

    db = tmp_path / "index.sqlite"
    store = SqliteStore(db)
    try:
        check_schema(store)
        page_v1 = "# app\n\n## Overview\nThe first version.\n\n## Usage\nRun it.\n"
        _store_wiki_partition(store, tmp_path, REPO, page_v1, "app.md", "h1")
        before = _ids(db, f"@wiki:{REPO}")
        assert before, "setup: the first page wrote no sections"
        monkeypatch.setattr(SqliteStore, "upsert_edges", _boom)
        page_v2 = "# app\n\n## Overview\nThe second version.\n"
        with pytest.raises(RuntimeError):
            _store_wiki_partition(store, tmp_path, REPO, page_v2, "app.md", "h2")
    finally:
        store.close()
    assert _ids(db, f"@wiki:{REPO}") == before
