"""Integration-level tests for the orchestration verbs and CLI dispatch."""

import re

import pytest

from conftest import make_local_repo
from contextlake import cli, core

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
    # styled, right-aligned summary: "<glyph> Synchronized   1"
    assert re.search(r"Synchronized\s+1\b", gls_logs.text)
    assert re.search(r"Missing\s+1\b", gls_logs.text)  # g/b missing (g/old archived)


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


class _SpyProgress:
    """Stand-in for style.Progress that only records call counts (no rendering),
    mirroring the wire-through idiom in tests/kb/test_kb_wiki.py and
    tests/kb/test_kb_commands.py."""

    instances: list["_SpyProgress"] = []

    def __init__(self, total, **kwargs):
        self.total = total
        self.label = kwargs.get("label")
        self.advance_calls = 0
        self.done_calls = 0
        _SpyProgress.instances.append(self)

    def advance(self, *args, **kwargs):
        self.advance_calls += 1

    def done(self, *args, **kwargs):
        self.done_calls += 1


def test_update_repositories_reports_progress_and_leaves_stdout_unchanged(
    tmp_path, base_config, monkeypatch, gls_logs
):
    """Wire-through: Progress.advance fires once per repo across every _status
    branch (updated/nochange/skip alike) and done() once, on a separate channel
    (stderr) from the existing stdout `_status(...)` detail lines, which must
    render exactly as before (byte-identical: same counter/glyph/path/message).
    """
    for name in ("r1", "r2", "r3"):
        make_local_repo(tmp_path, name)

    outcomes = {
        "r1": ("ok", "r1", "Updated to abc123"),
        "r2": ("nochange", "r2", "Already up to date"),
        "r3": ("skip", "r3", "Skipped (unsafe: dirty)"),
    }
    monkeypatch.setattr(core, "update_repository", lambda p, wd, cfg: outcomes[p])

    _SpyProgress.instances = []
    monkeypatch.setattr(core.style, "Progress", _SpyProgress)

    core.update_repositories(str(tmp_path), base_config)

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == 3
    assert p.label == "update"
    assert p.advance_calls == 3
    assert p.done_calls == 1

    text = gls_logs.text
    for path, (_status_val, _p, message) in outcomes.items():
        assert path in text
        assert message in text
    # The existing counter text stays exactly as before: "[i/3]" for each repo.
    for i in range(1, 4):
        assert f"[{i}/3]" in text


def test_switch_repository_branches_reports_progress(
    tmp_path, base_config, monkeypatch, gls_logs, cached_projects
):
    """Same wire-through coverage for the branches loop: advance once per repo
    (switched/already/error branches), done() once, existing stdout unchanged.
    """
    for name in ("g/a", "g/b"):
        make_local_repo(tmp_path, name)

    outcomes = {
        "g/a": ("switched", "g/a", "Switched to dev"),
        "g/b": ("error", "g/b", "checkout failed"),
    }
    monkeypatch.setattr(core, "switch_repository_branch", lambda p, proj, wd, cfg: outcomes[p])

    _SpyProgress.instances = []
    monkeypatch.setattr(core.style, "Progress", _SpyProgress)

    core.switch_repository_branches(str(tmp_path), base_config, "g")

    assert len(_SpyProgress.instances) == 1
    p = _SpyProgress.instances[0]
    assert p.total == 2
    assert p.label == "branches"
    assert p.advance_calls == 2
    assert p.done_calls == 1

    text = gls_logs.text
    for path, (_status_val, _p, message) in outcomes.items():
        assert path in text
        assert message in text
    for i in range(1, 3):
        assert f"[{i}/2]" in text
