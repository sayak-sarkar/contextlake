"""Tests for branch selection strategies and protection."""

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import select_most_active_branch, switch_repository_branch

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
