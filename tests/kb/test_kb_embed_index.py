"""Tests for the embed pass (node_text + embed_repo) and the embed command."""

from argparse import Namespace

import contextlake.kb.embeddings as emb_pkg
from contextlake.kb.commands import cmd_embed
from contextlake.kb.embeddings.index import embed_repo, node_text
from contextlake.kb.embeddings.store import VectorStore, build_vector_store
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore


class _FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        # deterministic 2-D vector per text; longer text -> larger x component
        return [[float(len(t)), 1.0] for t in texts]


def test_node_text_combines_fields():
    n = Node(id="x", repo="r", kind="function", name="foo",
             qualified_name="mod.foo", file="a.py")
    t = node_text(n)
    assert "function" in t and "foo" in t and "mod.foo" in t and "a.py" in t


def test_embed_repo_writes_vectors(tmp_path):
    nodes = [
        Node(id="n1", repo="r", kind="function", name="foo"),
        Node(id="n2", repo="r", kind="class", name="Bar"),
    ]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=[]))
    vs = VectorStore(tmp_path / "embeddings.sqlite")
    try:
        n = embed_repo(tmp_path, vs, _FakeEmbedder(), "r")
        assert n == 2 and vs.count() == 2
        assert vs.search([3.0, 1.0], k=1)[0][0] in {"n1", "n2"}
        # re-embedding the same repo replaces, not duplicates
        assert embed_repo(tmp_path, vs, _FakeEmbedder(), "r") == 2
        assert vs.count() == 2
    finally:
        vs.close()


def test_embed_repo_limit_and_missing_shard(tmp_path):
    nodes = [Node(id=f"n{i}", repo="r", kind="function", name=f"f{i}") for i in range(5)]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=[]))
    vs = VectorStore(tmp_path / "e.sqlite")
    try:
        assert embed_repo(tmp_path, vs, _FakeEmbedder(), "r", limit=2) == 2
        assert embed_repo(tmp_path, vs, _FakeEmbedder(), "absent") == 0  # no shard
    finally:
        vs.close()


_EMBED_CONFIG = """
[kb]
store_dir = "{store}"

[embeddings]
enabled = true
provider = "ollama"
batch_size = 8
"""


def test_cmd_embed_e2e(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="h",
        nodes=[Node(id="n1", repo="r", kind="function", name="foo")], edges=[]))

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    args = Namespace(config=str(cfg), workspace=None, source=None, repo=None, limit=None)
    assert cmd_embed(args) == 0

    # read back via the same factory cmd_embed used (sqlite-vec when available, else brute)
    vs = build_vector_store(store_dir / "embeddings.sqlite", backend="auto")
    try:
        assert vs.count() == 1
    finally:
        vs.close()


def test_cmd_embed_returns_nonzero_when_all_repos_fail(tmp_path, monkeypatch):
    """If every repo in a non-empty work set fails to embed (e.g. the embedder
    goes unreachable mid-run), cmd_embed must exit non-zero, not report success."""
    import contextlake.kb.embeddings.index as emb_index

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "r")))
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit="h",
        nodes=[Node(id="n1", repo="r", kind="function", name="foo")], edges=[]))

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())

    def boom(*a, **k):
        raise RuntimeError("embedder unreachable")
    monkeypatch.setattr(emb_index, "embed_repo", boom)

    args = Namespace(config=str(cfg), workspace=None, source=None, repo=None, limit=None)
    assert cmd_embed(args) == 1


def test_cmd_embed_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')  # embeddings off by default
    args = Namespace(config=str(cfg), workspace=None, source=None, repo=None, limit=None)
    assert cmd_embed(args) == 0
    assert not (store_dir / "embeddings.sqlite").exists()


def test_embedded_head_roundtrip(tmp_path):
    from contextlake.kb.embeddings.store import get_embedded_head, set_embedded_head
    vs = VectorStore(tmp_path / "e.sqlite")
    try:
        assert get_embedded_head(vs, "r") is None
        set_embedded_head(vs, "r", "abc")
        assert get_embedded_head(vs, "r") == "abc"
        set_embedded_head(vs, "r", None)  # empty -> None back
        assert get_embedded_head(vs, "r") is None
    finally:
        vs.close()


def _setup_embed_repo(tmp_path, head):
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))
    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    s.upsert_repo(Repo(id="r", path=str(tmp_path / "r"), head_commit=head))
    s.close()
    write_shard(store_dir, GraphShard(
        repo="r", head_commit=head,
        nodes=[Node(id="n1", repo="r", kind="function", name="foo")], edges=[]))
    return cfg


def test_cmd_embed_incremental_skips_force_and_head_move(tmp_path, monkeypatch):
    import contextlake.kb.embeddings.index as emb_index

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _setup_embed_repo(tmp_path, "h1")
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())

    calls = []
    real = emb_index.embed_repo
    monkeypatch.setattr(emb_index, "embed_repo",
                        lambda *a, **k: (calls.append(a[3]), real(*a, **k))[1])

    base = dict(config=str(cfg), workspace=None, source=None, repo=None, limit=None)
    assert cmd_embed(Namespace(**base, force=False)) == 0
    assert calls == ["r"]                       # first embed

    assert cmd_embed(Namespace(**base, force=False)) == 0
    assert calls == ["r"]                       # head unchanged -> skipped

    assert cmd_embed(Namespace(**base, force=True)) == 0
    assert calls == ["r", "r"]                  # --force re-embeds

    _setup_embed_repo(tmp_path, "h2")           # re-index moves HEAD
    assert cmd_embed(Namespace(**base, force=False)) == 0
    assert calls == ["r", "r", "r"]             # moved HEAD -> re-embeds
