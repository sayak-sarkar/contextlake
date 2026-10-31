"""Rewriting a wiki page must not leave the old page's vectors behind.

`_store_wiki_partition` clears the graph partition and re-embeds the new sections, but
until this test it never swept the partition's VECTORS. A document is one node and
one-or-more vectors, keyed by `chunk_key(node.id, i)`, and the write is
`INSERT OR REPLACE` on that key -- so a key the new page does not produce is never
touched. Section nodes are keyed by INDEX (`@wiki:<repo>:<i>`), so a page that loses
its last section leaves that index's vectors in the store with no node behind them,
answering semantic searches about text the page no longer contains.

`ingest.py` describes this failure in its own comment and sweeps for it. `wiki.py`
imported `_embed_documents` from that module without the sweep that has to accompany it.

Asserted against the vector store directly rather than through `search()`. `search()`
collapses chunk keys back to node ids and the read paths join against nodes, so a
retrieval-level assertion can pass because the NODE is gone while the orphaned vector
is still sitting there -- which is the state this test exists to forbid.
"""

from __future__ import annotations

import pytest

import contextlake.kb.cmds.wiki as wiki_cmd
from contextlake.cli import main
from contextlake.kb import embeddings
from contextlake.kb.cmds.wiki import _store_wiki_partition, _wiki_partition, _wiki_section_nodes
from contextlake.kb.embeddings.store import build_vector_store
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        # One distinct unit vector per text. The values do not matter: every assertion
        # here counts rows, none of them ranks.
        return [[1.0, float(i)] for i, _ in enumerate(texts)]


_THREE_SECTIONS = """# r

## Alpha

The alpha section.

## Bravo

The bravo section.

## Charlie

The charlie section, which is about to be deleted.
"""

_TWO_SECTIONS = """# r

## Alpha

The alpha section.

## Bravo

The bravo section.
"""


def _write(store, store_dir, page):
    """Store and embed `page` as repo `r`'s wiki, the way `cmd_wiki` does."""
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        _store_wiki_partition(store, store_dir, "r", page, "r.md", "head1",
                              embedder=_FakeEmbedder(), vs=vs)
    finally:
        vs.close()


def _vector_count(store_dir, partition):
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo(partition)
    finally:
        vs.close()


def test_a_section_removed_from_a_page_takes_its_vectors_with_it(tmp_path):
    """FAILS before the fix: the count stays at the OLD section count.

    Section ids are per-index, so shrinking the page from N sections to N-1 strands
    index N-1. Nothing rewrites that key, so `INSERT OR REPLACE` never reaches it.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    store = SqliteStore(store_dir / "index.sqlite")
    part = _wiki_partition("r")
    try:
        _write(store, store_dir, _THREE_SECTIONS)
        before, _ = _wiki_section_nodes("r", _THREE_SECTIONS, "r.md")
        assert _vector_count(store_dir, part) == len(before), (
            "the fixture never embedded one vector per section, so the assertion "
            "below could not tell a swept store from an unswept one")

        _write(store, store_dir, _TWO_SECTIONS)
        after, _ = _wiki_section_nodes("r", _TWO_SECTIONS, "r.md")
        # The case that matters: strictly fewer sections than the first write.
        assert len(after) < len(before)
        assert _vector_count(store_dir, part) == len(after), (
            "the removed section's vectors outlived its node: semantic search can "
            "still return text the page no longer contains")
    finally:
        store.close()


def test_a_page_emptied_of_content_leaves_no_vectors_behind(tmp_path):
    """FAILS before the fix, and this is the worst case.

    An empty page produces no nodes, so `_store_wiki_partition` returns early right
    after clearing the graph partition. Every vector the previous page wrote is
    stranded, not only the tail.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    store = SqliteStore(store_dir / "index.sqlite")
    part = _wiki_partition("r")
    try:
        _write(store, store_dir, _THREE_SECTIONS)
        assert _vector_count(store_dir, part) > 0

        _write(store, store_dir, "")
        assert _wiki_section_nodes("r", "", "r.md")[0] == [], (
            "the fixture is not exercising the early-return path")
        assert _vector_count(store_dir, part) == 0, (
            "an emptied page stranded every vector it had ever written")
    finally:
        store.close()


def test_pruning_a_module_page_clears_its_vectors_before_its_nodes(tmp_path):
    """The prune path has no rewrite after it, so the clear ORDER is load-bearing.

    Cleared nodes-first, an interruption between the two leaves that module's vectors
    with no nodes: permanent, and they still answer semantic searches about a module
    that no longer qualifies. Cleared vectors-first it leaves nodes with no vectors, and
    the next run prunes the partition again. Same reasoning as `forget.py`'s clear order.

    Asserted by recording the call order, because the failure this guards is an
    interruption BETWEEN the two calls and no completed run can show it.
    """
    from contextlake.kb.cmds.wiki import _prune_orphan_module_pages, _store_wiki_partition

    store_dir = tmp_path / "kb"
    wiki_dir = store_dir / "wiki"
    wiki_dir.mkdir(parents=True)
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        vs = build_vector_store(store_dir / "embeddings.sqlite")
        try:
            # A module page that is about to stop qualifying.
            _store_wiki_partition(store, store_dir, "r::src/gone", _TWO_SECTIONS,
                                  "r__src__gone.md", "head1",
                                  embedder=_FakeEmbedder(), vs=vs, source_repo="r")
            part = _wiki_partition("r::src/gone")
            assert vs.count_repo(part) > 0, "fixture stored no vectors to prune"

            calls = []
            real_store_clear, real_vs_clear = store.clear_repo, vs.clear_repo
            store.clear_repo = lambda p: (calls.append(("nodes", p)), real_store_clear(p))[1]
            vs.clear_repo = lambda p: (calls.append(("vectors", p)), real_vs_clear(p))[1]

            # `modules` empty: nothing qualifies any more, so the page is an orphan.
            assert _prune_orphan_module_pages(store, store_dir, wiki_dir, "r", [], vs=vs) == 1

            ours = [kind for kind, p in calls if p == part]
            assert ours == ["vectors", "nodes"], (
                f"prune cleared in the order {ours}; nodes-first strands vectors "
                "permanently, because nothing rewrites either side afterwards")
            assert vs.count_repo(part) == 0
        finally:
            vs.close()
    finally:
        store.close()


# --- a changed embedder is refused before `kb wiki` clears a partition's vectors -----

class _ModelEmbedder:
    """Two instances differ only in `identity` and `dim`, as two models do."""

    def __init__(self, identity, dim=2, *, flaky_first=False, down=False):
        self.identity = self.name = identity
        self.dim = dim
        self._flaky_first = flaky_first
        self._down = down
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self._down:
            raise ConnectionError("embedder is down")
        if self._flaky_first and self.calls == 1:
            raise ConnectionError("first call timed out")
        return [[1.0] + [float(i)] * (self.dim - 1) for i, _ in enumerate(texts)]


def _wiki_fixture(tmp_path):
    """An indexed repo `r` and a config with embeddings on and no LLM. Returns the store dir
    and the config path."""
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n\n'
                   '[embeddings]\nenabled = true\nprovider = "ollama"\n', encoding="utf-8")
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    store.close()
    write_shard(store_dir, GraphShard(repo="r", head_commit="abc123", nodes=[
        Node(id="svc", repo="r", kind="class", name="ForecastService", file="svc.py"),
        Node(id="main", repo="r", kind="entry_point", name="main", file="cmd/main.go"),
    ], edges=[]))
    return store_dir, cfg


def _run_wiki(monkeypatch, cfg, embedder, *extra):
    monkeypatch.setattr(embeddings, "build_embedder", lambda _cfg: embedder)
    with pytest.raises(SystemExit) as e:
        main(["kb", "wiki", *extra, "--config", str(cfg)])
    return e.value.code


@pytest.mark.parametrize("dim_b", [2, 3], ids=["same-dimension", "new-dimension"])
def test_a_changed_embedder_is_refused_before_wiki_clears_any_vector(
        tmp_path, monkeypatch, capsys, dim_b):
    """`_store_wiki_partition` cleared the page's vectors and then embedded, so a changed
    `[embeddings]` model deleted them before the store's identity guard refused the run.
    A probe vector now goes through the guard before the first page is stored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, cfg = _wiki_fixture(tmp_path)
    part = _wiki_partition("r")
    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a")) == 0
    seeded = _vector_count(store_dir, part)
    assert seeded > 0, "the fixture embedded nothing, so the refusal below proves nothing"
    capsys.readouterr()

    stored = []
    real_store = wiki_cmd._store_wiki_partition
    monkeypatch.setattr(wiki_cmd, "_store_wiki_partition",
                        lambda *a, **k: (stored.append(a[2]), real_store(*a, **k))[1])
    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-b", dim_b)) == 1

    out = capsys.readouterr().out
    assert out.strip(), "the capture saw nothing, so the assertions below prove nothing"
    assert _vector_count(store_dir, part) == seeded, "the refused run cleared the vectors"
    assert stored == [], "a page was stored on a run the guard refused"
    assert "this embedding store was built with" in out
    assert "This run changed nothing" in out


def test_wiki_embeds_nothing_for_a_repo_id_that_is_not_indexed(tmp_path, monkeypatch, capsys):
    """The probe sits after the repo filter, so a mistyped id exits 1 without an embed call."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, cfg = _wiki_fixture(tmp_path)
    embedder = _ModelEmbedder("openai:fake-a")

    assert _run_wiki(monkeypatch, cfg, embedder, "typo-id") == 1

    assert embedder.calls == 0
    assert "No indexed repo matches typo-id" in capsys.readouterr().out


def test_wiki_goes_on_when_the_probe_got_no_answer(tmp_path, monkeypatch, capsys):
    """An embedder that timed out on the probe is not a refusal: the guard did not run, and
    the embed phase reports an embedder that stays down. Today's behavior stays."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, cfg = _wiki_fixture(tmp_path)

    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a", flaky_first=True)) == 0

    assert _vector_count(store_dir, _wiki_partition("r")) > 0
    assert "This run changed nothing" not in capsys.readouterr().out


def test_wiki_keeps_the_vectors_when_the_embedder_never_answers(tmp_path, monkeypatch, capsys):
    """The probe got no answer, so the guard had not run when `_store_wiki_partition` cleared
    the page's vectors. An embedder that stayed down for the batch too left the partition
    with 0 vectors."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, cfg = _wiki_fixture(tmp_path)
    part = _wiki_partition("r")
    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a")) == 0
    seeded = _vector_count(store_dir, part)
    assert seeded > 0, "setup: the first run stored no vectors"
    capsys.readouterr()

    _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a", down=True))

    assert "embed: stopped after 0 of" in capsys.readouterr().out, (
        "the embed phase never failed, so the count below proves nothing")
    assert _vector_count(store_dir, part) == seeded


def test_wiki_clears_the_old_vectors_once_the_first_batch_after_an_unanswered_probe_passes(
        tmp_path, monkeypatch):
    """The clear waits for the first batch, it is not dropped: a vector the new page does not
    write goes when the first batch of the run holds a vector."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, cfg = _wiki_fixture(tmp_path)
    part = _wiki_partition("r")
    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a")) == 0
    vs = build_vector_store(store_dir / "embeddings.sqlite")
    try:
        vs.upsert([("stale-key", part, [1.0, 0.0])])
        seeded = vs.count_repo(part)
    finally:
        vs.close()

    assert _run_wiki(monkeypatch, cfg, _ModelEmbedder("openai:fake-a", flaky_first=True)) == 0

    assert _vector_count(store_dir, part) == seeded - 1


@pytest.mark.parametrize(("flaky_first", "checked"), [(False, True), (True, False)],
                         ids=["probe-answers", "probe-gets-no-answer"])
def test_wiki_tells_each_page_store_whether_the_probe_passed_the_guard(
        tmp_path, monkeypatch, flaky_first, checked):
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_dir, cfg = _wiki_fixture(tmp_path)
    seen = []
    real_store = wiki_cmd._store_wiki_partition
    monkeypatch.setattr(wiki_cmd, "_store_wiki_partition",
                        lambda *a, **k: (seen.append(k["embedder_checked"]),
                                         real_store(*a, **k))[1])

    assert _run_wiki(monkeypatch, cfg,
                     _ModelEmbedder("openai:fake-a", flaky_first=flaky_first)) == 0

    assert seen and set(seen) == {checked}
