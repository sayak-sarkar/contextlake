"""Tests for branch selection strategies and protection."""

import os
import subprocess

from conftest import FakeCompleted, make_local_repo
from contextlake import core
from contextlake.cli import apply_cli_overrides, build_parser
from contextlake.core import (
    select_most_active_branch,
    switch_repository_branch,
    switch_repository_branches,
)

PROJECTS = {"a": {"archived": False, "http": "h", "ssh": "s", "default_branch": "main"}}
PROJECTS_REPO = {"repo": PROJECTS["a"]}


def _info(name, count, ts):
    return {"name": name, "count": count, "ts": ts}


def test_strategy_commits_picks_highest_count():
    info = [_info("main", 100, 1000), _info("dev", 500, 10)]
    assert select_most_active_branch(info, "commits") == "dev"


def test_strategy_recency_picks_newest():
    info = [_info("main", 100, 1000), _info("dev", 500, 10)]
    assert select_most_active_branch(info, "recency") == "main"


def test_strategy_hybrid_balances_count_and_recency():
    # dev has far more commits; main is only slightly newer -> hybrid favours dev.
    info = [_info("main", 100, 1000), _info("dev", 5000, 900)]
    assert select_most_active_branch(info, "hybrid") == "dev"


def test_empty_branch_info_returns_none():
    assert select_most_active_branch([], "hybrid") is None


def test_collect_branch_info_drops_only_the_symbolic_head_ref(monkeypatch):
    """The `origin/HEAD` symbolic ref must be excluded by an EXACT match, not a
    substring one -- a real branch merely named e.g. `release/HEAD-fix` must
    not be silently dropped from consideration."""
    foreach_out = "\n".join([
        "refs/remotes/origin/HEAD|2026-06-10 12:00:00 +0000|abc0",
        "refs/remotes/origin/main|2026-06-09 12:00:00 +0000|abc1",
        "refs/remotes/origin/release/HEAD-fix|2026-06-08 12:00:00 +0000|abc2",
    ])

    def fake_run(cmd, **kwargs):
        if "for-each-ref" in cmd:
            return FakeCompleted(stdout=foreach_out)
        if "rev-list" in cmd:
            return FakeCompleted(stdout="10")
        return FakeCompleted()

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    info = core._collect_branch_info("/repo", branch_timeout=5)
    names = {b["name"] for b in info}
    assert names == {"main", "release/HEAD-fix"}


def _real_clone(tmp_path, monkeypatch):
    """A clone of a local bare origin that has the branches `main` and `hotfix/origin/x`.

    `main` has 2 commits and is the newest. `hotfix/origin/x` stops at the first commit.
    The branch name holds `origin/` on purpose, and the dates are set, so no two refs
    tie on a timestamp by chance. The clone is on `main`.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    ident = ("-c", "user.email=t@example.invalid", "-c", "user.name=t")

    def git(cwd, *args, date=None):
        env = {**os.environ, "GIT_COMMITTER_DATE": date} if date else None
        return subprocess.run(["git", *ident, *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout

    origin, seed, work = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    work.mkdir()
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for n, date in ((1, "2026-01-01T00:00:00Z"), (2, "2026-02-01T00:00:00Z")):
        (seed / "f.txt").write_text(f"{n}\n")
        git(seed, "add", "f.txt")
        git(seed, "commit", "-qm", f"c{n}", date=date)
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main", "HEAD~1:refs/heads/hotfix/origin/x")
    git(tmp_path, "clone", "-q", str(origin), str(work / "repo"))
    return work, work / "repo", git


def test_collect_branch_info_on_a_real_clone_lists_only_real_branches(tmp_path, monkeypatch):
    """A fresh clone has `refs/remotes/origin/HEAD`. Git shortens it to `origin`, not to
    `origin/HEAD`, so it must not be listed as a branch named `origin`. A branch name that
    holds `origin/` keeps it."""
    _, repo, _ = _real_clone(tmp_path, monkeypatch)

    info = core._collect_branch_info(str(repo), branch_timeout=30)

    assert sorted((b["name"], b["count"]) for b in info) == [("hotfix/origin/x", 1), ("main", 2)]


def test_recency_strategy_keeps_a_clone_on_main_when_main_is_newest(
        tmp_path, base_config, monkeypatch):
    """With `branch_strategy = recency`, the default branch holds the newest commit, so the
    run stays on it. A phantom `origin` branch tied with it, won, and detached HEAD."""
    work, repo, git = _real_clone(tmp_path, monkeypatch)
    cfg = {**base_config, "branch_strategy": "recency", "max_retries": "1"}

    for _ in range(2):  # the second run is the one that read "on working branch: HEAD"
        status, _, msg = switch_repository_branch("repo", PROJECTS_REPO, str(work), cfg)
        assert (status, msg) == ("ok", "Already on main"), (status, msg)
        assert git(repo, "branch", "--show-current").strip() == "main"


def _switch_handler(current="dev", branches=("origin/main", "origin/dev")):
    foreach = "\n".join(f"refs/remotes/{b}|2026-06-10 12:00:00 +0000|abc{i}"
                        for i, b in enumerate(branches))

    def handler(cmd, **kwargs):
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return FakeCompleted(stdout=current)
        if "for-each-ref" in cmd:
            return FakeCompleted(stdout=foreach)
        if "rev-list" in cmd:
            return FakeCompleted(stdout="10")
        return FakeCompleted()

    return handler


def test_switch_repository_branch_deleted_project_is_a_clean_skip(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """A deleted/access-revoked upstream project must not inflate the run's
    error count here either -- update_repository already treats this as a
    clean skip; switch_repository_branch's own `git fetch --all` hits the
    exact same origin and must be consistent, not report a generic error."""
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))

    def handler(cmd, **kwargs):
        if "fetch" in cmd:
            return FakeCompleted(
                returncode=1,
                stderr="fatal: repository 'https://gitlab.example.com/g/p.git/' not found: "
                       "The project you were looking for could not be found or "
                       "you don't have permission to view it.",
            )
        return FakeCompleted()

    fake_subprocess.handler = handler
    cfg = base_config.copy()
    cfg["max_retries"] = "1"
    result = switch_repository_branch("a", PROJECTS, str(tmp_path), cfg)
    assert result[0] == "skip"
    assert "deleted or access revoked" in result[2]


def test_switch_repository_branch_empty_repo_is_a_note(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """A repo with no commits yet (git: "ambiguous argument 'HEAD'") has no branch
    to switch to -- this describes what the repo IS, not a failure or something
    withheld, so it's a "note", with the same _git_reason-derived message
    update_repository now gives the identical condition, not a hardcoded string
    that could drift out of sync with it."""
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))

    def handler(cmd, **kwargs):
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return FakeCompleted(
                returncode=128,
                stderr="fatal: ambiguous argument 'HEAD': unknown revision or path not "
                       "in the working tree.")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = switch_repository_branch("a", PROJECTS, str(tmp_path), base_config)
    assert status == "note"
    assert msg == "New repo -- no commits yet"


def test_protected_working_branch_is_skipped(tmp_path, base_config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))
    monkeypatch.setattr(core, "is_safe_branch", lambda b, c: b in ("main", "master"))
    fake_subprocess.handler = _switch_handler(current="feature/x")
    cfg = base_config.copy()
    cfg["protect_working_branches"] = "true"
    status, _, msg = switch_repository_branch("a", PROJECTS, str(tmp_path), cfg)
    assert status == "skip"
    assert "working branch" in msg


def test_dry_run_does_not_checkout(tmp_path, base_config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))
    monkeypatch.setattr(core, "is_safe_branch", lambda b, c: True)
    # Start on a different (safe) branch so a switch is actually warranted.
    fake_subprocess.handler = _switch_handler(current="master")
    cfg = base_config.copy()
    cfg.update(dry_run="true", branch_strategy="commits")
    status, _, msg = switch_repository_branch("a", PROJECTS, str(tmp_path), cfg)
    assert status == "dry-run"
    assert not fake_subprocess.commands_matching("git", "checkout")


# --- switch_repository_branches loop: unified status_line rendering --------

def test_switch_repository_branches_switched_line_has_switched_glyph(
    tmp_path, base_config, monkeypatch, gls_logs
):
    make_local_repo(tmp_path, "a")
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    monkeypatch.setattr(
        core, "switch_repository_branch",
        lambda p, proj, wd, cfg: ("switched", "a", "main -> dev"),
    )

    switch_repository_branches(str(tmp_path), base_config, "g")

    text = gls_logs.text
    assert "↝" in text
    assert "main -> dev" in text


def test_switch_repository_branches_failure_line_has_fail_glyph(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """H2: an 'error' outcome renders with the fail glyph (not raw git text)."""
    make_local_repo(tmp_path, "a")
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    monkeypatch.setattr(
        core, "switch_repository_branch",
        lambda p, proj, wd, cfg: ("error", "a", "checkout failed"),
    )

    switch_repository_branches(str(tmp_path), base_config, "g")

    text = gls_logs.text
    assert "✗" in text
    assert "checkout failed" in text


def test_switch_repository_branches_summary_names_the_retry_command_on_failure(
    tmp_path, base_config, monkeypatch, gls_logs
):
    make_local_repo(tmp_path, "a")
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    monkeypatch.setattr(
        core, "switch_repository_branch",
        lambda p, proj, wd, cfg: ("error", "a", "checkout failed"),
    )

    switch_repository_branches(str(tmp_path), base_config, "g")

    text = gls_logs.text
    assert "Failed:" in text
    assert "contextlake mirror branches --repos" in text


def test_switch_repository_branches_summary_line_warns_on_partial_failure(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """The final 'Branch switch complete: ...' summary must not keep its green
    checkmark when a repo failed -- index/embed/wiki already swap to the warn
    glyph on partial failure; branches previously didn't."""
    make_local_repo(tmp_path, "a")
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    monkeypatch.setattr(
        core, "switch_repository_branch",
        lambda p, proj, wd, cfg: ("error", "a", "checkout failed"),
    )

    switch_repository_branches(str(tmp_path), base_config, "g")

    summary = [ln for ln in gls_logs.text.splitlines() if "Branch switch complete" in ln][0]
    assert "⚠" in summary
    assert "✓" not in summary


def test_unpinned_summary_does_not_say_the_repo_stayed_put(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """A repo on a safe branch with no pinned branch is checked out to its most active
    one (`develop -> main`), so the summary may not claim it "stayed on" that branch."""
    make_local_repo(tmp_path, "a")
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    monkeypatch.setattr(
        core, "switch_repository_branch",
        lambda p, proj, wd, cfg: ("unpinned", "a",
                                  "develop -> main (no branch 'release/24.1' here)"),
    )
    cfg = dict(base_config, branch="release/24.1")

    switch_repository_branches(str(tmp_path), cfg, "g")
    line = [ln for ln in gls_logs.text.splitlines() if "have no branch" in ln][0]
    assert "stayed on" not in line
    assert "'release/24.1'; each is on its most active branch instead" in line

    gls_logs.clear()
    switch_repository_branches(str(tmp_path), dict(cfg, dry_run="true"), "g")
    line = [ln for ln in gls_logs.text.splitlines() if "have no branch" in ln][0]
    assert "each would be on its most active branch instead" in line


def test_branches_honours_the_repos_filter(tmp_path, base_config, monkeypatch):
    """Same defect as update: --repos was accepted and ignored, while the
    failure hint this command prints recommends exactly that flag."""
    for name in ("blog", "api"):
        make_local_repo(tmp_path, name)
    project = {"archived": False, "http": "h", "ssh": "s", "default_branch": "main"}
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda c, g: {"blog": dict(project), "api": dict(project)})
    seen = []

    def record(path, projects, work_dir, cfg):
        seen.append(path)
        return ("ok", path, "Already")

    monkeypatch.setattr(core, "switch_repository_branch", record)

    cfg = base_config.copy()
    cfg["repo_filter"] = "blog"
    switch_repository_branches(str(tmp_path), cfg, "grp")

    assert seen == ["blog"]


def test_protect_working_branches_spelled_yes_still_protects(
        tmp_path, base_config, fake_subprocess, monkeypatch):
    """`yes` read as false, so `branches` moved a clone off its working branch."""
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (True, []))
    monkeypatch.setattr(core, "is_safe_branch", lambda b, c: b in ("main", "master"))
    fake_subprocess.handler = _switch_handler(current="feature/x")
    cfg = apply_cli_overrides(build_parser().parse_args(["mirror", "branches"]),
                              {**base_config, "protect_working_branches": "yes"})
    status, _, msg = switch_repository_branch("a", PROJECTS, str(tmp_path), cfg)
    assert status == "skip"
    assert "working branch" in msg
    assert not fake_subprocess.commands_matching("checkout")
