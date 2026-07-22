"""Tests for update behaviour, incl. bug #5 (failed pulls reported as success)."""

from conftest import FakeCompleted, make_local_repo
from contextlake import core
from contextlake.core import update_repositories, update_repository


def _safe(monkeypatch, safe=True, warnings=None):
    monkeypatch.setattr(core, "check_repository_safety", lambda *a, **k: (safe, warnings or []))


def _branch_main(cmd):
    return "rev-parse" in cmd and "--abbrev-ref" in cmd


def test_failed_merge_reports_error_not_uptodate(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """Bug #5: a non-zero fast-forward must be an error, never 'Already up to date'."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            return FakeCompleted(returncode=1, stderr="fatal: refusing to merge unrelated histories")  # noqa: E501
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeCompleted(stdout="aaa")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "error"
    assert "unrelated histories" in msg


def test_diverged_branch_is_skipped(tmp_path, base_config, fake_subprocess, monkeypatch):
    """A branch that diverged from origin is skipped cleanly (no merge/rebase)."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="dev")
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            return FakeCompleted(returncode=1, stderr="fatal: Not possible to fast-forward, aborting.")  # noqa: E501
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeCompleted(stdout="aaa")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "Diverged" in msg


def test_deleted_upstream_branch_is_skipped(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """A deleted upstream branch is reported as a clean skip, not a fatal error."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="feature/gone")
        if cmd[:2] == ["git", "fetch"]:
            return FakeCompleted(returncode=1, stderr="fatal: couldn't find remote ref feature/gone")  # noqa: E501
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "deleted" in msg.lower()


def test_deleted_upstream_branch_carries_remediation_hint(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """H2: the one place a failure class is already distinguished in the code
    (missing-ref) gets a one-line remediation hint pointing at the fix."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="feature/gone")
        if cmd[:2] == ["git", "fetch"]:
            return FakeCompleted(returncode=1, stderr="fatal: couldn't find remote ref feature/gone")  # noqa: E501
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "branches" in msg.lower()  # points at the remediation (the branches verb)


def test_transient_fetch_error_is_retried(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """A transient 'unexpected eof' fetch drop is retried, then the update succeeds."""
    _safe(monkeypatch)
    heads = iter(["before", "after"])
    fetches = {"n": 0}

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd[:2] == ["git", "fetch"]:
            fetches["n"] += 1
            if fetches["n"] == 1:  # first attempt drops, second succeeds
                return FakeCompleted(returncode=1, stderr="TLS ... unexpected eof while reading")
            return FakeCompleted()
        if cmd == ["git", "rev-parse", "HEAD"]:
            return FakeCompleted(stdout=next(heads))
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = update_repository("a", str(tmp_path), base_config)
    assert status == "ok"
    assert fetches["n"] == 2  # retried exactly once


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
    # A dirty working tree is the only thing that makes a repo unsafe to update.
    _safe(monkeypatch, safe=False, warnings=["Uncommitted changes detected"])
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "unsafe" in msg


def test_branch_read_failure_is_error_not_empty_fetch(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """A failed branch read must surface as an error, not proceed with branch ''."""
    _safe(monkeypatch)
    fake_subprocess.handler = lambda cmd, **k: (
        FakeCompleted(returncode=128, stderr="fatal: not a git repository")
        if _branch_main(cmd) else FakeCompleted()
    )
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "error"
    assert "not a git repository" in msg


def test_rev_parse_failure_is_error_not_nochange(
    tmp_path, base_config, fake_subprocess, monkeypatch
):
    """If the before/after HEAD read fails, report an error -- never a silent
    'nochange' from two empty strings comparing equal."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="main")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return FakeCompleted(returncode=128, stderr="fatal: bad object HEAD")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "error"
    assert "bad object" in msg


def test_clean_feature_branch_is_updated(tmp_path, base_config, fake_subprocess, monkeypatch):
    """A clean repo on a feature branch is updated, not skipped by branch name."""
    _safe(monkeypatch)  # check_repository_safety reports safe for a clean tree
    heads = iter(["before", "after"])

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="feature/x")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return FakeCompleted(stdout=next(heads))
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status in ("updated", "ok")
    assert "feature/x" in msg


# --- update_repositories loop: unified status_line rendering ---------------

def test_update_repositories_failure_line_has_fail_glyph(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """H2: an 'error' outcome renders with the fail glyph (not raw git text)."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository", lambda p, wd, cfg: ("error", "r1", "fatal: boom")
    )

    update_repositories(str(tmp_path), base_config)

    text = gls_logs.text
    assert "✗" in text
    assert "fatal: boom" in text


def test_update_repositories_dryrun_line_has_dryrun_glyph(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """The 'dry-run' outcome maps to the 'dryrun' state glyph ('~'), not fail."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository", lambda p, wd, cfg: ("dry-run", "r1", "Would update main")
    )

    update_repositories(str(tmp_path), base_config)

    text = gls_logs.text
    assert "~" in text
    assert "✗" not in text
