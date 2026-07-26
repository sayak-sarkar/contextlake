"""Tests for branch selection strategies and protection."""

from conftest import FakeCompleted, make_local_repo
from contextlake import core
from contextlake.core import (
    select_most_active_branch,
    switch_repository_branch,
    switch_repository_branches,
)

PROJECTS = {"a": {"archived": False, "http": "h", "ssh": "s", "default_branch": "main"}}


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
        "origin/HEAD|2026-06-10 12:00:00 +0000|abc0",
        "origin/main|2026-06-09 12:00:00 +0000|abc1",
        "origin/release/HEAD-fix|2026-06-08 12:00:00 +0000|abc2",
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


def _switch_handler(current="dev", branches=("origin/main", "origin/dev")):
    foreach = "\n".join(f"{b}|2026-06-10 12:00:00 +0000|abc{i}" for i, b in enumerate(branches))

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


def test_switch_repository_branch_empty_repo_is_a_clean_skip(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """A repo with no commits yet (git: "ambiguous argument 'HEAD'") has no branch
    to switch to -- must be a skip, with the same _git_reason-derived message
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
    assert status == "skip"
    assert msg == "No commits yet (empty repository)"


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
    assert "contextlake branches --repos" in text


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
