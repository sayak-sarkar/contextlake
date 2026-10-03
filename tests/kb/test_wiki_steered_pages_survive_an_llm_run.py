"""`.contextlake/wiki.toml` `pages` must survive a run that has an LLM configured.

The structural stage planned a repository's module pages WITH the steering override, and
the generated stage recomputed the plan WITHOUT it, then pruned every stored module page
that was not in its own plan. On a repository that asked for pages the heuristic does not
pick (any small repository, or a large one naming two subsystems of six), a single run
with an LLM wrote the requested pages and then deleted them, with their `@wiki:` partitions,
shards and vectors.

Driven through `cmd_wiki` with a fake LLM and a fake embedder. A test of the planner alone
would pass: `_module_page_plan` is correct, and the defect is that the two stages did not
call it the same way.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

import contextlake.kb.llm as llm_pkg
from contextlake.kb.cmds import wiki as wiki_cmd
from contextlake.kb.cmds.wiki import cmd_wiki
from contextlake.kb.embeddings.store import VectorStore
from contextlake.kb.model import Node, Repo
from contextlake.kb.state import check_schema
from contextlake.kb.store.shards import GraphShard, shard_path, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore
from contextlake.kb.wiki.structural import is_structural_page

_CFG = '[kb]\nstore_dir = "{store}"\n\n[llm]\nenabled = true\nprovider = "ollama"\n'


class _FakeEmbedder:
    name = "fake-embedder"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _FakeLlm:
    """Passes the council and writes a draft built from the structural page in the prompt."""

    name = "fake"

    def __init__(self):
        self.page_prompts: list[str] = []

    def generate(self, prompt, *, system=None):
        if "Review lens" in prompt:
            return '{"score": 0.95, "issues": []}'
        self.page_prompts.append(prompt)
        from wiki_doubles import sound_draft

        return sound_draft(prompt)


@pytest.fixture
def fake_vectors(monkeypatch):
    monkeypatch.setattr("contextlake.kb.embeddings.build_embedder",
                        lambda cfg_: _FakeEmbedder())
    monkeypatch.setattr("contextlake.kb.embeddings.store.build_vector_store",
                        lambda path, **kw: VectorStore(path))


def _setup(tmp_path, *, pages, n_modules=4, nodes_per_module=12):
    """A repo ``fed`` whose working tree carries a `wiki.toml` naming ``pages``.

    Nodes go into both the SQLite index (what `repo_modules` reads) and the shard (what
    `repo_brief` reads), as a real index run writes them.
    """
    store_dir = tmp_path / "kb"
    store_dir.mkdir(parents=True)
    (tmp_path / "kb.toml").write_text(_CFG.format(store=store_dir.as_posix()),
                                      encoding="utf-8")
    repo_dir = tmp_path / "fed"
    if pages is not None:
        (repo_dir / ".contextlake").mkdir(parents=True)
        listed = ", ".join(f'"{p}"' for p in pages)
        (repo_dir / ".contextlake" / "wiki.toml").write_text(f"pages = [{listed}]\n",
                                                             encoding="utf-8")
    else:
        repo_dir.mkdir(parents=True)
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="fed", path=str(repo_dir)))
    nodes = [Node(id=f"mod{m}_n{i}", repo="fed", kind="function", name=f"fn{i}",
                  file=f"mod{m}/f{i}.py")
             for m in range(n_modules) for i in range(nodes_per_module)]
    store.upsert_nodes("fed", nodes)
    store.close()
    write_shard(store_dir, GraphShard(repo="fed", head_commit="fedhead", nodes=nodes,
                                      edges=[]))
    return store_dir, repo_dir


def _wiki(tmp_path, monkeypatch, llm):
    """One `kb wiki` run. ``llm`` of None is the no-LLM run (structural pages only)."""
    monkeypatch.setattr(llm_pkg, "build_llm", lambda cfg: llm)
    return cmd_wiki(Namespace(config=str(tmp_path / "kb.toml")))


def _module_page(store_dir, prefix):
    return store_dir / "wiki" / "_modules" / f"fed__{prefix}.md"


def _vectors(store_dir, prefix):
    vs = VectorStore(store_dir / "embeddings.sqlite")
    try:
        return vs.count_repo(f"@wiki:fed::{prefix}")
    finally:
        vs.close()


def _has_partition(store_dir, prefix):
    store = SqliteStore(store_dir / "index.sqlite")
    try:
        return store.get_node(f"@wiki:fed::{prefix}:0") is not None
    finally:
        store.close()


def _assert_alive(store_dir, prefix, *, generated, logs=""):
    page = _module_page(store_dir, prefix)
    assert page.exists(), f"requested page `{prefix}` is gone from disk.\n{logs}"
    text = page.read_text(encoding="utf-8")
    assert is_structural_page(text) is (not generated), (
        f"`{prefix}` is {'structural' if is_structural_page(text) else 'generated'}")
    assert _has_partition(store_dir, prefix), f"`{prefix}` lost its @wiki partition.\n{logs}"
    assert shard_path(store_dir, f"@wiki:fed::{prefix}").exists(), (
        f"`{prefix}` lost its shard.\n{logs}")
    assert _vectors(store_dir, prefix) > 0, f"`{prefix}` lost its vectors.\n{logs}"


@pytest.mark.parametrize("structural_run_first", [False, True],
                         ids=["one-llm-run", "structural-run-then-llm-run"])
def test_requested_pages_survive_a_run_with_an_llm(tmp_path, monkeypatch, gls_logs,
                                                   fake_vectors, structural_run_first):
    """A small repo asks for two module pages. The heuristic refuses small repos outright,
    so a generated stage that ignores the file plans nothing and prunes both.

    The two-run variant is the scheduled shape: a no-LLM run wrote the structural pages
    earlier, and the first run with a backend arrives to find them and delete them.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, _ = _setup(tmp_path, pages=["mod1", "mod3"])
    if structural_run_first:
        assert _wiki(tmp_path, monkeypatch, None) == 0
        for prefix in ("mod1", "mod3"):
            _assert_alive(store_dir, prefix, generated=False)

    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0

    logs = gls_logs.text
    assert logs.strip(), "the capture saw nothing, so the assertions below prove nothing"
    for prefix in ("mod1", "mod3"):
        _assert_alive(store_dir, prefix, generated=True, logs=logs)
    for prefix in ("mod0", "mod2"):
        assert not _module_page(store_dir, prefix).exists(), (
            f"`{prefix}` was not requested and got a page")
    assert "pruned the wiki page" not in logs, logs


def test_the_generated_stage_follows_the_requested_list_on_a_federated_repo(
        tmp_path, monkeypatch, fake_vectors):
    """The other direction. A large repo qualifies on all six modules by the heuristic, and
    its file names two. The generated stage used to ignore the file and write pages for all
    six, so the structural pages followed the maintainer and the prose pages did not.

    Second half: the pages the earlier no-steering run left behind for the other four are
    pruned, because an explicit list is a complete statement of what should exist (see
    `_module_page_plan`).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, repo_dir = _setup(tmp_path, pages=None, n_modules=6, nodes_per_module=835)
    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0
    for m in range(6):
        _assert_alive(store_dir, f"mod{m}", generated=True)

    (repo_dir / ".contextlake").mkdir()
    (repo_dir / ".contextlake" / "wiki.toml").write_text('pages = ["mod1", "mod4"]\n',
                                                         encoding="utf-8")
    second = _FakeLlm()
    assert _wiki(tmp_path, monkeypatch, second) == 0

    for prefix in ("mod1", "mod4"):
        _assert_alive(store_dir, prefix, generated=True)
    for prefix in ("mod0", "mod2", "mod3", "mod5"):
        assert not _module_page(store_dir, prefix).exists(), f"`{prefix}` was not pruned"
        assert not _has_partition(store_dir, prefix)
        assert _vectors(store_dir, prefix) == 0


def test_one_plan_is_computed_per_repo_and_it_carries_the_override(tmp_path, monkeypatch,
                                                                   fake_vectors):
    """"One plan, computed once" as a property. Two calls could agree today and drift the
    day one of them is edited, which is how this defect started. The call count is what
    pins the structure, and the override argument is what pins its content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _setup(tmp_path, pages=["mod1", "mod3"])
    real = wiki_cmd._module_page_plan
    calls: list[tuple[str, list[str] | None]] = []

    def _spy(store, repo_id, node_count, *, override=None):
        calls.append((repo_id, override))
        return real(store, repo_id, node_count, override=override)

    monkeypatch.setattr(wiki_cmd, "_module_page_plan", _spy)
    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0

    assert calls == [("fed", ["mod1", "mod3"])], calls


def test_a_repo_the_structural_stage_skipped_is_planned_once_with_the_override(
        tmp_path, monkeypatch, fake_vectors):
    """The structural stage skips a repository that indexed to no symbols, so it leaves no
    plan behind. The generated stage then plans that repository itself, and it must pass
    the same steering list the stage would have, read from the repository's own path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store_dir, _ = _setup(tmp_path, pages=["mod1"])
    write_shard(store_dir, GraphShard(repo="fed", head_commit="fedhead", nodes=[], edges=[]))
    real = wiki_cmd._module_page_plan
    calls: list[tuple[str, list[str] | None]] = []

    def _spy(store, repo_id, node_count, *, override=None):
        calls.append((repo_id, override))
        return real(store, repo_id, node_count, override=override)

    monkeypatch.setattr(wiki_cmd, "_module_page_plan", _spy)
    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0

    assert calls == [("fed", ["mod1"])], calls


def _write_pages(repo_dir, pages):
    (repo_dir / ".contextlake").mkdir(exist_ok=True)
    listed = ", ".join(f'"{p}"' for p in pages)
    (repo_dir / ".contextlake" / "wiki.toml").write_text(f"pages = [{listed}]\n",
                                                         encoding="utf-8")


def test_a_long_requested_list_is_not_cut_at_the_per_run_generation_cap(
        tmp_path, monkeypatch, fake_vectors):
    """The plan carried only the first 20 names of an explicit list, so the rest never got
    a page. The 20 is how many pages ONE run generates, and `_select_module_pages` already
    enforces it by rotating through the tail. Cutting the plan as well turned a per-run
    bound into a permanent limit on what a maintainer can ask for.

    A run must still stop at the cap: the first run below writes 20 generated pages and the
    second picks up the other five.
    """
    from contextlake.kb.cmds.wiki import _MAX_MODULE_PAGES_PER_REPO

    monkeypatch.setenv("HOME", str(tmp_path))
    n = _MAX_MODULE_PAGES_PER_REPO + 5
    names = [f"mod{i}" for i in range(n)]
    store_dir, _ = _setup(tmp_path, pages=names, n_modules=n)

    first = _FakeLlm()
    assert _wiki(tmp_path, monkeypatch, first) == 0
    assert len([p for p in first.page_prompts if "ONLY the" in p]) == _MAX_MODULE_PAGES_PER_REPO

    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0
    for name in names:
        _assert_alive(store_dir, name, generated=True)


def test_the_prune_never_sees_a_list_cut_at_the_cap(tmp_path, monkeypatch, fake_vectors):
    """`_prune_orphan_module_pages` must be handed the FULL list of what should exist, never
    a selection (its own docstring says so). Five pages were requested and generated. The
    file then grows to 25 names with those five last, and a plan cut at 20 would call them
    orphans and delete them, with their partitions and vectors."""
    from contextlake.kb.cmds.wiki import _MAX_MODULE_PAGES_PER_REPO

    monkeypatch.setenv("HOME", str(tmp_path))
    n = _MAX_MODULE_PAGES_PER_REPO + 5
    names = [f"mod{i}" for i in range(n)]
    store_dir, repo_dir = _setup(tmp_path, pages=names[-5:], n_modules=n)
    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0
    for name in names[-5:]:
        _assert_alive(store_dir, name, generated=True)

    _write_pages(repo_dir, names)
    assert _wiki(tmp_path, monkeypatch, _FakeLlm()) == 0

    for name in names:
        _assert_alive(store_dir, name, generated=True)
