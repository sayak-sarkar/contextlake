"""Exit codes for the mirror pipeline.

Every mirror command used to exit 0 no matter how much of the fleet failed, so a
`mirror sync` that cloned nothing looked exactly like a healthy one -- and the
systemd unit shipped in examples/ could never trip OnFailure=. These tests pin
the contract: failures exit 1, clean runs exit 0, --exit-zero-on-partial buys the
old behaviour back, and the pre-existing Ctrl-C / bad-config exits are unchanged.
"""

import pytest

from conftest import make_local_repo
from contextlake import cli, core
from contextlake.config import ConfigError

_FAKE_CFG = {"work_dir": "/tmp/x", "gitlab_group": "g"}

# mirror command -> the cli-level function that runs it
_STAGES = {
    "fetch": "fetch_gitlab_projects",
    "clone": "clone_missing_repos",
    "update": "update_repositories",
    "branches": "switch_repository_branches",
    "verify": "verify_structure",
}

_PROJECTS = {"g/a": {"archived": False, "http": "ha", "ssh": "sa", "default_branch": "main"}}


@pytest.fixture(autouse=True)
def patched_config(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path=None: dict(_FAKE_CFG))
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: None)


def _stub_pipeline(monkeypatch, failing=()):
    """Stub every stage; those named in ``failing`` report one failed repo.

    fetch is stubbed at its own contract (the project map) rather than a result
    object -- an empty map with no --repos filter is what fetch_result scores as
    a failure, so this exercises the real scoring, not a hand-made verdict.
    """
    for command, function in _STAGES.items():
        failed = command in failing
        if function == "fetch_gitlab_projects":
            value = {} if failed else dict(_PROJECTS)
        else:
            value = core.StageResult(failed=1) if failed else core.StageResult(ok=1)
        monkeypatch.setattr(cli, function, lambda *a, _v=value, **k: _v)


@pytest.mark.parametrize("command", list(_STAGES))
def test_each_mirror_command_exits_one_on_failure(monkeypatch, command):
    _stub_pipeline(monkeypatch, failing={command})
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", command])
    assert exc.value.code == 1


@pytest.mark.parametrize("command", list(_STAGES))
def test_each_mirror_command_exits_zero_when_clean(monkeypatch, command):
    _stub_pipeline(monkeypatch)
    # No SystemExit at all: main() returning normally is exit 0.
    assert cli.main(["mirror", command]) is None


@pytest.mark.parametrize("stage", list(_STAGES))
def test_sync_exits_one_on_a_failure_in_any_stage(monkeypatch, stage):
    """sync aggregates: one bad stage fails the whole run, even when the stages
    after it succeed (they still run -- a partial mirror is resumable)."""
    _stub_pipeline(monkeypatch, failing={stage})
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "sync"])
    assert exc.value.code == 1


def test_sync_exits_zero_when_every_stage_is_clean(monkeypatch):
    _stub_pipeline(monkeypatch)
    assert cli.main(["mirror", "sync"]) is None


def test_sync_finale_glyph_tracks_the_exit_code(monkeypatch, capsys):
    """No ✓ over a run that exits 1 -- the hollow success this guard exists for."""
    _stub_pipeline(monkeypatch, failing={"clone"})
    with pytest.raises(SystemExit):
        cli.main(["mirror", "sync"])
    out = capsys.readouterr().out
    assert "Full synchronization complete" in out
    assert "✓ Full synchronization complete" not in out


def test_sync_runs_the_audit_stage_before_exiting_nonzero(monkeypatch):
    """The exit decision is the last thing that happens, so a partial mirror still
    gets its audit report written."""
    audited = []
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: audited.append(1))
    _stub_pipeline(monkeypatch, failing={"update"})
    with pytest.raises(SystemExit):
        cli.main(["mirror", "sync"])
    assert audited == [1]


@pytest.mark.parametrize("argv", [["mirror", "clone"], ["mirror", "sync"]])
def test_exit_zero_on_partial_restores_the_old_exit_status(monkeypatch, argv):
    _stub_pipeline(monkeypatch, failing={"clone"})
    assert cli.main(argv + ["--exit-zero-on-partial"]) is None


def test_keyboard_interrupt_still_exits_130(monkeypatch):
    _stub_pipeline(monkeypatch)

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "update_repositories", _interrupt)
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update"])
    assert exc.value.code == 130


def test_config_error_still_exits_1(monkeypatch):
    def _raise(path=None):
        raise ConfigError("config file not found: /nope.ini")

    monkeypatch.setattr(cli, "load_config", _raise)
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update"])
    assert exc.value.code == 1


# --- the counts come from the stages themselves, not from the CLI -----------

def test_update_failure_counted_by_the_real_stage_reaches_the_exit_code(
    monkeypatch, tmp_path
):
    """End to end through core: one repo whose update errors -> `errors` bucket ->
    StageResult.failed -> exit 1."""
    make_local_repo(tmp_path, "r1")
    monkeypatch.setattr(core, "update_repository",
                        lambda p, wd, cfg: ("error", "r1", "fatal: boom"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update", "--work-dir", str(tmp_path)])
    assert exc.value.code == 1


def test_verify_fails_only_on_an_invalid_repo_not_a_missing_one(monkeypatch, tmp_path):
    """A cloned path with no .git is corruption (exit 1); a project simply not
    cloned yet is routine (archived projects never are) and must stay exit 0."""
    monkeypatch.setattr(core, "load_gitlab_projects",
                        lambda config, group, **kw: dict(_PROJECTS))

    assert cli.main(["mirror", "verify", "--work-dir", str(tmp_path)]) is None  # missing

    (tmp_path / "g" / "a").mkdir(parents=True)  # present, but not a git repo
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "verify", "--work-dir", str(tmp_path)])
    assert exc.value.code == 1


def test_a_filtered_fetch_matching_nothing_is_not_a_failure(monkeypatch):
    """0 projects with --repos in play is a legitimate answer to a narrow pattern;
    0 projects with nothing filtering means the group or token is wrong."""
    monkeypatch.setattr(cli, "fetch_gitlab_projects", lambda *a, **k: {})

    assert cli.main(["mirror", "fetch", "--repos", "no-such-team/*"]) is None

    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "fetch"])
    assert exc.value.code == 1
