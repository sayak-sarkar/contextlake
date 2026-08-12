"""A staleness marker that was never written must not read like one written empty.

The rule, stated once for every commit-keyed marker in the knowledge base:

    Absence is the absence of a value -- a missing row, a missing column, a
    missing footer clause. It is NEVER a sentinel empty string. An artefact or a
    marker that predates a field carries no field; it must not be migrated into
    something that looks like a field written empty, because every one of these
    markers exists only to be compared for equality against whatever is current,
    and "nobody wrote this yet" and "what was written is empty" are different
    answers to that comparison.

The empty string is reachable, which is what makes this a bug and not a
hypothetical: ``kb index --source <shard>.json`` passes an imported shard's
``head_commit`` and ``parser_version`` straight through to the ``repos`` row and
from there to the vector store's markers. The shard reader, ``peek_parser_version``
and ``get_repo_parser_version`` all hand ``""`` back verbatim; the vector store's
two accessors used to fold it into ``None``. One conceptual marker, two
disciplines, and the disagreement decided a staleness question.

The tests below pin both halves: the distinction itself, and the anti-loop
behaviour that any "fix" to it must not break.
"""

from argparse import Namespace

import contextlake.kb.embeddings as emb_pkg
from contextlake.kb.commands import cmd_embed
from contextlake.kb.embeddings.store import (
    VectorStore,
    get_content_version,
    get_embedded_head,
    get_embedded_parser_version,
    set_embedded_head,
    set_embedded_parser_version,
)
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema, indexed_parser_version, mark_repo_indexed
from contextlake.kb.store.shards import GraphShard, peek_parser_version, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_EMBED_CONFIG = """
[kb]
store_dir = "{store}"

[embeddings]
enabled = true
provider = "ollama"
batch_size = 8
"""


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


# --- the marker accessors: absent, written-empty, written-value ---------------

def test_embedded_parser_version_distinguishes_absent_from_written_empty(tmp_path):
    """THE LOAD-BEARING ASSERTION of this module is the ``== ""`` one below.

    Before the fix the accessor read ``row[0] if row and row[0] else None``, so a
    marker written as ``""`` came back ``None`` -- byte-identical to a repo whose
    vectors nobody has ever built. Everything else here would pass without the
    fix; only that assertion separates the two states.
    """
    vs = VectorStore(tmp_path / "vec.sqlite")
    try:
        assert get_embedded_parser_version(vs, "team/svc") is None  # never written

        set_embedded_parser_version(vs, "team/svc", "")
        # LOAD-BEARING: a written-empty version stays written-empty.
        assert get_embedded_parser_version(vs, "team/svc") == ""

        set_embedded_parser_version(vs, "team/svc", "4")
        assert get_embedded_parser_version(vs, "team/svc") == "4"

        # None means "could not be established", and is recorded by clearing the
        # marker -- not by writing "" over it, which would land back in the state
        # the assertion above exists to keep distinguishable.
        set_embedded_parser_version(vs, "team/svc", None)
        assert get_embedded_parser_version(vs, "team/svc") is None
        assert vs.conn.execute(
            "SELECT COUNT(*) FROM vec_meta WHERE key='parser:team/svc'"
        ).fetchone()[0] == 0
    finally:
        vs.close()


def test_embedded_head_distinguishes_absent_from_written_empty(tmp_path):
    """The head marker follows the same discipline as the parser marker.

    Two accessors for the same kind of fact must not answer the "is this absent?"
    question differently -- that divergence is what let the parser marker's hole
    sit next to a head marker nobody suspected.
    """
    vs = VectorStore(tmp_path / "vec.sqlite")
    try:
        assert get_embedded_head(vs, "team/svc") is None
        set_embedded_head(vs, "team/svc", "")
        assert get_embedded_head(vs, "team/svc") == ""     # written empty
        set_embedded_head(vs, "team/svc", "abc123")
        assert get_embedded_head(vs, "team/svc") == "abc123"
        set_embedded_head(vs, "team/svc", None)
        assert get_embedded_head(vs, "team/svc") is None   # cleared, not ""
    finally:
        vs.close()


def test_the_graph_side_accessors_already_keep_empty_distinct(tmp_path):
    """The read/write pair the fix had to line up with, pinned on the other side.

    The shard and the ``repos`` row were always verbatim about ``""``. Had this
    been asserted alongside the vector-store round-trip, the two disciplines could
    not have drifted apart unnoticed.
    """
    write_shard(tmp_path, GraphShard(repo="team/svc", head_commit="h1", parser_version=""))
    assert peek_parser_version(tmp_path, "team/svc") == ""

    store = SqliteStore(tmp_path / "index.sqlite")
    try:
        store.upsert_repo(Repo(id="team/svc", path="/w/team/svc"))
        mark_repo_indexed(store, "team/svc", "h1", "")
        assert store.get_repo_parser_version("team/svc") == ""
        assert indexed_parser_version(store, tmp_path, "team/svc") == ""
    finally:
        store.close()


def test_content_version_zero_sentinel_is_the_other_correct_pattern(tmp_path):
    """Not every "absent" needs a distinct value -- this one is right as it is.

    ``content_version`` has no meaningful zero: version numbering starts at 1, so 0
    is unused by construction and can carry "predates version tracking" without
    colliding with anything a writer could store. That is a reserved sentinel, not
    a collapse, and it is pinned here so nobody "fixes" it into an Optional.
    """
    vs = VectorStore(tmp_path / "vec.sqlite")
    try:
        assert get_content_version(vs) == 0
    finally:
        vs.close()


# --- the decision the markers exist to make ---------------------------------

def _fleet(tmp_path, *, parser_version):
    """One indexed repo whose shard and ``repos`` row carry ``parser_version``."""
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    mark_repo_indexed(s, "r", "h1", parser_version)
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="h1", parser_version=parser_version,
        nodes=[Node(id="n1", repo="r", kind="function", name="foo")], edges=[]))
    return cfg


def _embed_twice(cfg, monkeypatch):
    """Run ``kb embed`` twice over the same unchanged fleet; return repos touched."""
    import contextlake.kb.embeddings.index as emb_index

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    calls: list[list[str]] = []
    real = emb_index.embed_repo

    def _spy(store_dir, vs, embedder, repo_id, **kw):
        calls[-1].append(repo_id)
        return real(store_dir, vs, embedder, repo_id, **kw)

    monkeypatch.setattr(emb_index, "embed_repo", _spy)
    args = dict(config=str(cfg), workspace=None, source=None, repo=None,
                limit=None, force=False)
    for _ in range(2):
        calls.append([])
        assert cmd_embed(Namespace(**args)) == 0
    return calls


def test_a_repo_stamped_with_an_empty_parser_version_settles(tmp_path, monkeypatch):
    """The bug at the decision level, not just at the accessor.

    An imported shard can carry ``parser_version: ""``. The first embed recorded
    that, the accessor read it back as ``None``, ``None == ""`` was False, and the
    repo was re-embedded on every run for the rest of the store's life -- while the
    run reported success and nothing about the fleet had changed. The whole point
    of the marker is that an unchanged fleet embeds once.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _fleet(tmp_path, parser_version="")
    first, second = _embed_twice(cfg, monkeypatch)
    assert first == ["r"]   # nothing embedded yet -> embed it
    assert second == []     # unchanged fleet -> skip


def test_an_unstamped_repo_also_settles(tmp_path, monkeypatch):
    """The anti-loop pin, and the reason the fix went on the WRITE side.

    When the shard carries no parser version at all, a re-embed cannot record one
    either, so "unknown must never match" would re-embed this repo forever -- the
    same loop ``cmds/wiki.py`` avoids for the same reason. Un-collapsing the READ
    while still writing ``""`` for None would pass the test above and fail this
    one; clearing the marker instead passes both.

    Green before the fix as well as after: this is the regression guard, not the
    demonstration.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _fleet(tmp_path, parser_version=None)
    first, second = _embed_twice(cfg, monkeypatch)
    assert first == ["r"]
    assert second == [], "unknown/unknown must match, or an unstamped repo loops"


def test_a_moved_parser_version_still_re_embeds(tmp_path, monkeypatch):
    """The signal the two tests above must not have bought at its expense: a
    parser version that really did move re-embeds, commit unchanged."""
    import contextlake.kb.embeddings.index as emb_index

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _fleet(tmp_path, parser_version="3")
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    calls: list[str] = []
    real = emb_index.embed_repo
    monkeypatch.setattr(emb_index, "embed_repo",
                        lambda *a, **k: (calls.append(a[3]), real(*a, **k))[1])
    args = dict(config=str(cfg), workspace=None, source=None, repo=None,
                limit=None, force=False)
    assert cmd_embed(Namespace(**args)) == 0
    assert calls == ["r"]

    # Same commit, newer parser: re-stamp the graph side only.
    store_dir = tmp_path / "kbstore"
    s = SqliteStore(store_dir / "index.sqlite")
    mark_repo_indexed(s, "r", "h1", "4")
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="h1", parser_version="4",
        nodes=[Node(id="n1", repo="r", kind="function", name="foo")], edges=[]))

    assert cmd_embed(Namespace(**args)) == 0
    assert calls == ["r", "r"], "a real parser move must still invalidate the vectors"
