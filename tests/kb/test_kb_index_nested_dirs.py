"""`kb index <dir>` when the directory is not itself a repo but holds some.

Three defects lived in the same four lines: the scan was one level deep (so the
count that conveys the damage was 95% low), the remedy it printed hardcoded
`--workspace .` regardless of the path actually named, and it was a warning that
indexing then blew straight past -- leaving one real store 63% duplicate with no
repair path at the time.
"""

import logging
import os
import subprocess
from argparse import Namespace

import pytest

from contextlake.kb.cmds.index import _depth_phrase, _nested_repo_dirs

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def logs():
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


def _fleet(tmp_path):
    """The real layout that reported 1 where the truth was 3: one repo at depth
    1 and the rest a level further down, under a plain directory."""
    ws = tmp_path / "ws"
    _git_repo(ws / "solo")                    # depth 1
    _git_repo(ws / "repositories" / "alpha")  # depth 2
    _git_repo(ws / "repositories" / "beta")   # depth 2
    return ws


def _args(store_dir, source, **kw):
    return Namespace(config=None, store_dir=str(store_dir), workspace=None,
                     source=str(source), repo=None, force=False, **kw)


# --- F11: the scan is one level deep ---------------------------------------

def test_nested_scan_finds_repos_below_the_first_level(tmp_path):
    """`glob("*/.git")` sees only direct children, so 2 of these 3 were
    invisible; the count is the whole point of the message that reports it."""
    ws = _fleet(tmp_path)

    assert len(list(ws.glob("*/.git"))) == 1, "the old shallow scan, for contrast"

    found = _nested_repo_dirs(ws)
    assert len(found) == 3
    assert sorted(p.relative_to(ws).as_posix() for p in found) == [
        "repositories/alpha", "repositories/beta", "solo",
    ]


def test_nested_scan_does_not_descend_into_a_repo_or_a_skipped_dir(tmp_path):
    """Bounded: a submodule is the parent repo's business, not a separate
    workspace member, and `node_modules` is never walked at all."""
    ws = tmp_path / "ws"
    _git_repo(ws / "outer")
    _git_repo(ws / "outer" / "vendor" / "inner")       # inside a repo
    _git_repo(ws / "node_modules" / "dep")             # inside a skipped dir
    _git_repo(ws / "deep" / "a" / "b" / "c" / "leaf")  # depth 5, still found

    found = sorted(p.relative_to(ws).as_posix() for p in _nested_repo_dirs(ws))
    assert found == ["deep/a/b/c/leaf", "outer"]


def test_nested_scan_skips_a_vendored_upstream_clone(tmp_path):
    """The one exclusion `discover_repos` applies that costs no git calls, so
    this count and what `--workspace` walks agree about it too."""
    ws = tmp_path / "ws"
    _git_repo(ws / "mine")
    _git_repo(ws / "module-federation" / "upstream")

    found = [p.relative_to(ws).as_posix() for p in _nested_repo_dirs(ws)]
    assert found == ["mine"]


def test_depth_phrase_reports_a_range_only_when_there_is_one():
    assert _depth_phrase({2}) == "at depth 2"
    assert _depth_phrase({1, 2}) == "at depths 1-2"


def test_index_reports_the_true_nested_count_and_its_depths(tmp_path, logs):
    """The message a reader acts on. Exit code is deliberately not asserted
    here: F4 owns whether this refuses, this test owns the count."""
    from contextlake.kb.commands import cmd_index

    ws = _fleet(tmp_path)
    store_dir = tmp_path / "kb"
    store_dir.mkdir()

    cmd_index(_args(store_dir, ws))
    warned = [m for m in logs if "isn't itself a git repo" in m]
    assert warned, "a directory holding repos must say so"
    assert "3 git working tree(s) at depths 1-2" in warned[0]
