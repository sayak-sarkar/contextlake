"""A --config path that doesn't exist must exit cleanly, not fall through to a
real global store or crash with a raw traceback -- the exact incident this
guards against (see kb/config.py's ConfigError)."""

import pytest

from contextlake import cli


def test_missing_config_path_exits_clean_not_traceback(tmp_path, capsys):
    """`index` -- the exact command from the incident this guards against -- has
    no config-error handling of its own (unlike `doctor`, which diagnoses config
    problems as its whole purpose); it relies on cli.py's top-level catch.

    ``capsys``, not the ``gls_logs``/caplog fixture: ``cli.main()`` rebuilds the
    logger's handlers via ``setup_logging()``, so a handler attached before the
    call misses everything it logs (real stdout doesn't)."""
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(SystemExit) as exc:
        cli.main(["kb", "index", "--config", str(missing), "--workspace", str(tmp_path)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert "Traceback" not in out
