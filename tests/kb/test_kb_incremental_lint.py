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
    """Capture contextlake log messages directly off the named logger (robust to
    stdout swapping in capsys/caplog)."""
    logger = logging.getLogger("contextlake")
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


def test_incremental_reindexes_a_repo_whose_parser_moved_on(tmp_path, logs):
    """A repo at an unchanged commit, built by an older parser, is not "unchanged".

    The upgrade path this covers: index with the previous release, upgrade, then
    run `kb index`. It used to report the repo unchanged and leave the old graph
    in place, with doctor and lint both reporting healthy. The repo here is pure
    Python, which was the hole -- doctor's stale check only looked at C/C++.
    """
    from contextlake.kb.parse import PARSER_VERSION
    from contextlake.kb.store.shards import read_shard

    ws = tmp_path / "ws"
    _git_repo(ws / "app")
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    try:
        _index_workspace(store, store_dir, ws)
        logs.clear()
        _index_workspace(store, store_dir, ws)
        assert any("1 unchanged" in m for m in logs)

        # Rewind the store to what the previous release left behind: the shard
        # stamped by the old parser, and no stamp on the row at all, since that
        # column did not exist then.
        [repo] = store.list_repos()
        shard = read_shard(store_dir, repo.id)
        shard.parser_version = "1"
        write_shard(store_dir, shard)
        store.conn.execute("UPDATE repos SET parser_version=NULL")
        store.conn.commit()

        logs.clear()
        _index_workspace(store, store_dir, ws)
        assert any(f"older parser (1 -> {PARSER_VERSION})" in m for m in logs)  # says why
        assert any("0 unchanged" in m for m in logs)  # and rebuilds rather than skipping
        assert read_shard(store_dir, repo.id).parser_version == PARSER_VERSION
        assert store.get_repo_parser_version(repo.id) == PARSER_VERSION

        # ... and it settles: the next pass is quiet again, so the auto-rebuild
        # costs one pass after an upgrade, not one on every run.
        logs.clear()
        _index_workspace(store, store_dir, ws)
        assert any("1 unchanged" in m for m in logs)
        assert not any("older parser" in m for m in logs)
    finally:
        store.close()


# --- lint (Phase 2.5 graph health) ----------------------------------------

def _seed(store_dir, repo_path, head, edges, parser_version=None):
    """Seed one repo's shard. ``parser_version`` defaults to this build's, so a
    seeded store is current in every sense unless a test says otherwise."""
    from contextlake.kb.parse import PARSER_VERSION

    store = SqliteStore(store_dir / "index.sqlite")
    check_schema(store)
    store.upsert_repo(Repo(id="app", path=str(repo_path)))
    nodes = [Node(id="a", repo="app", kind="function", name="foo")]
    write_shard(store_dir, GraphShard(repo="app", head_commit=head, nodes=nodes, edges=edges,
                                      parser_version=parser_version or PARSER_VERSION))
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


def test_lint_reports_parser_staleness_and_agrees_with_doctor(tmp_path, monkeypatch,
                                                              logs, capsys):
    """The disagreement this closes: for a repo sitting at its indexed HEAD but
    whose shard an older parser built, ``doctor`` said "1 repo(s) indexed with an
    older parser -- re-index" while ``lint`` said "0 stale" and exited 0.

    Both commands now report it, from the same source (the shard). lint's exit
    code is deliberately unchanged -- see ``cmd_lint``'s docstring -- so the fix
    cannot turn a parser bump into a red CI gate on upgrade.
    """
    from contextlake.kb.commands import cmd_doctor, lint_result

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "app"
    head = _git_repo(repo)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir.as_posix()}"\n')
    # HEAD matches the index and the one edge resolves: the *only* thing wrong
    # with this store is the parser that built it.
    _seed(store_dir, repo, head, [_edge("a")], parser_version="0")

    store = SqliteStore(store_dir / "index.sqlite")
    try:
        res = lint_result(store, store_dir)
    finally:
        store.close()
    assert res["stale"] == 0 and res["dangling"] == 0   # nothing else is wrong
    assert res["parser_stale"] == 1 and res["parser_stale_repos"] == ["app"]

    assert cmd_lint(Namespace(config=str(cfg))) == 0    # exit code unchanged
    assert any("parser-stale: app" in m for m in logs)
    assert any("1 built by an older parser" in m for m in logs)

    cmd_doctor(Namespace(config=str(cfg)))
    assert "1 repo(s) indexed with an older parser" in capsys.readouterr().out

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


def test_single_repo_index_skips_an_unchanged_head_like_the_workspace_path(tmp_path, logs):
    """`kb index <repo>` re-parsed on every run, contradicting --force's own
    help ("only repos whose HEAD moved") -- which --workspace does honour. Same
    store, same commit, seconds apart."""
    from contextlake.kb.commands import cmd_index

    repo = tmp_path / "app"
    _git_repo(repo)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()

    def _args(force=False):
        return Namespace(config=None, store_dir=str(store_dir), workspace=None,
                         source=str(repo), repo="app", force=force)

    assert cmd_index(_args()) == 0
    assert any("Indexed app" in m for m in logs)

    logs.clear()
    assert cmd_index(_args()) == 0
    assert any("unchanged" in m for m in logs), "an unchanged HEAD must be skipped"
    assert not any("Indexed app" in m for m in logs), "it re-parsed instead of skipping"

    # --force still re-indexes regardless, as its help says
    logs.clear()
    assert cmd_index(_args(force=True)) == 0
    assert any("Indexed app" in m for m in logs)

    # a new commit moves HEAD -> it re-indexes without --force
    (repo / "m.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "c2"], repo)
    logs.clear()
    assert cmd_index(_args()) == 0
    assert any("Indexed app" in m for m in logs)


def test_a_non_git_directory_does_not_inherit_an_ancestor_repos_head(tmp_path, logs):
    """`git -C <dir> rev-parse HEAD` walks UP, so a plain directory sitting
    inside another working tree was recorded as a repository at THAT tree's
    commit -- exit 0, no warning, and every staleness check thereafter comparing
    against an unrelated project's history.

    The detection already existed in repo_identity, worded well, and was simply
    never wired into the path that writes the row.
    """
    from contextlake.kb.cmds._common import _git_head

    outer_head = _git_repo(tmp_path / "outer")
    assert _git_head(tmp_path / "outer") == outer_head        # the real repo is unaffected

    inner = tmp_path / "outer" / "plain"                      # no .git of its own
    inner.mkdir()
    (inner / "a.py").write_text("def g():\n    return 2\n")

    assert _git_head(inner) is None, "must not report the ancestor repository's commit"
    assert any("DIFFERENT" in m and "ancestor" in m for m in logs), logs


def test_a_directory_with_no_git_anywhere_stays_silent(tmp_path, logs):
    """Only the misattribution case is worth a warning. A directory with no
    repository above it at all is an ordinary thing to index, and warning about
    it every time would train the reader to skip the message that matters."""
    from contextlake.kb.cmds._common import _git_head

    plain = tmp_path / "nogit"
    plain.mkdir()
    (plain / "a.py").write_text("def g():\n    return 2\n")
    assert _git_head(plain) is None
    assert not [m for m in logs if "DIFFERENT" in m], logs


# --- redaction reaches the knowledge layer --------------------------------
# <workspace> and <group> were derived from the mirror sync INI, which no kb
# command reads, so --redact, --no-redact and the default all produced the same
# unredacted log for every knowledge command -- the half of the product the flag
# is most needed for. <store> looked registered (in _open_store) but `doctor`
# builds its own SqliteStore and never goes through it.

def test_load_kb_config_registers_the_store_path(tmp_path, monkeypatch):
    from contextlake import observability
    from contextlake.kb.config import load_kb_config

    observability.reset_redactions()
    try:
        store = tmp_path / "somewhere" / "private-store"
        cfg_file = tmp_path / "kb.toml"
        cfg_file.write_text(f'[kb]\nstore_dir = "{store}"\n', encoding="utf-8")
        load_kb_config(str(cfg_file))
        assert observability.redact(f"store reachable - {store}") == \
            "store reachable - <store>"
    finally:
        observability.reset_redactions()


def test_a_first_index_redacts_repo_ids_before_it_names_them(tmp_path, logs):
    """The repo ids were registered from the STORE, which is empty on a first
    index -- exactly the run that prints every id for the first time, and so
    exactly the run whose log leaked them. They come from the walk now."""
    from contextlake import observability

    observability.reset_redactions()
    try:
        ws = tmp_path / "ws"
        _git_repo(ws / "acme-private" / "svc-alpha")
        store_dir = tmp_path / "store"
        store = SqliteStore(store_dir / "index.sqlite")
        check_schema(store)
        try:
            # the store is empty, so anything registered during this call cannot
            # have come from it -- which is the whole point of the fix
            assert store.list_repos() == []
            _index_workspace(store, store_dir, ws)
            repo_id = store.list_repos()[0].id
            scrubbed = observability.redact(f"indexed {repo_id}")
            assert repo_id not in scrubbed, scrubbed
            assert "repo-" in scrubbed, scrubbed
        finally:
            store.close()
    finally:
        observability.reset_redactions()
