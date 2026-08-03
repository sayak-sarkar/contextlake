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


def test_hook_body_uses_the_namespaced_command(tmp_path):
    """The hook lands in a file contextlake never revisits, and it backgrounds
    itself -- so a wrong command here fails with no output at all, and the only
    symptom is a graph that quietly stops tracking HEAD."""
    _repo(tmp_path)
    git_hook.install(str(tmp_path), "team/app")
    body = (tmp_path / ".git" / "hooks" / "post-commit").read_text()
    assert "contextlake kb index" in body
    assert "( contextlake index" not in body


def test_install_rewrites_a_hook_carrying_the_pre_namespace_syntax(tmp_path):
    """Self-healing for hooks written by an older contextlake: re-running
    `hook install` refreshes the managed block in place, so a user who follows
    the migration guide is fixed everywhere without hand-editing .git/hooks."""
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        f"{git_hook.MARK_BEGIN}\n"
        '( contextlake index "/old/path" --repo "team/app" >/dev/null 2>&1 & ) </dev/null\n'
        f"{git_hook.MARK_END}\n")

    assert git_hook.install(str(tmp_path), "team/app") == "refreshed"
    body = hook.read_text()
    assert "contextlake kb index" in body
    assert '( contextlake index "/old/path"' not in body
    assert body.count(git_hook.MARK_BEGIN) == 1


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
    args = build_parser().parse_args(["kb", "hook", action])
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


def test_hook_install_warns_on_unresolved_repo_id(tmp_path, monkeypatch, gls_logs):
    """A bad --config used to be silently swallowed by _canonical_repo_id's bare
    except, install a hook wired to the bare directory name instead of the repo's
    real stored id, and never tell the user -- so `hook install` reported a clean
    success while quietly mis-wiring (or permanently inert-ing) the hook."""
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    repo = tmp_path / "widget-api"
    _repo(repo)
    monkeypatch.chdir(repo)

    args = build_parser().parse_args(["kb", "hook", "install", "--config", "/does/not/exist.toml"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0  # the fallback still installs *something*

    raw = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "Could not resolve this repo's stored id" in raw
    hook = repo / ".git" / "hooks" / "post-commit"
    assert f'--repo "{repo.name}"' in hook.read_text()  # fell back to the dir name, as before


def test_cmd_hook_status_shows_dim_dot_when_not_installed(tmp_path, monkeypatch, gls_logs):
    """H3: the 'not installed' glyph must come from style.dim('·'), not a bare '·'."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    args = build_parser().parse_args(["kb", "hook", "status"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0
    # gls_logs.text is ANSI-stripped by pytest's LogCaptureHandler itself, so
    # read the raw record messages (log()'s actual argument) to see the codes.
    raw = "\n".join(r.getMessage() for r in gls_logs.records)
    assert f"\033[2m·\033[0m {tmp_path.name}" in raw
