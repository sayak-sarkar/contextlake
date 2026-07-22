"""Tests for the embed pass (node_text + embed_repo) and the embed command."""

from argparse import Namespace

import contextlake.kb.embeddings as emb_pkg
from contextlake.kb import commands as commands_mod
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


def test_node_text_v2_includes_signature_and_capped_doc():
    n = Node(id="x", repo="r", kind="function", name="proc", file="a.py",
             attrs={"signature": "(order, cfg)",
                    "doc": "Charge the saved card for an order. " + "pad " * 200})
    t = node_text(n)
    assert "(order, cfg)" in t
    assert "Charge the saved card" in t
    # the doc contribution is capped so a verbose docstring can't drown the name
    assert len(t) < 500
    # nodes without attrs still embed exactly as before
    bare = Node(id="y", repo="r", kind="function", name="foo", file="a.py")
    assert node_text(bare) == "function foo a.py"


def test_cmd_embed_content_version_forces_one_full_reembed(tmp_path, monkeypatch):
    """A node->text mapping bump must re-embed everything once (despite unchanged
    HEADs), then stamp the store so incremental behavior resumes."""
    import contextlake.kb.embeddings.index as emb_index
    from contextlake.kb.embeddings.store import get_content_version, set_content_version

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _setup_embed_repo(tmp_path, "h1")
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())
    calls = []
    real = emb_index.embed_repo
    monkeypatch.setattr(emb_index, "embed_repo",
                        lambda *a, **k: (calls.append(a[3]), real(*a, **k))[1])
    base = dict(config=str(cfg), workspace=None, source=None, repo=None,
                limit=None, force=False)

    assert cmd_embed(Namespace(**base)) == 0
    assert calls == ["r"]                        # first embed, stamps the version
    assert cmd_embed(Namespace(**base)) == 0
    assert calls == ["r"]                        # same HEAD + same version -> skipped

    emb_path = tmp_path / "kbstore" / "embeddings.sqlite"
    vs = build_vector_store(emb_path, backend="auto")
    try:
        set_content_version(vs, 1)               # simulate a name-only-era store
    finally:
        vs.close()

    assert cmd_embed(Namespace(**base)) == 0
    assert calls == ["r", "r"]                   # stale version -> re-embedded
    vs = build_vector_store(emb_path, backend="auto")
    try:
        assert get_content_version(vs) == emb_index.EMBED_CONTENT_VERSION
    finally:
        vs.close()


def test_cmd_embed_watch_reruns_the_pass(tmp_path, monkeypatch):
    """`embed --watch` routes the embed pass through _watch_loop and re-runs it."""
    from contextlake.kb import commands as cmds

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _setup_embed_repo(tmp_path, "h1")
    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())

    passes = []

    def fake_watch(run_once, *, interval=60, **kw):
        passes.append(run_once())               # simulate two scheduler passes
        passes.append(run_once())
        return 2

    monkeypatch.setattr(cmds, "_watch_loop", fake_watch)
    base = dict(config=str(cfg), workspace=None, source=None, repo=None,
                limit=None, force=False, interval=0)
    assert cmd_embed(Namespace(**base, watch=True)) == 0
    assert passes == [0, 0]                      # each pass ran and returned success


def test_embed_repo_skips_non_definition_kinds(tmp_path):
    # file / module / package / topic nodes carry little semantic signal and (for
    # the shared ones) repeat ids across repos; embed_repo skips them by default.
    from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
    nodes = [
        Node(id="fn", repo="r", kind="function", name="charge"),
        Node(id="cls", repo="r", kind="class", name="OrderService"),
        Node(id="ep", repo="r", kind="endpoint", name="/charge"),
        Node(id="file", repo="r", kind="file", name="svc.py"),
        Node(id="mod", repo="(mods)", kind="module", name="os"),
        Node(id="pkg", repo="(packages)", kind="package", name="requests"),
    ]
    write_shard(tmp_path, GraphShard(repo="r", head_commit="h", nodes=nodes, edges=[]))
    vs = VectorStore(tmp_path / "e.sqlite")
    try:
        n = embed_repo(tmp_path, vs, _FakeEmbedder(), "r")
        assert n == 3                       # function, class, endpoint — not file/module/package
        assert vs.count() == 3
        assert {"function", "class", "endpoint"} <= EMBEDDABLE_KINDS
        assert "file" not in EMBEDDABLE_KINDS and "module" not in EMBEDDABLE_KINDS
        # an explicit kinds override still wins
        assert embed_repo(tmp_path, vs, _FakeEmbedder(), "r", kinds={"file"}) == 1
    finally:
        vs.close()


def test_cmd_embed_fails_fast_on_unavailable_embedder(tmp_path, monkeypatch, capsys):
    """A whole-environment problem (missing kb-local extra, unreachable Ollama)
    must be reported ONCE via an up-front probe — not repeated for every repo."""
    import contextlake.kb.embeddings.index as emb_index

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    for rid in ("r1", "r2", "r3"):
        s.upsert_repo(Repo(id=rid, path=str(tmp_path / rid)))
        write_shard(store_dir, GraphShard(
            repo=rid, head_commit="h",
            nodes=[Node(id=f"{rid}_n", repo=rid, kind="function", name="foo")], edges=[]))
    s.close()

    class _DeadEmbedder:
        name = "builtin"

        def embed(self, texts):
            raise RuntimeError("needs the 'kb-local' extra")

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _DeadEmbedder())
    calls = []
    monkeypatch.setattr(emb_index, "embed_repo",
                        lambda *a, **k: calls.append(a[3]))  # must never be reached

    args = Namespace(config=str(cfg), workspace=None, source=None, repo=None, limit=None)
    assert cmd_embed(args) == 1
    assert calls == []                                   # never entered the per-repo loop
    out = capsys.readouterr().out
    assert out.count("kb-local") <= 2                    # one probe message, not one-per-repo


def test_resource_kind_is_embeddable():
    from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
    assert "resource" in EMBEDDABLE_KINDS
    # non-meaningful HCL kinds stay out of semantic search
    for k in ("variable", "output", "data", "module", "local"):
        assert k not in EMBEDDABLE_KINDS


def test_sql_table_and_view_are_embeddable():
    from contextlake.kb.embeddings.index import EMBEDDABLE_KINDS
    assert "table" in EMBEDDABLE_KINDS
    assert "view" in EMBEDDABLE_KINDS
    assert "procedure" not in EMBEDDABLE_KINDS  # low signal without a signature


class _SpyProgress:
    """Stand-in for style.Progress that only records call counts (no rendering),
    mirroring the wire-through idiom in tests/kb/test_kb_wiki.py."""

    instances: list["_SpyProgress"] = []

    def __init__(self, total, **kwargs):
        self.total = total
        self.label = kwargs.get("label")
        self.advance_calls = 0
        self.done_calls = 0
        _SpyProgress.instances.append(self)

    def advance(self, *args, **kwargs):
        self.advance_calls += 1

    def done(self, *args, **kwargs):
        self.done_calls += 1


def test_cmd_embed_reports_progress_and_leaves_stdout_unchanged(tmp_path, monkeypatch, gls_logs):
    """Wire-through: Progress.advance fires once per pass target (success, failure,
    and incremental-skip branches alike) and done() once, on a separate channel
    from the existing stdout detail/summary log() lines, which must render exactly
    as before (byte-identical).

    Asserts on gls_logs (not capsys) per the convention documented in
    tests/kb/test_source_cmd.py -- see test_kb_wiki.py's equivalent test.
    """
    import contextlake.kb.embeddings.index as emb_index
    from contextlake.kb.embeddings.store import set_embedded_head

    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir = tmp_path / "kbstore"
    store_dir.mkdir(parents=True)
    cfg = tmp_path / "kb.toml"
    cfg.write_text(_EMBED_CONFIG.format(store=store_dir.as_posix()))

    s = SqliteStore(store_dir / "index.sqlite")
    check_schema(s)
    # r1: embeds cleanly. r2: embed_repo raises (failure/continue branch).
    # r3: incremental skip (embedded head already matches, skip/continue branch).
    s.upsert_repo(Repo(id="r1", path=str(tmp_path / "r1"), head_commit="h1"))
    s.upsert_repo(Repo(id="r2", path=str(tmp_path / "r2"), head_commit="h2"))
    s.upsert_repo(Repo(id="r3", path=str(tmp_path / "r3"), head_commit="h3"))
    s.close()
    for rid, head in (("r1", "h1"), ("r2", "h2"), ("r3", "h3")):
        write_shard(store_dir, GraphShard(
            repo=rid, head_commit=head,
            nodes=[Node(id=f"{rid}_n", repo=rid, kind="function", name="foo")], edges=[]))

    monkeypatch.setattr(emb_pkg, "build_embedder", lambda c: _FakeEmbedder())

    vs = build_vector_store(store_dir / "embeddings.sqlite", backend="auto")
    try:
        set_embedded_head(vs, "r3", "h3")   # r3 already up to date -> skip branch
    finally:
        vs.close()

    real = emb_index.embed_repo

    def _flaky(store_dir, vs, embedder, repo_id, **kw):
        if repo_id == "r2":
            raise RuntimeError("embed failed for r2")
        return real(store_dir, vs, embedder, repo_id, **kw)

    monkeypatch.setattr(emb_index, "embed_repo", _flaky)

    _SpyProgress.instances = []
    monkeypatch.setattr(commands_mod.style, "Progress", _SpyProgress)

    args = Namespace(config=str(cfg), workspace=None, source=None, repo=None,
                      limit=None, force=False)
    assert cmd_embed(args) == 0   # not every repo failed -> success

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == 3
    assert p.advance_calls == 3          # per-item, across success/failure/skip branches
    assert p.done_calls == 1

    text = gls_logs.text
    assert "r1: embedded 1 node(s)" in text          # success detail line, unchanged
    # failure detail line, unchanged (checked around the existing em-dash separator
    # without retyping it here, per the no-em-dash-in-new-code convention)
    assert "r2: embed failed" in text
    assert "embed failed for r2" in text
    assert "r3: embedded" not in text                # skipped -> no detail line, as before
    assert "✓ Embed complete: 1 vector(s) written" in text   # glyph-prefixed summary
