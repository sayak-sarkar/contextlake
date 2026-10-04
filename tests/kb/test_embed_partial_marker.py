"""The "embedded at this head" marker must mean the whole repo is embedded.

``kb embed`` skips a repo whose indexed head and parser version match the markers
stored with its vectors. ``embed_repo`` clears a repo's vectors before it writes the
new ones, and it used to leave the old markers alone. So a run that stopped early
left the markers saying "complete" over a partial set of vectors:

* ``kb embed --limit 5`` on a 50-node repo, then a plain ``kb embed``: the plain run
  printed "already up to date" and the repo kept 5 of 50 vectors.
* ``kb embed --force`` that died half way (an unreachable embedder, Ctrl-C): same
  result.

Semantic search then missed most of the repo, with no warning and a healthy-looking
``doctor`` row count. The tests below pin both directions: a partial run must not
leave the repo marked current, and a complete run must still let the next plain run
skip it.
"""

from argparse import Namespace

import pytest

import contextlake.kb.embeddings as emb_pkg
from contextlake.kb.commands import cmd_embed
from contextlake.kb.embeddings.index import embed_repo
from contextlake.kb.embeddings.store import (
    VectorStore,
    build_vector_store,
    get_embedded_head,
    get_embedded_parser_version,
    set_embedded_head,
    set_embedded_parser_version,
)
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema, mark_repo_indexed
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

N_NODES = 6
# batch_size 2 over 6 nodes is 3 batches, so "dies on batch 2" leaves a partial set.
_EMBED_CONFIG = """
[kb]
store_dir = "{store}"

[embeddings]
enabled = true
provider = "ollama"
batch_size = 2
"""

# The two probe texts `cmd_embed` sends before any repo is touched. A fake that dies on
# these makes the command return at pre-flight and never reach `embed_repo`, which is a
# different test.
_PROBES = {"contextlake", "contextlake embedder readiness probe"}


class _Embedder:
    name = "fake"

    def __init__(self):
        self.batches = 0
        self.die_on_batch = None
        self.die_with = RuntimeError

    def embed(self, texts):
        if len(texts) == 1 and texts[0] in _PROBES:
            return [[1.0, 1.0]]
        self.batches += 1
        if self.die_on_batch is not None and self.batches >= self.die_on_batch:
            raise self.die_with("embedder died mid-run")
        return [[float(len(t)), 1.0] for t in texts]


def _nodes(n=N_NODES):
    return [Node(id=f"n{i}", repo="r", kind="function", name=f"fn{i}") for i in range(n)]


def _fleet(tmp_path, parser="p1"):
    """One indexed repo with ``N_NODES`` embeddable nodes. Returns ``(cfg, store_dir)``.

    ``parser=None`` is a shard that carries no parser version. There the parser marker
    cannot tell a partial repo from a complete one (unknown matches unknown, see
    ``get_embedded_parser_version``), so the head marker is the only thing between the
    next plain run and a skip."""
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    mark_repo_indexed(s, "r", "h1", parser)
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="h1", parser_version=parser, nodes=_nodes(), edges=[]))
    return cfg, store_dir


def _run(cfg, embedder, monkeypatch, **flags):
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: embedder)
    args = dict(config=str(cfg), workspace=None, source=None, repo=None,
                limit=None, force=False)
    args.update(flags)
    return cmd_embed(Namespace(**args))


def _stored(store_dir):
    vs = build_vector_store(store_dir / "embeddings.sqlite", backend="auto")
    try:
        return vs.count_repo("r")
    finally:
        vs.close()


def _head_marker(store_dir):
    vs = build_vector_store(store_dir / "embeddings.sqlite", backend="auto")
    try:
        return get_embedded_head(vs, "r")
    finally:
        vs.close()


# --- command level: the next PLAIN run must repair a partial repo ----------------

@pytest.mark.parametrize("parser", ["p1", None])
def test_a_limited_run_does_not_leave_the_repo_marked_current(
        parser, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, store_dir = _fleet(tmp_path, parser)
    emb = _Embedder()

    assert _run(cfg, emb, monkeypatch) == 0
    assert _stored(store_dir) == N_NODES

    assert _run(cfg, emb, monkeypatch, limit=2) == 0
    # A limited pass overwrites its slice and deletes nothing (it used to cut the repo
    # to 2 vectors), but it still withdraws the "complete at this head" claim.
    assert _stored(store_dir) == N_NODES
    assert _head_marker(store_dir) is None

    before = emb.batches
    assert _run(cfg, emb, monkeypatch) == 0
    # LOAD-BEARING: before the marker fix the plain run found the old head marker, said
    # "already up to date" and embedded nothing.
    assert emb.batches > before, "the plain run skipped a repo a limited run left partial"
    assert _stored(store_dir) == N_NODES


@pytest.mark.parametrize("parser", ["p1", None])
def test_an_interrupted_force_run_does_not_leave_the_repo_marked_current(
        parser, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, store_dir = _fleet(tmp_path, parser)
    emb = _Embedder()
    assert _run(cfg, emb, monkeypatch) == 0
    assert _stored(store_dir) == N_NODES

    dying = _Embedder()
    dying.die_on_batch = 2
    assert _run(cfg, dying, monkeypatch, force=True) == 1  # every attempted repo failed
    # The failed pass keeps every vector it did not reach. It used to clear the repo first
    # and leave only batch 1: 6,048 vectors down to 0 when an embedder failed early.
    assert _stored(store_dir) == N_NODES
    assert _head_marker(store_dir) is None

    before = emb.batches
    assert _run(cfg, emb, monkeypatch) == 0
    assert emb.batches > before, "the plain run skipped the repo the failed pass left"
    assert _stored(store_dir) == N_NODES


@pytest.mark.parametrize("parser", ["p1", None])
def test_a_keyboard_interrupt_mid_force_run_does_not_leave_the_repo_marked_current(
        parser, tmp_path, monkeypatch):
    """`except Exception` in the command does not catch Ctrl-C, so this leaves
    through a different door than the failure above and has to end the same way."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, store_dir = _fleet(tmp_path, parser)
    emb = _Embedder()
    assert _run(cfg, emb, monkeypatch) == 0

    dying = _Embedder()
    dying.die_on_batch = 2
    dying.die_with = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _run(cfg, dying, monkeypatch, force=True)
    assert _stored(store_dir) == N_NODES
    assert _head_marker(store_dir) is None

    before = emb.batches
    assert _run(cfg, emb, monkeypatch) == 0
    assert emb.batches > before
    assert _stored(store_dir) == N_NODES


@pytest.mark.parametrize("parser", ["p1", None])
def test_a_complete_run_still_lets_the_next_plain_run_skip(parser, tmp_path, monkeypatch):
    """The other direction: the skip is the point of the markers, and the fix must not
    cost it. A repo embedded in full is not touched again."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, store_dir = _fleet(tmp_path, parser)
    emb = _Embedder()

    assert _run(cfg, emb, monkeypatch) == 0
    assert _stored(store_dir) == N_NODES
    assert _head_marker(store_dir) == "h1"
    batches_after_first = emb.batches
    assert batches_after_first == 3

    assert _run(cfg, emb, monkeypatch) == 0
    assert emb.batches == batches_after_first, "an unchanged, complete repo is skipped"
    assert _stored(store_dir) == N_NODES


# --- embed_repo level: it owns the vectors, so it owns the markers ---------------

def _vector_store_with_markers(tmp_path):
    vs = VectorStore(tmp_path / "e.sqlite")
    set_embedded_head(vs, "r", "h1")
    set_embedded_parser_version(vs, "r", "p1")
    return vs


def test_embed_repo_clears_both_markers_when_the_embedder_dies(tmp_path):
    write_shard(tmp_path, GraphShard(
        repo="r", head_commit="h1", parser_version="p1", nodes=_nodes(), edges=[]))
    vs = _vector_store_with_markers(tmp_path)
    try:
        dying = _Embedder()
        dying.die_on_batch = 2
        with pytest.raises(RuntimeError):
            embed_repo(tmp_path, vs, dying, "r", batch_size=2)
        assert vs.count_repo("r") == 2
        assert get_embedded_head(vs, "r") is None
        assert get_embedded_parser_version(vs, "r") is None
    finally:
        vs.close()


def test_embed_repo_clears_both_markers_on_a_limited_run(tmp_path):
    write_shard(tmp_path, GraphShard(
        repo="r", head_commit="h1", parser_version="p1", nodes=_nodes(), edges=[]))
    vs = _vector_store_with_markers(tmp_path)
    try:
        assert embed_repo(tmp_path, vs, _Embedder(), "r", batch_size=2, limit=2) == 2
        assert get_embedded_head(vs, "r") is None
        assert get_embedded_parser_version(vs, "r") is None
    finally:
        vs.close()


def test_embed_repo_clears_the_markers_before_it_writes_a_vector(tmp_path):
    """Order matters. A crash between the two steps must leave a repo that gets
    re-embedded, never markers that claim a vector set that has changed."""
    write_shard(tmp_path, GraphShard(
        repo="r", head_commit="h1", parser_version="p1", nodes=_nodes(), edges=[]))

    class _DiesOnFirstWrite(VectorStore):
        def upsert(self, items):
            raise OSError("disk went away")

    seed = VectorStore(tmp_path / "e.sqlite")
    seed.upsert((n.id, "r", [1.0, 1.0]) for n in _nodes())
    seed.close()
    vs = _DiesOnFirstWrite(tmp_path / "e.sqlite")
    set_embedded_head(vs, "r", "h1")
    set_embedded_parser_version(vs, "r", "p1")
    try:
        with pytest.raises(OSError):
            embed_repo(tmp_path, vs, _Embedder(), "r", batch_size=2)
        assert vs.count_repo("r") == N_NODES, "the vectors were never touched"
        assert get_embedded_head(vs, "r") is None, "the markers were already gone"
        assert get_embedded_parser_version(vs, "r") is None
    finally:
        vs.close()


def test_embed_repo_leaves_the_markers_alone_when_it_touches_no_vectors(tmp_path):
    """A shard holding only kinds this pass never embeds is returned from before any
    clear. Its vectors belong to another writer, so its markers stay too."""
    write_shard(tmp_path, GraphShard(
        repo="r", head_commit="h1", parser_version="p1",
        nodes=[Node(id="d1", repo="r", kind="document", name="a page")], edges=[]))
    vs = _vector_store_with_markers(tmp_path)
    try:
        assert embed_repo(tmp_path, vs, _Embedder(), "r") == 0
        assert get_embedded_head(vs, "r") == "h1"
        assert get_embedded_parser_version(vs, "r") == "p1"
    finally:
        vs.close()


def test_a_complete_pass_sweeps_the_vectors_of_nodes_that_are_gone(tmp_path):
    """The sweep half of write-then-sweep: a node the shard no longer holds loses its
    vector once a complete pass has written every node that remains."""
    write_shard(tmp_path, GraphShard(
        repo="r", head_commit="h1", parser_version="p1", nodes=_nodes(), edges=[]))
    vs = VectorStore(tmp_path / "e.sqlite")
    try:
        vs.upsert([("deleted_fn", "r", [1.0, 1.0]), ("other_repo_fn", "q", [1.0, 1.0])])
        assert embed_repo(tmp_path, vs, _Embedder(), "r", batch_size=2) == N_NODES
        assert vs.count_repo("r") == N_NODES          # "deleted_fn" swept
        assert vs.count_repo("q") == 1                # another repo untouched
    finally:
        vs.close()
