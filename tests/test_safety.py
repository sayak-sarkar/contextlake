"""Tests for the branch-safety helpers."""

import subprocess
import types

import pytest

from conftest import FakeCompleted
from contextlake import safety


@pytest.fixture
def fake_safety_subprocess(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return run.handler(list(cmd), **kwargs)

    run.handler = lambda cmd, **k: FakeCompleted()
    run.calls = calls
    monkeypatch.setattr(safety, "subprocess", types.SimpleNamespace(run=run))
    return run


def test_is_safe_branch():
    cfg = {"safe_branches": "main,master,develop"}
    assert safety.is_safe_branch("main", cfg)
    assert not safety.is_safe_branch("feature/x", cfg)
    assert not safety.is_safe_branch("HEAD", cfg)
    assert not safety.is_safe_branch(None, cfg)


def test_is_safe_branch_tolerates_whitespace_in_config():
    # A natural "main, master, develop" (spaces after commas) must still match each
    # branch -- otherwise every entry but the first is silently treated as unsafe.
    cfg = {"safe_branches": "main, master , develop"}
    assert safety.is_safe_branch("master", cfg)
    assert safety.is_safe_branch("develop", cfg)


def test_has_uncommitted_changes(fake_safety_subprocess):
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout=" M file.py\n")
    assert safety.has_uncommitted_changes("/repo")
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout="")
    assert not safety.has_uncommitted_changes("/repo")


def test_has_uncommitted_changes_fails_closed_on_git_error(fake_safety_subprocess):
    """A non-zero git exit (e.g. not a repo) must read as 'dirty', never 'clean':
    treating an unknown tree as safe to modify is what loses local work."""
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(returncode=128,
                                                                     stderr="not a git repo")
    assert safety.has_uncommitted_changes("/repo")


def test_has_uncommitted_changes_fails_closed_on_exception(fake_safety_subprocess):
    """A crashed/timed-out git invocation must also fail closed (unsafe)."""
    def boom(cmd, **k):
        raise OSError("git missing")
    fake_safety_subprocess.handler = boom
    assert safety.has_uncommitted_changes("/repo")


def test_get_current_branch_none_on_error(fake_safety_subprocess):
    """An unreadable branch returns None (the fail-closed value is_safe_branch rejects)."""
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(returncode=128)
    assert safety.get_current_branch("/repo") is None

    def boom(cmd, **k):
        raise OSError("git missing")
    fake_safety_subprocess.handler = boom
    assert safety.get_current_branch("/repo") is None


def test_clean_feature_branch_is_safe(fake_safety_subprocess, tmp_path):
    """Branch name alone must NOT trigger a skip: a clean working tree is safe
    even on a feature branch."""
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout="")  # clean tree
    cfg = {"protect_working_branches": "true", "require_clean_workspace": "true",
           "safe_branches": "main,master"}
    safe, warnings = safety.check_repository_safety("a", str(tmp_path), cfg)
    assert safe
    assert warnings == []


def test_dirty_tree_is_unsafe(fake_safety_subprocess, tmp_path):
    """Only a dirty working tree (uncommitted/unstaged/untracked) makes a repo
    unsafe -- regardless of which branch it is on."""
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout=" M file.py\n")
    cfg = {"protect_working_branches": "true", "require_clean_workspace": "true",
           "safe_branches": "main,master"}
    safe, warnings = safety.check_repository_safety("a", str(tmp_path), cfg)
    assert not safe
    assert any("Uncommitted changes" in w for w in warnings)


def test_require_clean_workspace_off_allows_dirty(fake_safety_subprocess, tmp_path):
    """With require_clean_workspace disabled, even a dirty tree is allowed."""
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout=" M file.py\n")
    cfg = {"require_clean_workspace": "false"}
    safe, warnings = safety.check_repository_safety("a", str(tmp_path), cfg)
    assert safe
    assert warnings == []


def test_stash_disabled_returns_false(fake_safety_subprocess):
    ok, msg, sha = safety.stash_changes("/repo", {"auto_stash": "false"})
    assert not ok and "disabled" in msg
    assert sha is None


def test_stash_runs_when_enabled(fake_safety_subprocess):
    stashed = ["", "deadbeef"]  # refs/stash: empty before the push, populated after

    def handler(cmd, **k):
        if "rev-parse" in cmd:
            return FakeCompleted(stdout=stashed.pop(0) if stashed else "deadbeef")
        return FakeCompleted(returncode=0)

    fake_safety_subprocess.handler = handler
    ok, _, sha = safety.stash_changes("/repo", {"auto_stash": "true"})
    assert ok
    assert sha == "deadbeef"
    assert any("stash" in " ".join(c) for c in fake_safety_subprocess.calls)


# --- the stash round trip, against a real git repo --------------------------

def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def dirty_repo(tmp_path):
    """A real repo with one commit and one uncommitted edit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "file.txt").write_text("committed\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "initial")
    (repo / "file.txt").write_text("committed\nlocal edit\n")
    return repo


def test_stash_then_restore_puts_the_edit_back(dirty_repo):
    """The round trip is the whole point: an auto-stash that is never popped
    takes the user's work out of the working tree with nothing on screen."""
    ok, _, sha = safety.stash_changes(str(dirty_repo), {"auto_stash": "true"})
    assert ok and sha
    assert "local edit" not in (dirty_repo / "file.txt").read_text()

    restored, msg = safety.restore_stash(str(dirty_repo), sha)
    assert restored, msg
    assert "local edit" in (dirty_repo / "file.txt").read_text()
    assert _git(dirty_repo, "stash", "list").stdout.strip() == ""


def test_stash_reports_nothing_stashed_for_untracked_only(tmp_path):
    """`git stash push` exits 0 having taken nothing when only untracked files
    are dirty, so an exit code alone cannot be trusted to mean 'stashed'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "tracked.txt").write_text("x\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    (repo / "new.txt").write_text("untracked\n")

    ok, msg, sha = safety.stash_changes(str(repo), {"auto_stash": "true"})
    assert not ok
    assert sha is None
    assert "untracked" in msg
    assert (repo / "new.txt").exists()


def test_restore_refuses_when_another_stash_is_on_top(dirty_repo):
    """Popping takes whatever is on top, so a stash pushed after ours must block
    the pop -- restoring the wrong changes is worse than leaving ours parked."""
    ok, _, sha = safety.stash_changes(str(dirty_repo), {"auto_stash": "true"})
    assert ok
    (dirty_repo / "file.txt").write_text("committed\nsomeone else's edit\n")
    _git(dirty_repo, "stash", "push", "-m", "not-ours")

    restored, msg = safety.restore_stash(str(dirty_repo), sha)
    assert not restored
    assert "stash" in msg
    assert len(_git(dirty_repo, "stash", "list").stdout.strip().splitlines()) == 2
