"""`update` must not move a repo off a branch that is not on origin.

When the tracked branch is missing on origin, ``update`` re-selects the most
active origin branch and switches to it. A branch that was never pushed is also
"missing on origin", so the repo was moved off the user's local work, and an
``--auto-stash`` popped the stash onto the new branch.

These tests use real git repos under ``tmp_path``: a bare origin and a clone.
"""

import subprocess

import pytest

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import update_repository

IDENT = ("-c", "user.email=t@example.invalid", "-c", "user.name=t")


def _git(cwd, *args):
    return subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout


def _make(tmp_path, branch, *, local_commits=0, dirty=False, deleted_upstream=False):
    """A clone of a bare origin, checked out on ``branch``.

    ``origin`` has ``main`` with 3 commits, so ``main`` is the most active branch.
    ``branch`` exists only locally, unless ``deleted_upstream`` is set. Then it was
    pushed at the first commit and removed from origin afterwards, which is the
    "merged and deleted" case the re-select was written for. The clone keeps its
    stale ``origin/<branch>`` ref, as it does after a plain ``git fetch``.

    Returns ``(work_dir, repo_dir)``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    for n in (1, 2, 3):
        (seed / "file.txt").write_text(f"base {n}\n")
        _git(seed, "add", "file.txt")
        _git(seed, "commit", "-qm", f"base {n}")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    if deleted_upstream:
        _git(seed, "push", "-q", "origin", f"HEAD~2:refs/heads/{branch}")
    subprocess.run(["git", "clone", "-q", str(origin), str(work / "repo")], check=True)
    repo = work / "repo"

    if deleted_upstream:
        _git(repo, "checkout", "-q", "-b", branch, f"origin/{branch}")
        _git(origin, "branch", "-D", branch)
    else:
        _git(repo, "checkout", "-q", "-b", branch)
    for n in range(local_commits):
        (repo / f"wip{n}.txt").write_text("unpushed\n")
        _git(repo, "add", f"wip{n}.txt")
        _git(repo, "commit", "-qm", f"local-only commit {n}")
    if dirty:
        (repo / "file.txt").write_text("base 3\nuncommitted edit\n")
    return work, repo


@pytest.fixture
def cfg(base_config):
    base_config.update(max_retries="1", auto_stash="true")
    return base_config


def _update(work, cfg):
    return update_repository("repo", str(work), cfg)


def _branch(repo):
    return _git(repo, "branch", "--show-current").strip()


def test_unpushed_feature_branch_stays_and_the_stash_returns_to_it(tmp_path, cfg):
    """The reported case: a local-only branch with a commit and an uncommitted edit.

    The edit must be back on the branch it came from, and the commit still there."""
    work, repo = _make(tmp_path, "feature/wip", local_commits=1, dirty=True)

    status, _, msg = _update(work, cfg)

    assert status == "skip", msg
    assert _branch(repo) == "feature/wip"
    assert "uncommitted edit" in (repo / "file.txt").read_text()
    assert _git(repo, "stash", "list").strip() == ""
    assert _git(repo, "log", "-1", "--format=%s").strip() == "local-only commit 0"


@pytest.mark.parametrize("branch, protect", [("develop", "true"), ("feature/x", "false")])
def test_unpushed_commits_hold_a_branch_even_when_protection_does_not_apply(
        tmp_path, cfg, branch, protect):
    """`develop` is in safe_branches, and protection is off for the second case, so
    only the unpushed-commit check can hold the repo here. Clean tree, no stash."""
    cfg["protect_working_branches"] = protect
    work, repo = _make(tmp_path, branch, local_commits=2)

    status, _, msg = _update(work, cfg)

    assert status == "skip", msg
    assert "2 commit(s) not on any origin branch" in msg
    assert _branch(repo) == branch


def test_a_feature_branch_with_nothing_at_risk_switches_under_default_protection(
        tmp_path, cfg):
    """The name-based protection is `branches`' rule, not update's. A branch the mirror
    tracked that is gone upstream (merged and deleted, the usual case) has no commits of
    its own and a clean tree here, so moving strands nothing."""
    work, repo = _make(tmp_path, "feature/x")

    status, _, msg = _update(work, cfg)

    assert status == "switched", msg
    assert _branch(repo) == "main"


def test_turning_protection_off_lets_a_branch_with_nothing_unpushed_switch(tmp_path, cfg):
    """The flag is read, not ignored: nothing is unpushed and no stash is held."""
    cfg["protect_working_branches"] = "false"
    work, repo = _make(tmp_path, "feature/x")

    status, _, msg = _update(work, cfg)

    assert status == "switched", msg
    assert _branch(repo) == "main"


def test_a_held_stash_pops_onto_the_branch_it_came_from(tmp_path, cfg):
    """A safe-named branch with nothing unpushed would otherwise switch, and the
    edit would pop onto `main`. A held stash keeps the branch where it is."""
    work, repo = _make(tmp_path, "develop", dirty=True, deleted_upstream=True)

    status, _, msg = _update(work, cfg)

    assert status == "skip", msg
    assert "auto-stashed" in msg
    assert _branch(repo) == "develop"
    assert "uncommitted edit" in (repo / "file.txt").read_text()
    assert _git(repo, "stash", "list").strip() == ""


def test_a_branch_deleted_upstream_still_switches_when_nothing_is_at_risk(tmp_path, cfg):
    """The case the re-select exists for stays working: a pushed branch that is gone
    from origin, a clean tree, nothing unpushed."""
    work, repo = _make(tmp_path, "develop", deleted_upstream=True)

    status, _, msg = _update(work, cfg)

    assert status == "switched", msg
    assert _branch(repo) == "main"


# --- the unpushed-commit check fails closed ---------------------------------

def _fake_branch_gone(fake_subprocess, on_count):
    """origin lacks the branch (the narrow fetch fails) and has `main`; ``on_count``
    answers the unpushed-commit count."""
    def handler(cmd, **kwargs):
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return FakeCompleted(stdout="develop")
        if cmd[:3] == ["git", "fetch", "--all"]:
            return FakeCompleted()
        if cmd[:2] == ["git", "fetch"]:
            return FakeCompleted(returncode=1,
                                 stderr="fatal: couldn't find remote ref develop")
        if "for-each-ref" in cmd:
            return FakeCompleted(stdout="origin/main|2026-06-10 12:00:00 +0000|abc0")
        if "--remotes=origin" in cmd:
            return on_count()
        if "rev-list" in cmd:
            return FakeCompleted(stdout="10")
        return FakeCompleted()
    fake_subprocess.handler = handler


def _timeout():
    raise subprocess.TimeoutExpired("git", 30)


@pytest.mark.parametrize("on_count", [
    lambda: FakeCompleted(returncode=128, stderr="fatal: bad revision"),
    lambda: FakeCompleted(stdout="not a number"),
    _timeout,
], ids=["git-fails", "unparseable", "timeout"])
def test_unreadable_unpushed_count_leaves_the_branch_alone(
        tmp_path, cfg, fake_subprocess, no_sleep, monkeypatch, on_count):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))
    _fake_branch_gone(fake_subprocess, on_count)

    status, _, msg = _update(tmp_path, cfg)

    assert status == "skip", msg
    assert "could not check" in msg
    assert not fake_subprocess.commands_matching("checkout")
