"""Tests for the git post-commit re-index hook (kb/git_hook.py) and `hook` verb."""
import shlex
import stat
import subprocess
import time

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
    # `shlex.quote` leaves a shell-safe word bare; it adds quotes only when one is needed.
    assert 'index' in body and "--repo team/app" in body
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


# --- hooks that git will never run ----------------------------------------------
#
# `install` wrote to <gitdir>/hooks and `is_installed` read the same file, so both said
# "present" while a commit never reached it: core.hooksPath (Husky, lefthook, a global
# guard) sends git to another directory, and an existing hook that ends in `exec` or
# `exit` stops before a block appended after it.

def _cmd_hook(action, path, monkeypatch):
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    monkeypatch.chdir(path)
    args = build_parser().parse_args(["kb", "hook", action])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    return kb.dispatch("hook", args)


def test_a_repo_with_core_hookspath_is_refused_and_nothing_is_written(
        tmp_path, monkeypatch, gls_logs):
    _repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.hooksPath", ".husky/_"],
                   check=True)

    assert git_hook.install(str(tmp_path), "app") == "hooks-path"
    assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()
    assert not git_hook.is_installed(str(tmp_path))

    assert _cmd_hook("install", tmp_path, monkeypatch) == 1
    assert ".husky/_" in gls_logs.text and "core.hooksPath" in gls_logs.text
    assert "Each commit now re-indexes" not in gls_logs.text


def test_a_hook_written_before_core_hookspath_was_set_is_not_reported_present(tmp_path):
    _repo(tmp_path)
    git_hook.install(str(tmp_path), "app")
    assert git_hook.is_installed(str(tmp_path))
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.hooksPath", "elsewhere"],
                   check=True)
    assert not git_hook.is_installed(str(tmp_path))


def test_core_hookspath_naming_the_repos_own_hooks_directory_is_not_a_redirect(tmp_path):
    _repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.hooksPath", ".git/hooks"],
                   check=True)
    assert git_hook.install(str(tmp_path), "app") == "installed"
    assert git_hook.is_installed(str(tmp_path))


def _stub_command(tmp_path, monkeypatch):
    """Make the managed block run a stub that writes a file, and return that file's path.
    The block detaches the command with `&`, so callers wait for the file with
    :func:`_appeared`."""
    from contextlake import launcher

    sentinel = tmp_path / "ran"
    stub = tmp_path / "clstub"
    stub.write_text(f"#!/bin/sh\necho ran > {shlex.quote(str(sentinel))}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(launcher, "launch_command", lambda **k: shlex.quote(str(stub)))
    return sentinel


def _appeared(path, seconds=5):
    """Bounded wait for a file the detached command writes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _run_hook_and_wait(repo, tmp_path, monkeypatch):
    """Install, run the hook with sh from the repo root as git does, and say whether the
    managed block's command ran."""
    sentinel = _stub_command(tmp_path, monkeypatch)
    git_hook.install(str(repo), "app")
    subprocess.run(["sh", str(repo / ".git" / "hooks" / "post-commit")], cwd=repo,
                   check=False, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    return _appeared(sentinel)


@pytest.mark.parametrize("tail", ["exec true\n", "exit 0\n", "exec true\n# done\n\n"])
def test_the_block_goes_above_a_closing_exec_or_exit(tmp_path, monkeypatch, tail):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\necho framework\n" + tail)

    assert _run_hook_and_wait(repo, tmp_path, monkeypatch), (
        "the block sits after the exec/exit, so a commit never reaches it")
    body = hook.read_text()
    assert body.index(git_hook.MARK_BEGIN) < body.index(tail.split("\n")[0])
    assert git_hook.is_installed(str(repo))
    assert git_hook.install(str(repo), "app") == "refreshed"      # stable on a re-run
    assert hook.read_text() == body


def test_an_exec_in_the_middle_of_a_hook_is_refused_and_the_file_is_untouched(tmp_path):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    text = "#!/bin/sh\nexec true\necho never reached\n"
    hook.write_text(text)
    assert git_hook.install(str(tmp_path), "app") == "blocked"
    assert hook.read_text() == text


@pytest.mark.parametrize("shebang", ["#!/usr/bin/env python3", "#!/usr/bin/python",
                                     "#!/usr/bin/env node"])
def test_a_hook_that_is_not_a_shell_script_is_refused_and_the_file_is_untouched(
        tmp_path, shebang):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    text = shebang + "\nprint('hi')\n"
    hook.write_text(text)
    assert git_hook.install(str(tmp_path), "app") == "not-shell"
    assert hook.read_text() == text


@pytest.mark.parametrize("shebang", ["#!/bin/bash", "#!/usr/bin/env bash",
                                     "#!/usr/bin/env -S bash -e", "#!/bin/sh -e",
                                     "#!/bin/zsh"])
def test_shell_shebangs_are_accepted(tmp_path, shebang):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(shebang + "\necho custom\n")
    assert git_hook.install(str(tmp_path), "app") == "appended"


def test_a_block_an_older_version_left_after_an_exec_is_moved_above_it(tmp_path):
    """Older installs appended after the existing hook. Status called that present; a re-run
    of install must repair it, not rewrite it in the same dead spot."""
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho framework\nexec true\n\n"
                    f"{git_hook.MARK_BEGIN}\nold body\n{git_hook.MARK_END}\n")
    assert not git_hook.is_installed(str(tmp_path))
    assert git_hook.install(str(tmp_path), "app") == "refreshed"
    body = hook.read_text()
    assert body.index(git_hook.MARK_BEGIN) < body.index("exec true")
    assert "old body" not in body
    assert git_hook.is_installed(str(tmp_path))


def test_a_marker_in_a_python_hook_is_not_reported_present(tmp_path):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env python3\n" f"{git_hook.MARK_BEGIN}\nx\n{git_hook.MARK_END}\n")
    assert not git_hook.is_installed(str(tmp_path))


def test_cmd_hook_exits_1_and_says_why_when_the_hook_is_refused(tmp_path, monkeypatch,
                                                                gls_logs):
    _repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexec true\necho later\n")
    assert _cmd_hook("install", tmp_path, monkeypatch) == 1
    assert "exec or exit before its end" in gls_logs.text
    assert "Each commit now re-indexes" not in gls_logs.text


# --- linked worktrees ------------------------------------------------------------
#
# A linked worktree's `.git` file names `.git/worktrees/NAME`, but git reads hooks from the
# common dir shared with the main checkout. `install` wrote into the worktree's own
# directory, which git never opens, and `status` read it back as present.

def _commit(repo):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True, capture_output=True)


def _with_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _repo(main)
    _commit(main)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "side"],
                   check=True, capture_output=True)
    return main, wt


def test_a_hook_installed_from_a_linked_worktree_runs_on_a_commit_there(
        tmp_path, monkeypatch):
    main, wt = _with_worktree(tmp_path)
    sentinel = _stub_command(tmp_path, monkeypatch)

    assert git_hook.install(str(wt), "app") == "installed"
    assert (main / ".git" / "hooks" / "post-commit").exists()
    assert not (main / ".git" / "worktrees" / "wt" / "hooks" / "post-commit").exists()
    assert git_hook.is_installed(str(wt)) and git_hook.is_installed(str(main))

    _commit(wt)
    assert _appeared(sentinel), "a commit in the worktree never ran the hook"


def test_a_second_checkout_does_not_repoint_the_shared_block(tmp_path):
    main, wt = _with_worktree(tmp_path)
    git_hook.install(str(main), "app")
    hook = main / ".git" / "hooks" / "post-commit"
    before = hook.read_text()

    assert git_hook.install(str(wt), "app") == "shared"
    assert hook.read_text() == before
    assert git_hook.install(str(main), "app") == "refreshed"


def test_uninstall_from_a_worktree_removes_the_shared_block_and_a_stale_local_one(tmp_path):
    main, wt = _with_worktree(tmp_path)
    git_hook.install(str(wt), "app")
    stale = main / ".git" / "worktrees" / "wt" / "hooks" / "post-commit"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"#!/bin/sh\n{git_hook.MARK_BEGIN}\nold\n{git_hook.MARK_END}\n")

    assert git_hook.uninstall(str(wt)) == "removed"
    assert not (main / ".git" / "hooks" / "post-commit").exists()
    assert not stale.exists()
    assert not git_hook.is_installed(str(wt))


def test_status_warns_instead_of_ok_when_not_every_repo_is_wired(
        tmp_path, monkeypatch, gls_logs):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.hooksPath", "elsewhere"],
                   check=True)
    assert _cmd_hook("status", tmp_path, monkeypatch) == 0
    raw = "\n".join(r.getMessage() for r in gls_logs.records)
    assert "\033[33m⚠\033[0m post-commit hook present on 0/1 repo(s)." in raw
    assert "\033[32m✓\033[0m post-commit hook present" not in raw


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
    # fell back to the dir name, as before
    assert f"--repo {shlex.quote(repo.name)}" in hook.read_text()


def test_hook_install_stores_an_absolute_config_path(tmp_path, monkeypatch, gls_logs):
    """`kb --config kb.toml hook install` wrote `--config kb.toml` into the hook. Git runs
    the hook from the repo root, so every commit then failed with "--config path not found"
    in contextlake-index.log, and install had already said each commit now re-indexes."""
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    repo = tmp_path / "widget-api"
    _repo(repo)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "kb.toml").write_text("")
    monkeypatch.chdir(cfg_dir)

    args = build_parser().parse_args(
        ["kb", "hook", "install", str(repo), "--config", "kb.toml"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0

    body = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert f"--config {shlex.quote(str(cfg_dir / 'kb.toml'))} kb index" in body


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


def test_a_directory_alone_means_install(tmp_path, monkeypatch, gls_logs):
    """August probe #6: `kb hook ./repo` refused with "Unknown hook action", because the
    order is `[action] [path]`. A lone directory now means install (owner, 2026-10-15)."""
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    target = tmp_path / "app"
    target.mkdir()
    _repo(target)
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["kb", "hook", str(target)])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0
    assert (target / ".git" / "hooks" / "post-commit").exists()


def test_an_unknown_word_is_still_refused(tmp_path, monkeypatch, gls_logs):
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["kb", "hook", "instal"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 1
    assert "Unknown hook action 'instal'" in gls_logs.text
    assert "No such directory" not in gls_logs.text
    assert "\u2014" not in gls_logs.text


@pytest.mark.parametrize("word", ["{tmp}/missing/dir", "~/missing/dir", "./missing", ".missing"])
def test_a_missing_path_says_no_such_directory(word, tmp_path, monkeypatch, gls_logs):
    """G-obs3: `kb hook ~/missing/dir` said "Unknown hook action", which blamed the word
    when the real fault is a path that does not exist. A path-shaped word (a separator,
    or a leading `~` or `.`) gets the directory message."""
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    word = word.replace("{tmp}", str(tmp_path))
    args = build_parser().parse_args(["kb", "hook", word])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 1
    assert f"No such directory: {word}" in gls_logs.text
    assert "Unknown hook action" not in gls_logs.text


def test_hook_install_on_an_empty_store_pins_the_origin_id_not_the_directory_name(
        tmp_path, monkeypatch, gls_logs):
    """The hook writes `--repo <id>` into the script. On a store that does not hold the
    repo yet the id was the directory name, which is not what a plain `kb index PATH`
    files the repo under, so the hook and the command made two rows for one checkout."""
    from contextlake.cli import _DEFAULTS, build_parser
    from contextlake.kb import commands as kb

    repo = tmp_path / "gamma"
    _repo(repo)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://git.example.org/team/gamma.git"], check=True)
    monkeypatch.chdir(repo)

    args = build_parser().parse_args(["kb", "hook", "install"])
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    assert kb.dispatch("hook", args) == 0

    hook = repo / ".git" / "hooks" / "post-commit"
    assert "--repo git.example.org/team/gamma" in hook.read_text()
    assert "--repo gamma" not in hook.read_text()
