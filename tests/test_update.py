"""Tests for update behaviour, incl. bug #5 (failed pulls reported as success)."""

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import update_repository


def _safe(monkeypatch, safe=True, warnings=None):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (safe, warnings or []))


def _branch_main(cmd):
    return "rev-parse" in cmd and "--abbrev-ref" in cmd


def test_failed_pull_reports_error_not_uptodate(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """Bug #5: a non-zero `git pull` must be an error, never 'Already up to date'."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd[:2] == ["git", "pull"]:
            return FakeCompleted(returncode=1, stderr="merge conflict")
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeCompleted(stdout="aaa")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "error"
    assert "conflict" in msg


def test_nochange_when_head_unmoved(tmp_path, base_config, fake_subprocess, monkeypatch):
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeCompleted(stdout="samehash")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = update_repository("a", str(tmp_path), base_config)
    assert status == "nochange"


def test_updated_when_head_moves(tmp_path, base_config, fake_subprocess, monkeypatch):
    _safe(monkeypatch)
    heads = iter(["before", "after"])

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return FakeCompleted(stdout=next(heads))
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = update_repository("a", str(tmp_path), base_config)
    assert status == "updated" or status == "ok"


def test_detached_head_skipped(tmp_path, base_config, fake_subprocess, monkeypatch):
    _safe(monkeypatch)
    fake_subprocess.handler = lambda cmd, **k: (
        FakeCompleted(stdout="HEAD") if _branch_main(cmd) else FakeCompleted()
    )
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "Detached" in msg


def test_dry_run_skips_pull(tmp_path, base_config, fake_subprocess, monkeypatch):
    _safe(monkeypatch)
    fake_subprocess.handler = lambda cmd, **k: (
        FakeCompleted(stdout="main") if _branch_main(cmd) else FakeCompleted()
    )
    cfg = base_config.copy()
    cfg["dry_run"] = "true"
    status, _, _ = update_repository("a", str(tmp_path), cfg)
    assert status == "dry-run"
    assert not fake_subprocess.commands_matching("git", "pull")


def test_unsafe_repo_skipped(tmp_path, base_config, fake_subprocess, monkeypatch):
    _safe(monkeypatch, safe=False, warnings=["On working branch: feature/x"])
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "unsafe" in msg
