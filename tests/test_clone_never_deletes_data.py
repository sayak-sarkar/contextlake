"""clone never deletes a non-empty directory that has no .git.

The incident, reproduced before the fix with real runs and git stubbed:

- A discovered .contextlake.ini sets `work_dir` and `gitlab_group`, which stay
  honoured from a local file. The forge lists `attacker-group/Documents`, and
  clone `rmtree`d a non-empty, non-git `Documents` under that work_dir as a
  "corrupted" directory, then cloned over it.
- The forge lists `grp/team` while `team/api` and `team/web` are clones. `team/`
  is a plain directory, so clone `rmtree`d it, and both clones with it.
- After a failed clone, the cleanup `rmtree` removed whatever stood at the
  destination, including a nested clone that appeared during the call (a
  concurrent clone of `grp/team/api` in the same run).

The rule now: an EMPTY directory (what an interrupted clone leaves before git
writes .git) may be removed when clean_corrupted is on. A non-empty one with no
.git is never removed, dry run or not. The cleanup after a failed clone removes
only an empty directory or one with .git at its top, its own leftover.

Everything created or deleted here is under tmp_path. git is never run.
"""

import subprocess
from pathlib import Path

import pytest

from conftest import FakeCompleted
from contextlake import config as cfgmod
from contextlake import core
from contextlake.config import load_config
from contextlake.core import clone_repository


def _git_creates_dest(cmd, **kwargs):
    """Stand-in for a successful `git clone URL DEST`: DEST/.git appears."""
    if cmd[:2] == ["git", "clone"]:
        (Path(cmd[-1]) / ".git").mkdir(parents=True, exist_ok=True)
    return FakeCompleted()


def _listing(monkeypatch, paths):
    page = [{"path_with_namespace": p, "http_url_to_repo": f"https://gitlab.com/{p}.git",
             "ssh_url_to_repo": "", "archived": False, "default_branch": "main"}
            for p in paths]
    monkeypatch.setattr(core, "_fetch_projects_page_glab",
                        lambda group_enc, per_page, page_no: page if page_no == 1 else [])


def _message(work, rel):
    return (f"{work / rel} exists, is not a git repository and is not empty, so it was "
            "left alone; move it aside to clone here")


@pytest.fixture
def git_stub(fake_subprocess, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    fake_subprocess.handler = _git_creates_dest
    return fake_subprocess


def test_planted_work_dir_and_group_cannot_delete_a_users_directory(
        tmp_path, monkeypatch, git_stub):
    """The S05 open item 2 chain, end to end through load_config's real ancestor walk."""
    home = tmp_path / "victim-home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "notes.txt").write_text("user data")
    repo = tmp_path / "clones" / "cloned-repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".contextlake.ini").write_text(
        f"[contextlake]\nwork_dir = {home}\ngitlab_group = attacker-group\n"
        "adaptive_workers = false\n")
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(tmp_path / "no-global.ini"))
    monkeypatch.setattr(cfgmod, "LOCAL_CONFIG_FILE", ".contextlake.ini")
    monkeypatch.delenv(cfgmod.NO_LOCAL_CONFIG_ENV, raising=False)
    monkeypatch.chdir(repo / "src")
    config = load_config()
    config["group"] = config["gitlab_group"]
    assert config["work_dir"] == str(home)  # still honoured from a local file
    _listing(monkeypatch, ["attacker-group/Documents"])

    core.fetch_gitlab_projects("attacker-group", config)
    result = core.clone_missing_repos(config["work_dir"], config, "attacker-group")

    assert (home / "Documents" / "notes.txt").read_text() == "user data"
    assert git_stub.commands_matching("git", "clone") == []
    assert (result.ok, result.failed) == (0, 1)


def test_a_project_path_over_a_directory_of_clones_leaves_them_alone(
        tmp_path, base_config, monkeypatch, git_stub, gls_logs):
    """S05 open item 2: forge path `grp/team` over local clones team/api and team/web."""
    work = tmp_path / "work"
    for repo in ("team/api", "team/web"):
        (work / repo / ".git").mkdir(parents=True)
        (work / repo / "README").write_text("cloned work tree")
    _listing(monkeypatch, ["grp/team", "grp/team/api", "grp/team/web"])
    config = {**base_config, "work_dir": str(work), "gitlab_group": "grp", "group": "grp",
              "clone_method": "git", "adaptive_workers": "false"}

    core.fetch_gitlab_projects("grp", config)
    result = core.clone_missing_repos(str(work), config, "grp")

    assert (work / "team/api/README").exists() and (work / "team/web/README").exists()
    assert git_stub.commands_matching("git", "clone") == []
    assert (result.ok, result.failed) == (0, 1)
    assert any(_message(work, "team") in r.getMessage() for r in gls_logs.records)


@pytest.mark.parametrize("clean", ["true", "false"])
@pytest.mark.parametrize("dry_run", ["false", "true"])
def test_a_non_empty_non_git_directory_is_an_error_and_untouched(
        tmp_path, base_config, git_stub, clean, dry_run):
    work = tmp_path / "work"
    (work / "g" / "p" / "sub").mkdir(parents=True)
    (work / "g" / "p" / "sub" / "file.txt").write_text("data")
    config = {**base_config, "clean_corrupted": clean, "dry_run": dry_run}

    result = clone_repository("g/p", "grp/g/p", "https://gitlab.com/grp/g/p.git", "",
                              str(work), config)

    assert result == ("error", "g/p", _message(work, "g/p"))
    assert (work / "g" / "p" / "sub" / "file.txt").read_text() == "data"
    assert git_stub.calls == []


def test_an_empty_directory_is_still_removed_and_cloned(tmp_path, base_config, git_stub):
    """What an interrupted clone leaves when it dies before git writes .git."""
    work = tmp_path / "work"
    (work / "g" / "p").mkdir(parents=True)
    status, _, _ = clone_repository("g/p", "grp/g/p", "https://gitlab.com/grp/g/p.git", "",
                                    str(work), {**base_config, "clone_method": "git"})
    assert status == "ok"
    assert (work / "g" / "p" / ".git").is_dir()


def test_an_empty_directory_in_dry_run_and_with_cleaning_off(tmp_path, base_config, git_stub):
    work = tmp_path / "work"
    (work / "g" / "p").mkdir(parents=True)
    dry = clone_repository("g/p", "grp/g/p", "u", "", str(work), {**base_config, "dry_run": "true"})
    off = clone_repository("g/p", "grp/g/p", "u", "", str(work),
                           {**base_config, "clean_corrupted": "false"})
    assert dry == ("dry-run", "g/p", "Would remove the empty directory and clone")
    assert off == ("error", "g/p", "Exists but not a git repo (use --clean-corrupted)")
    assert (work / "g" / "p").is_dir() and git_stub.calls == []


def _nested_clone_appears(dest):
    """What a concurrent clone of team/api does to `team/` during this call."""
    (dest / "api" / ".git").mkdir(parents=True, exist_ok=True)
    (dest / "api" / "README").write_text("another clone's work tree")


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_cleanup_after_a_failed_clone_spares_a_directory_it_did_not_create(
        tmp_path, base_config, fake_subprocess, monkeypatch, failure):
    """The two rmtree calls after a failed clone (generic error, and Timeout)."""
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    work = tmp_path / "work"
    work.mkdir()
    dest = work / "team"

    def handler(cmd, **kwargs):
        if cmd[:2] != ["git", "clone"]:
            return FakeCompleted()
        _nested_clone_appears(dest)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return FakeCompleted(returncode=128, stderr=(
            f"fatal: destination path '{dest}' already exists and is not an empty directory."))

    fake_subprocess.handler = handler
    status, _, _ = clone_repository("team", "grp/team", "https://gitlab.com/grp/team.git", "",
                                    str(work), {**base_config, "clone_method": "git",
                                                "max_retries": "1"})
    assert status == "error"
    assert (dest / "api" / "README").read_text() == "another clone's work tree"


def test_a_retry_spares_a_directory_it_did_not_create(
        tmp_path, base_config, fake_subprocess, monkeypatch, no_sleep):
    """The per-attempt clear in _clone_once, on the second attempt."""
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    work = tmp_path / "work"
    work.mkdir()
    dest = work / "team"
    attempts = []

    def handler(cmd, **kwargs):
        if cmd[:2] != ["git", "clone"]:
            return FakeCompleted()
        attempts.append(1)
        if len(attempts) == 1:
            _nested_clone_appears(dest)
            return FakeCompleted(returncode=1, stderr="connection reset")
        if dest.exists() and any(dest.iterdir()):
            return FakeCompleted(returncode=128, stderr="fatal: destination path already "
                                 "exists and is not an empty directory.")
        return FakeCompleted()

    fake_subprocess.handler = handler
    status, _, _ = clone_repository("team", "grp/team", "https://gitlab.com/grp/team.git", "",
                                    str(work), {**base_config, "clone_method": "git",
                                                "max_retries": "2"})
    assert status == "error"
    assert len(attempts) == 2
    assert (dest / "api" / "README").exists()


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_cleanup_still_removes_its_own_partial_clone(
        tmp_path, base_config, fake_subprocess, monkeypatch, failure):
    """The leftover a dying git clone does leave (.git first) is still cleared."""
    monkeypatch.setattr(core.shutil, "which", lambda _: None)
    work = tmp_path / "work"
    work.mkdir()
    dest = work / "g" / "p"

    def handler(cmd, **kwargs):
        if cmd[:2] != ["git", "clone"]:
            return FakeCompleted()
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        (dest / "half-checked-out.txt").write_text("x")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return FakeCompleted(returncode=1, stderr="early EOF")

    fake_subprocess.handler = handler
    status, _, _ = clone_repository("g/p", "grp/g/p", "https://gitlab.com/grp/g/p.git", "",
                                    str(work), {**base_config, "clone_method": "git",
                                                "max_retries": "1"})
    assert status == "error"
    assert not dest.exists()
