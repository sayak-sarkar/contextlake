"""A synthetic partition is replaced in one transaction, with its vectors cleared first.

`kb ingest`, `kb enrich` and `kb connect` each replaced a partition as separate commits: clear
the rows, upsert the nodes, upsert the edges. A failure part way left the partition empty or
half-written. Ingest and enrich also swept the vectors AFTER the rows, so a stop between the
two left vectors answering queries for documents the partition no longer held.

All three now go through `store_partition` (the transaction `reindex_shard` already gave a
code repo), and ingest and enrich clear vectors before anything else, then write the shard,
then the rows: the order `kb forget` and `kb index` use. That holds once the embedder has passed the
identity check. After a probe that got no answer they write the shard and the rows first and clear
the vectors when the first batch holds a vector.
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


def _folder(tmp_path, name, files):
    d = tmp_path / name
    d.mkdir()
    for f in files:
        (d / f).write_text(f"# {f}\n{f} body\n")
    return d


def test_a_path_run_on_another_folder_names_the_documents_it_replaced(
        tmp_path, monkeypatch, capsys):
    """Every `--path` run writes the one `@ingest:cli` partition. A run on a second folder
    removed the first folder's documents and printed a clean `cli: 1 document(s)`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    first = _folder(tmp_path, "docs", ["a1.md", "a2.md"])
    second = _folder(tmp_path, "notes", ["b1.md"])

    assert _ingest(tmp_path, first) == 0
    assert "replaced" not in capsys.readouterr().out, "a first run replaces nothing"

    assert _ingest(tmp_path, second) == 0
    out = capsys.readouterr().out
    db = tmp_path / "kb" / "index.sqlite"
    assert _ids(db, "@ingest:cli") == ["@ingest:cli:b1.md"]
    assert "cli: replaced 2 document(s) the last run stored and this run did not read" in out
    assert "a1.md" in out and "a2.md" in out
    assert "contextlake kb source add NAME --type files --set path=PATH" in out

    assert _ingest(tmp_path, second) == 0
    assert "replaced" not in capsys.readouterr().out, "re-reading the same folder is quiet"


def test_the_replaced_notice_suggests_a_source_of_the_type_that_was_run():
    """The hint is built from the run's own type and target option. A web or api run reads
    `url`, so `--type files --set path=` would have named a source that cannot work."""
    from contextlake.kb.cmds.ingest import _own_source_hint

    assert _own_source_hint("files", "./notes") == (
        "contextlake kb source add NAME --type files --set path=PATH")
    assert _own_source_hint("web", "https://example.invalid/a") == (
        "contextlake kb source add NAME --type web --set url=URL")
    assert _own_source_hint("api", "https://example.invalid/items") == (
        "contextlake kb source add NAME --type api --set url=URL")


def test_a_path_run_whose_write_fails_does_not_claim_it_replaced_anything(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _ingest(tmp_path, _folder(tmp_path, "docs", ["a1.md", "a2.md"])) == 0
    capsys.readouterr()

    monkeypatch.setattr(SqliteStore, "upsert_edges", _boom)
    assert _ingest(tmp_path, _folder(tmp_path, "notes", ["b1.md"])) != 0

    out = capsys.readouterr().out
    assert out.strip(), "the capture must see something or this proves nothing"
    assert "replaced" not in out
    assert _ids(tmp_path / "kb" / "index.sqlite", "@ingest:cli") == [
        "@ingest:cli:a1.md", "@ingest:cli:a2.md"]


def test_a_path_run_that_missed_a_target_keeps_documents_and_does_not_say_replaced(
        tmp_path, monkeypatch, capsys):
    """When a target could not be read the earlier documents are kept, not replaced."""
    import contextlake.kb.sources as sources_pkg

    monkeypatch.setenv("HOME", str(tmp_path))
    assert _ingest(tmp_path, _folder(tmp_path, "docs", ["a1.md", "a2.md"])) == 0
    capsys.readouterr()

    class _PartialSource:
        failures = [("page-2", "HTTP 500")]

        def iter_documents(self):
            yield Document(id="a1.md", title="A1", text="fresh body")

    monkeypatch.setattr(sources_pkg, "build_source", lambda *a, **k: _PartialSource())
    assert _ingest(tmp_path, tmp_path / "docs") == 1

    out = capsys.readouterr().out
    assert _ids(tmp_path / "kb" / "index.sqlite", "@ingest:cli") == [
        "@ingest:cli:a1.md", "@ingest:cli:a2.md"]
    assert "kept 1 from the last run" in out
    assert "replaced" not in out


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
    return lambda src, terms, **_kw: docs


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


def _enrich_with_embedder(tmp_path, monkeypatch, runs):
    """Run ``run_enrich_repo`` once per ``(embedder_checked, documents)`` in ``runs``, with a
    working embedder and a real vector store. Returns the order in which the last run cleared
    vectors and rows and wrote the shard, and the partition's vector count after it."""
    store_dir = tmp_path / "kb"
    store = _seed(store_dir)
    order: list[str] = []
    vs = vector_store_mod.build_vector_store(store_dir / "embeddings.sqlite")
    try:
        real_clear = vs.clear_repo
        vs.clear_repo = lambda part: (order.append("vectors"), real_clear(part))[1]
        real_write = enrich.write_shard
        monkeypatch.setattr(enrich, "write_shard",
                            lambda *a, **k: (order.append("shard"), real_write(*a, **k))[1])
        real_rows = SqliteStore.clear_repo
        monkeypatch.setattr(SqliteStore, "clear_repo",
                            lambda self, part: (order.append("rows"), real_rows(self, part))[1])
        for checked, docs in runs:
            order.clear()
            monkeypatch.setattr(enrich, "search_source", _found(docs))
            run_enrich_repo(store, store_dir, _cfg(), REPO, embedder=_FakeEmbedder(),
                            vector_store=vs, embedder_checked=checked)
        return order, vs.count_repo(enrich_partition(REPO))
    finally:
        vs.close()
        store.close()


_ONE_DOC = [Document(id="d1", title="Runbook", text="page", uri="https://x/1")]


def test_enrich_with_a_checked_probe_clears_vectors_before_the_shard_and_the_rows(
        tmp_path, monkeypatch):
    order, vectors = _enrich_with_embedder(tmp_path, monkeypatch, [(True, _ONE_DOC)])
    assert order == ["vectors", "shard", "rows"], order
    assert vectors == 1


def test_enrich_with_no_probe_answer_clears_vectors_after_the_shard_and_the_rows(
        tmp_path, monkeypatch):
    """The clear is `_embed_documents`'s sweep, taken once the first batch holds a vector.
    Before it, an embedder that never answered left the partition with no vectors."""
    order, vectors = _enrich_with_embedder(tmp_path, monkeypatch, [(False, _ONE_DOC)])
    assert order == ["shard", "rows", "vectors"], order
    assert vectors == 1


def test_enrich_with_no_probe_answer_still_clears_up_front_when_nothing_is_embedded(
        tmp_path, monkeypatch):
    """A source that answers with no documents clears the partition: no batch runs, so there
    is no first batch to wait for."""
    order, vectors = _enrich_with_embedder(
        tmp_path, monkeypatch, [(True, _ONE_DOC), (False, [])])
    assert order == ["vectors", "shard", "rows"], order
    assert vectors == 0


# --- kb wiki ------------------------------------------------------------------------------

def _wiki_with_embedder(tmp_path, monkeypatch, *, checked):
    """Store a wiki page with a working embedder, over a page already stored, and return the
    order in which vectors, shard and rows were cleared or written."""
    import contextlake.kb.cmds.wiki as wiki_cmd
    from contextlake.kb.cmds.wiki import _store_wiki_partition

    db = tmp_path / "index.sqlite"
    store = SqliteStore(db)
    vs = vector_store_mod.build_vector_store(tmp_path / "embeddings.sqlite")
    order: list[str] = []
    try:
        check_schema(store)
        page = "# app\n\n## Overview\nThe first version.\n\n## Usage\nRun it.\n"
        _store_wiki_partition(store, tmp_path, REPO, page, "app.md", "h1",
                              embedder=_FakeEmbedder(), vs=vs, embedder_checked=True)
        real_clear = vs.clear_repo
        vs.clear_repo = lambda part: (order.append("vectors"), real_clear(part))[1]
        real_write = wiki_cmd.write_shard
        monkeypatch.setattr(wiki_cmd, "write_shard",
                            lambda *a, **k: (order.append("shard"), real_write(*a, **k))[1])
        real_rows = SqliteStore.clear_repo
        monkeypatch.setattr(SqliteStore, "clear_repo",
                            lambda self, part: (order.append("rows"), real_rows(self, part))[1])
        _store_wiki_partition(store, tmp_path, REPO, page, "app.md", "h2",
                              embedder=_FakeEmbedder(), vs=vs, embedder_checked=checked)
    finally:
        vs.close()
        store.close()
    return order


def test_wiki_with_a_checked_probe_clears_vectors_before_the_shard_and_the_rows(
        tmp_path, monkeypatch):
    order = _wiki_with_embedder(tmp_path, monkeypatch, checked=True)
    assert order == ["vectors", "shard", "rows"], order


def test_wiki_with_no_probe_answer_clears_vectors_after_the_shard_and_the_rows(
        tmp_path, monkeypatch):
    order = _wiki_with_embedder(tmp_path, monkeypatch, checked=False)
    assert order == ["shard", "rows", "vectors"], order


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
