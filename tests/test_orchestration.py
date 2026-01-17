"""Integration-level tests for the orchestration verbs and CLI dispatch."""

import pytest

from gitlab_sync import cli, core

PROJECTS = {
    "g/a": {"archived": False, "http": "ha", "ssh": "sa", "default_branch": "main"},
    "g/b": {"archived": False, "http": "hb", "ssh": "sb", "default_branch": "main"},
    "g/old": {"archived": True, "http": "ho", "ssh": "so", "default_branch": "main"},
}

_FAKE_CFG = {"work_dir": "/tmp/x", "gitlab_group": "g"}


def _patch_config(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path=None: dict(_FAKE_CFG))


@pytest.fixture
def cached_projects(monkeypatch):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda config, group: dict(PROJECTS))


def test_clone_missing_dry_run(tmp_path, base_config, fake_subprocess, cached_projects, gls_logs):
    cfg = base_config.copy()
    cfg["dry_run"] = "true"
    core.clone_missing_repos(str(tmp_path), cfg, "g")
    # archived repo excluded; nothing actually cloned in dry-run
    assert not fake_subprocess.commands_matching("clone")
    assert "To clone: 2" in gls_logs.text


def test_clone_missing_skips_already_present(
    tmp_path, base_config, fake_subprocess, cached_projects
):
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "g" / "b" / ".git").mkdir(parents=True)
    core.clone_missing_repos(str(tmp_path), base_config, "g")
    assert not fake_subprocess.commands_matching("git", "clone")


def test_verify_structure_reports_nested_and_extra(tmp_path, base_config, monkeypatch, gls_logs):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "g" / "a" / "inner" / ".git").mkdir(parents=True)  # nested
    (tmp_path / "g" / "extra" / ".git").mkdir(parents=True)  # not in GitLab
    core.verify_structure(str(tmp_path), base_config, "g")
    assert "1 nested" in gls_logs.text
    assert "g/a/inner" in gls_logs.text


def test_show_status_counts(tmp_path, base_config, monkeypatch, gls_logs):
    monkeypatch.setattr(core, "load_gitlab_projects", lambda c, g: dict(PROJECTS))
    (tmp_path / "g" / "a" / ".git").mkdir(parents=True)
    core.show_status(str(tmp_path), base_config, "g")
    assert "Synchronized: 1" in gls_logs.text
    assert "Missing: 1" in gls_logs.text  # g/b missing (g/old is archived, excluded)


@pytest.mark.parametrize(
    "command,target",
    [
        ("fetch", "fetch_gitlab_projects"),
        ("clone", "clone_missing_repos"),
        ("update", "update_repositories"),
        ("branches", "switch_repository_branches"),
        ("verify", "verify_structure"),
        ("status", "show_status"),
    ],
)
def test_main_dispatches_to_command(monkeypatch, command, target):
    called = {"n": 0}
    _patch_config(monkeypatch)
    monkeypatch.setattr(cli, target, lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    cli.main([command])
    assert called["n"] == 1


def test_main_sync_runs_full_pipeline(monkeypatch):
    order = []
    for name in ["fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
                 "switch_repository_branches", "verify_structure"]:
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: order.append(_n))
    _patch_config(monkeypatch)
    cli.main(["sync"])
    assert order == [
        "fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
        "switch_repository_branches", "verify_structure",
    ]
