"""Tests for the branch-safety helpers."""

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


def test_has_uncommitted_changes(fake_safety_subprocess):
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout=" M file.py\n")
    assert safety.has_uncommitted_changes("/repo")
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(stdout="")
    assert not safety.has_uncommitted_changes("/repo")


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
    ok, msg = safety.stash_changes("/repo", {"auto_stash": "false"})
    assert not ok and "disabled" in msg


def test_stash_runs_when_enabled(fake_safety_subprocess):
    fake_safety_subprocess.handler = lambda cmd, **k: FakeCompleted(returncode=0)
    ok, _ = safety.stash_changes("/repo", {"auto_stash": "true"})
    assert ok
    assert any("stash" in " ".join(c) for c in fake_safety_subprocess.calls)
