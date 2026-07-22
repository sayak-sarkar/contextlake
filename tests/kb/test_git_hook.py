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
def test_cmd_hook_dispatch(tmp_path, action, monkeypatch, gls_logs):
    # FORCE_COLOR makes the "status" assertion below discriminating: a bare "✓"
    # (the old code) would not carry the ANSI codes asserted, so this fails
    # against the pre-fix code and passes against the fix -- unlike a plain-text
    # glyph check, which is identical either way.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
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

    if action == "status":
        # H3: the per-repo status glyph must come from style.ok(), not a bare "✓".
        # gls_logs.text is ANSI-stripped by pytest's LogCaptureHandler itself, so
        # read the raw record messages (log()'s actual argument) to see the codes.
        raw = "\n".join(r.getMessage() for r in gls_logs.records)
        assert f"\033[32m✓\033[0m {tmp_path.name}" in raw


def test_cmd_hook_status_shows_dim_dot_when_not_installed(tmp_path, monkeypatch, gls_logs):
    """H3: the 'not installed' glyph must come from style.dim('·'), not a bare '·'."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    args = build_parser().parse_args(["hook", "status"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0
    # gls_logs.text is ANSI-stripped by pytest's LogCaptureHandler itself, so
    # read the raw record messages (log()'s actual argument) to see the codes.
    raw = "\n".join(r.getMessage() for r in gls_logs.records)
    assert f"\033[2m·\033[0m {tmp_path.name}" in raw
