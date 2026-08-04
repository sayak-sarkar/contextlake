"""A mirror command must refuse to run against the placeholder group.

With no config anywhere, `contextlake mirror status` used to warn that it had
found none, then carry on using the literal `your-gitlab-group`, read whatever
project cache happened to be lying around, and print a plausible-looking sync
report -- exiting 0. Nothing in that output tells a script (or a tired reader)
it is fiction. `init` already refuses the same placeholder with exit 2, so the
tool disagreed with itself about whether it is a usable value.
"""

import pytest

from contextlake import cli
from contextlake.config import DEFAULT_CONFIG

PLACEHOLDER = DEFAULT_CONFIG["gitlab_group"]

# Every mirror verb whose result is defined by the group: it enumerates the
# group, or it reads the project cache keyed on it.
GROUP_COMMANDS = ["fetch", "clone", "branches", "verify", "sync", "status", "audit"]


@pytest.fixture(autouse=True)
def _no_config(tmp_path, monkeypatch):
    """No global config, no ancestor config: the exact "fresh machine" state the
    acceptance run reproduced."""
    monkeypatch.setattr("contextlake.config.CONFIG_FILE", str(tmp_path / "none.ini"))
    monkeypatch.setattr("contextlake.config.LOCAL_CONFIG_FILE", str(tmp_path / "no-local.ini"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _explode_if_a_stage_runs(monkeypatch):
    """Every mirror stage is replaced with a detonator: the guard must fire
    before any of them, since the defect was a report assembled from real
    (stale) cache data, not an empty one."""
    def boom(*a, **kw):
        raise AssertionError("a mirror stage ran without a configured group")

    for name in ("fetch_gitlab_projects", "clone_missing_repos", "update_repositories",
                 "switch_repository_branches", "verify_structure", "show_status",
                 "run_audit"):
        monkeypatch.setattr(cli, name, boom)


@pytest.mark.parametrize("command", GROUP_COMMANDS)
def test_a_mirror_command_refuses_the_placeholder_group(command, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", command])
    # 2, matching `init`'s refusal of this same placeholder -- and above all not 0.
    assert exc.value.code == 2
    # The CLI logs through log(), i.e. stdout, for warnings and errors alike.
    out = capsys.readouterr().out
    assert PLACEHOLDER in out
    assert "--group" in out


@pytest.mark.parametrize("command", GROUP_COMMANDS)
def test_the_refusal_prints_no_sync_report(command, capsys):
    """The damage was not the exit code alone: a plausible "N projects (active) /
    N missing" report is what a reader would have believed."""
    with pytest.raises(SystemExit):
        cli.main(["mirror", command])
    out = capsys.readouterr().out
    assert "Missing repositories" not in out
    assert "projects (active)" not in out


def test_an_empty_group_is_refused_too(capsys):
    """Not just the shipped literal: a config with `gitlab_group =` (blank) has
    no group either, and used to enumerate an empty group name."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "status", "--group", "   "])
    assert exc.value.code == 2


def test_a_real_group_still_runs(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "show_status",
                        lambda work_dir, config, group: seen.update(group=group))
    cli.main(["mirror", "status", "--group", "acme"])
    assert seen == {"group": "acme"}


def test_the_group_override_reaches_the_cache_path(tmp_path, monkeypatch, capsys):
    """--group/--work-dir are not in _SCALAR_FLAGS, so apply_cli_overrides never
    propagated them: two runs against different groups from ONE config file
    computed the same cache path and overwrote each other's project list. The
    resolved pair is written back before anything derives a path from it -- and
    under BOTH spellings, since `group` shadows `gitlab_group`."""
    ini = tmp_path / "one.ini"
    ini.write_text("[contextlake]\ngroup = team-a\nwork_dir = " + str(tmp_path / "w") + "\n")
    monkeypatch.setattr(cli, "show_status", lambda *a, **kw: None)

    seen = []
    for group in ("team-a", "team-b"):
        cli.main(["mirror", "status", "--config", str(ini), "--group", group])
        seen.append([line for line in capsys.readouterr().out.splitlines()
                     if "Cache file:" in line][0])

    assert seen[0] != seen[1], seen


def test_update_still_works_without_a_group(monkeypatch):
    """`update` is the one mirror verb defined entirely by what is already on
    disk (update_repositories takes no group), so refusing it would break a
    legitimate offline workflow for no honesty gain."""
    seen = {}
    monkeypatch.setattr(cli, "update_repositories",
                        lambda work_dir, config: seen.update(ran=True) or cli.StageResult())
    cli.main(["mirror", "update"])
    assert seen == {"ran": True}


def test_bootstrap_without_mirroring_or_auditing_still_works(monkeypatch):
    """`bootstrap --no-sync --no-audit` is a knowledge-layer build over repos
    already cloned; nothing on that path consults the group."""
    seen = {}
    monkeypatch.setattr(cli, "_bootstrap",
                        lambda *a, **kw: seen.update(ran=True))
    cli.main(["bootstrap", "--no-sync", "--no-audit"])
    assert seen == {"ran": True}


def test_bootstrap_that_would_mirror_is_refused(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bootstrap"])
    assert exc.value.code == 2
    assert PLACEHOLDER in capsys.readouterr().out
