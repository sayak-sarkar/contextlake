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


def test_deleted_upstream_branch_auto_switches_to_a_new_one(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """A branch deleted upstream (renamed/merged/superseded) is not just reported
    for the user to fix by hand -- update_repository auto-reselects and switches
    to the most-active remaining branch, same selection `branches` would make."""
    _safe(monkeypatch)
    foreach = "origin/main|2026-06-10 12:00:00 +0000|abc0"

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="feature/gone")
        if cmd[:3] == ["git", "fetch", "--all"]:
            return FakeCompleted()  # the broader reselect fetch succeeds
        if cmd[:2] == ["git", "fetch"]:
            # the narrow per-branch fetch is the one that fails with missing-ref
            return FakeCompleted(returncode=1, stderr="fatal: couldn't find remote ref feature/gone")  # noqa: E501
        if "for-each-ref" in cmd:
            return FakeCompleted(stdout=foreach)
        if "rev-list" in cmd:
            return FakeCompleted(stdout="10")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "switched"
    assert "feature/gone" in msg and "main" in msg


def test_deleted_upstream_branch_reselect_failure_still_skips_cleanly(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """If the broader reselect fetch ALSO fails (e.g. total network outage, not
    just the one branch being gone), fall back to a clean skip with a manual
    remediation hint rather than crashing or reporting a raw error."""
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
    assert "branches" in msg.lower()  # points at the manual remediation


def test_deleted_upstream_project_is_skipped_not_errored(
    tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep
):
    """A whole project deleted (or access revoked) on GitLab is a clean skip, not
    a fatal error -- same expected-steady-state treatment as a deleted branch."""
    _safe(monkeypatch)

    def handler(cmd, **kwargs):
        if _branch_main(cmd):
            return FakeCompleted(stdout="dev")
        if cmd[:2] == ["git", "fetch"]:
            return FakeCompleted(
                returncode=1,
                stderr="remote: The project you were looking for could not be "
                       "found or you don't have permission to view it.",
            )
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, msg = update_repository("a", str(tmp_path), base_config)
    assert status == "skip"
    assert "deleted" in msg.lower() or "access revoked" in msg.lower()


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


def test_update_repositories_switched_line_has_switched_glyph_and_summary(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """A repo auto-switched onto a new branch (its old one deleted upstream)
    renders with the 'switched' glyph and is counted in the run summary --
    not silently folded into 'skipped' or misreported as a failure."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository",
        lambda p, wd, cfg: ("switched", "r1",
                             "Upstream branch deleted: feature/gone -- auto-switched to main"),
    )

    update_repositories(str(tmp_path), base_config)

    text = gls_logs.text
    assert "↝" in text
    assert "auto-switched to main" in text
    assert "1 switched" in text  # the final summary line


def test_update_repositories_summary_names_the_retry_command_on_failure(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """A bare 'Update complete: ... errors' with no next step leaves the user
    to go re-read scrollback for which repo failed and what to do about it."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository",
        lambda p, wd, cfg: ("fail", "r1", "network unreachable"),
    )

    update_repositories(str(tmp_path), base_config)

    text = gls_logs.text
    assert "1 errors" in text
    assert "Failed:" in text
    assert "contextlake update --repos" in text


def test_update_repositories_summary_line_warns_on_partial_failure(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """The final 'Update complete: ...' summary must not keep its green
    checkmark when a repo failed -- index/embed/wiki already swap to the warn
    glyph on partial failure; update previously didn't."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository",
        lambda p, wd, cfg: ("fail", "r1", "network unreachable"),
    )

    update_repositories(str(tmp_path), base_config)

    summary = [ln for ln in gls_logs.text.splitlines() if "Update complete" in ln][0]
    assert "⚠" in summary
    assert "✓" not in summary


def test_update_repositories_summary_notes_auto_switch_count(
    tmp_path, base_config, monkeypatch, gls_logs
):
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(
        core, "update_repository",
        lambda p, wd, cfg: ("switched", "r1", "auto-switched to main"),
    )

    update_repositories(str(tmp_path), base_config)

    text = gls_logs.text
    assert "1 repo(s) auto-switched" in text


def test_empty_repo_reports_a_readable_reason_not_gits_usage_hint():
    """git's "ambiguous argument 'HEAD'" is a three-line usage dump; a status
    line must carry one readable sentence instead."""
    raw = (
        "fatal: ambiguous argument 'HEAD': unknown revision or path not in the "
        "working tree.\nUse '--' to separate paths from revisions, like this:\n"
        "'git <command> [<revision>...] -- [<file>...]'"
    )
    assert core._git_reason(raw) == "No commits yet (empty repository)"


def test_git_reason_falls_back_to_a_clamped_first_line():
    raw = "error: " + ("x" * 400) + "\nsecond line"
    out = core._git_reason(raw)
    assert "\n" not in out
    assert len(out) <= 120


def test_summary_keeps_the_space_after_the_colon(tmp_path, base_config, monkeypatch, capsys):
    """style.ok() rstrips, so ok("... : ") + summary used to render "complete:0"."""
    line = core.style.ok("Update complete: " + core._summarize({"updated": [], "errors": []}))
    assert "complete: 0 updated" in core.style.strip_ansi(line)
