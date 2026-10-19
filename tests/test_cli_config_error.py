"""A --config path that doesn't exist must exit cleanly, not fall through to
the real global ~/.contextlake.ini or crash with a raw traceback -- the mirror
side of the same guard kb.toml already has (see tests/kb/test_cli_kb_config_error.py
and config.py's ConfigError)."""

import pytest

from contextlake import cli


def test_missing_config_path_exits_clean_not_traceback(tmp_path, capsys):
    """`mirror status` has no config-error handling of its own; it relies on
    cli.py's top-level catch, same as `kb index` does for kb.toml."""
    missing = tmp_path / "does-not-exist.ini"
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "status", "--config", str(missing)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert "Traceback" not in out


def _ini(tmp_path, extra):
    (tmp_path / "ws").mkdir(exist_ok=True)
    ini = tmp_path / "c.ini"
    ini.write_text(f"[contextlake]\ngitlab_group = acme\nwork_dir = {tmp_path / 'ws'}\n"
                   f"cache_dir = {tmp_path / 'cache'}\n{extra}")
    return ini


def _status(ini):
    try:
        cli.main(["mirror", "status", "--config", str(ini)])
    except SystemExit as exc:
        return exc.code
    return 0


def test_dry_run_spelled_yes_in_the_config_is_a_dry_run(tmp_path, capsys):
    """It read as false, so the run was real and the banner never appeared."""
    _status(_ini(tmp_path, "dry_run = yes\n"))
    assert "DRY RUN" in capsys.readouterr().out


def test_dry_run_spelled_no_in_the_config_is_not_a_dry_run(tmp_path, capsys):
    """The control: the banner must follow the value, not appear on every run."""
    _status(_ini(tmp_path, "dry_run = no\n"))
    assert "DRY RUN" not in capsys.readouterr().out


@pytest.mark.parametrize("extra, message", [
    ("protect_working_branches = ture\n", "protect_working_branches: 'ture' is not one of"),
    ("branch_strategy = bogus\n", "branch_strategy: 'bogus' is not one of"),
], ids=["boolean", "branch_strategy"])
def test_a_bad_config_value_exits_clean_and_names_the_key(tmp_path, capsys, extra, message):
    """apply_cli_overrides ran outside the ConfigError handler, so a bad branch_strategy
    was a traceback, and a new check there would have been one too."""
    code = _status(_ini(tmp_path, extra))
    out = capsys.readouterr().out
    assert code == 1
    assert message in out
    assert "Traceback" not in out


def test_a_config_with_a_percent_path_is_read_by_mirror_commands(tmp_path, capsys):
    """`init` writes work_dir unescaped, and a % in it crashed every later mirror command."""
    ini = _ini(tmp_path, "")
    ini.write_text(ini.read_text().replace(str(tmp_path / "ws"), str(tmp_path / "50%off")))

    code = _status(ini)
    out = capsys.readouterr().out

    assert code == 0, out
    assert "50%off" in out
    assert "Traceback" not in out


def test_a_config_with_no_section_header_exits_clean(tmp_path, capsys):
    ini = tmp_path / "nohdr.ini"
    ini.write_text("gitlab_group = acme\n")

    code = _status(ini)
    out = capsys.readouterr().out

    assert code == 1
    assert "before any [contextlake] header" in out
    assert "Traceback" not in out
