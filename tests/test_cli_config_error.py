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
