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

from contextlake.kb.cmds.wiki import _store_wiki_partition, _wiki_partition, _wiki_section_nodes
from contextlake.kb.embeddings.store import build_vector_store
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
    that no longer qualifies. Cleared vectors-first it leaves nodes with no vectors,
    which `kb embed` rebuilds. Same reasoning as `forget.py`'s clear order.

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
