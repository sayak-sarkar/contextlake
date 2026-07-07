"""Tests for the git post-commit re-index hook (kb/git_hook.py) and `hook` verb."""
import subprocess

import pytest

from contextlake.kb import git_hook


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_install_creates_executable_hook(tmp_path):
    _repo(tmp_path)
    assert git_hook.install(str(tmp_path), "team/app") == "installed"
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    assert hook.exists()
    body = hook.read_text()
    assert 'index' in body and '--repo "team/app"' in body
    assert git_hook.is_installed(str(tmp_path))


def test_install_is_idempotent(tmp_path):
    _repo(tmp_path)
    git_hook.install(str(tmp_path), "app")
    assert git_hook.install(str(tmp_path), "app") == "refreshed"
    body = (tmp_path / ".git" / "hooks" / "post-commit").read_text()
    assert body.count(git_hook.MARK_BEGIN) == 1   # never duplicated


def test_install_preserves_existing_hook(tmp_path):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho custom\n")
    assert git_hook.install(str(tmp_path), "app") == "appended"
    body = hook.read_text()
    assert "echo custom" in body and git_hook.MARK_BEGIN in body


def test_uninstall_keeps_foreign_hook_but_removes_our_block(tmp_path):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho custom\n")
    git_hook.install(str(tmp_path), "app")
    assert git_hook.uninstall(str(tmp_path)) == "removed"
    body = hook.read_text()
    assert "echo custom" in body and git_hook.MARK_BEGIN not in body


def test_uninstall_deletes_hook_when_ours_only(tmp_path):
    _repo(tmp_path)
    git_hook.install(str(tmp_path), "app")
    assert git_hook.uninstall(str(tmp_path)) == "removed"
    assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()
    assert git_hook.uninstall(str(tmp_path)) == "absent"


def test_not_a_repo(tmp_path):
    assert git_hook.install(str(tmp_path), "app") == "not-a-repo"
    assert not git_hook.is_installed(str(tmp_path))


def test_worktree_gitdir_file(tmp_path):
    # A `.git` *file* pointing at a real gitdir (worktree/submodule shape).
    _repo(tmp_path)
    real = tmp_path / ".git"
    linked = tmp_path / "wt"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {real}\n")
    assert git_hook.install(str(linked), "app") == "installed"
    assert (real / "hooks" / "post-commit").exists()


@pytest.mark.parametrize("action", ["install", "status", "uninstall"])
def test_cmd_hook_dispatch(tmp_path, action, monkeypatch):
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    if action != "install":
        git_hook.install(str(tmp_path), tmp_path.name)
    args = build_parser().parse_args(["hook", action])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0
