"""Tests for clone behaviour: dry-run, corruption cleanup, retry, command choice."""

from conftest import FakeCompleted
from contextlake import core
from contextlake.core import clone_repository


def _cfg(base_config, **over):
    cfg = base_config.copy()
    cfg.update(over)
    return cfg


def test_skip_existing_valid_repo(tmp_path, base_config, fake_subprocess):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    status, _, _ = clone_repository("a", "grp/a", "http", "ssh", str(tmp_path), base_config)
    assert status == "skip"
    assert fake_subprocess.calls == []  # nothing executed


def test_dry_run_does_not_clone(tmp_path, base_config, fake_subprocess):
    status, _, msg = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, dry_run="true")
    )
    assert status == "dry-run"
    assert fake_subprocess.calls == []


def test_clone_success(tmp_path, base_config, fake_subprocess, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)  # force git
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http://x/g/p.git", "ssh", str(tmp_path),
        _cfg(base_config, clone_method="git"),
    )
    assert status == "ok"
    assert fake_subprocess.commands_matching("git", "clone")


def test_clone_prefers_glab_when_method_glab(tmp_path, base_config, fake_subprocess):
    clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clone_method="glab")
    )
    # glab must receive the FULL group-qualified path, not the local dest path
    assert fake_subprocess.commands_matching("glab", "repo", "clone", "grp/g/p")


def test_corrupted_dir_cleaned_and_recloned(tmp_path, base_config, fake_subprocess):
    corrupt = tmp_path / "g" / "p"
    corrupt.mkdir(parents=True)
    (corrupt / "junk.txt").write_text("not a repo")
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clean_corrupted="true")
    )
    assert status == "ok"
    assert not (corrupt / "junk.txt").exists()  # cleaned


def test_corrupted_dir_errors_when_clean_disabled(tmp_path, base_config, fake_subprocess):
    corrupt = tmp_path / "g" / "p"
    corrupt.mkdir(parents=True)
    status, _, msg = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path), _cfg(base_config, clean_corrupted="false")
    )
    assert status == "error"
    assert "not a git repo" in msg


def test_clone_retries_then_succeeds(tmp_path, base_config, fake_subprocess, no_sleep, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    state = {"n": 0}

    def handler(cmd, **kwargs):
        if "clone" in cmd:
            state["n"] += 1
            if state["n"] < 2:
                return FakeCompleted(returncode=1, stderr="connection reset")
            return FakeCompleted(returncode=0)
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = clone_repository(
        "g/p", "grp/g/p", "http", "ssh", str(tmp_path),
        _cfg(base_config, clone_method="git", max_retries="3"),
    )
    assert status == "ok"
    assert state["n"] == 2  # retried once
