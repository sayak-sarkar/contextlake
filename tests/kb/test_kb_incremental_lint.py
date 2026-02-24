"""Tests for incremental workspace indexing and the lint command."""

import logging
import os
import subprocess
from argparse import Namespace
from datetime import date

import pytest

from contextlake.kb.commands import _index_workspace, _watch_loop, cmd_lint
from contextlake.kb.model import Confidence, Edge, Node, Provenance, Repo
from contextlake.kb.state import check_schema, mark_repo_indexed
from contextlake.kb.store.shards import GraphShard, reindex_shard, write_shard
from contextlake.kb.store.sqlite_store import SqliteStore

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def logs():
    """Capture gitlab_sync log messages directly off the named logger (robust to
    stdout swapping in capsys/caplog)."""
    logger = logging.getLogger("gitlab_sync")
    saved = logger.handlers[:]
    logger.handlers.clear()
    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield messages
    logger.handlers[:] = saved


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path, body="def foo():\n    return 1\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text(body)
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)
    return _git(["rev-parse", "HEAD"], path)


# --- incremental indexing (Phase 2.6) -------------------------------------

def test_incremental_skips_unchanged_until_head_moves(tmp_path, logs):
    ws = tmp_path / "ws"
    repo = ws / "app"
    _git_repo(repo)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    try:
        _index_workspace(store, store_dir, ws)
        assert any("0 unchanged" in m for m in logs)  # first pass indexes it

        logs.clear()
        _index_workspace(store, store_dir, ws)
        assert any("1 unchanged" in m for m in logs)  # second pass skips it

        # a new commit moves HEAD -> it re-indexes
        (repo / "m.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", "c2"], repo)
        logs.clear()
        _index_workspace(store, store_dir, ws)
        assert any("0 unchanged" in m for m in logs)  # changed repo re-indexed

        logs.clear()
        _index_workspace(store, store_dir, ws, force=True)
        assert any("0 unchanged" in m for m in logs)  # --force re-indexes regardless
    finally:
        store.close()


# --- lint (Phase 2.5 graph health) ----------------------------------------

def _seed(store_dir, repo_path, head, edges):
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="app", path=str(repo_path)))
    nodes = [Node(id="a", repo="app", kind="function", name="foo")]
    write_shard(store_dir, GraphShard(repo="app", head_commit=head, nodes=nodes, edges=edges))
    reindex_shard(store, store_dir, "app")
    mark_repo_indexed(store, "app", head)
    store.close()


def _edge(dst):
    return Edge(src="a", dst=dst, relation="calls", confidence=Confidence.INFERRED,
                provenance=Provenance(source_file="m.py", source_line=1,
                                      verified_at=date(2026, 6, 21)))


def test_lint_flags_dangling_edge(tmp_path, monkeypatch, logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "app"
    head = _git_repo(repo)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    (tmp_path / "kb.toml").write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    _seed(store_dir, repo, head, [_edge("ZZmissing")])  # dst node does not exist

    rc = cmd_lint(Namespace(config=str(tmp_path / "kb.toml")))
    assert rc == 1
    assert any("dangling" in m for m in logs) and any("1 dangling" in m for m in logs)


def test_lint_clean_graph_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "app"
    head = _git_repo(repo)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    (tmp_path / "kb.toml").write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    _seed(store_dir, repo, head, [_edge("a")])  # self-edge resolves; repo HEAD matches

    assert cmd_lint(Namespace(config=str(tmp_path / "kb.toml"))) == 0


# --- watch loop (Phase 2.6) -----------------------------------------------

def test_watch_loop_runs_n_times():
    calls = []
    n = _watch_loop(lambda: calls.append(1), interval=0, iterations=3, sleep=lambda s: None)
    assert n == 3 and len(calls) == 3


def test_watch_loop_stops_on_interrupt():
    calls = []

    def boom(_):
        raise KeyboardInterrupt

    n = _watch_loop(lambda: calls.append(1), interval=0, sleep=boom)  # unbounded but interrupted
    assert n == 1 and len(calls) == 1
