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


def _raising_dispatch(monkeypatch):
    """Point kb dispatch at the failure a full disk actually produces."""
    import sqlite3

    from contextlake.kb import commands as kb_commands

    def _boom(command, args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kb_commands, "dispatch", _boom)
    return sqlite3.OperationalError


def test_a_store_write_failure_is_a_message_not_a_traceback(tmp_path, capsys, monkeypatch):
    """The kb side of the CLI caught only ConfigError, so every other failure
    left as a raw traceback at any verbosity, while the mirror side had carried
    a top-level guard for a while. Measured on a full disk: a write failure
    during `kb index` reached the user as `sqlite3.OperationalError: disk I/O
    error` and a stack, with no -v passed."""
    _raising_dispatch(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["kb", "index", "--workspace", str(tmp_path)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Error: disk I/O error" in out
    assert "Traceback" not in out


def test_verbose_still_gets_the_traceback(tmp_path, monkeypatch):
    """The one-line summary must not cost a crash report its stack: -v re-raises,
    exactly as the mirror side's guard does."""
    error = _raising_dispatch(monkeypatch)
    with pytest.raises(error):
        cli.main(["-v", "kb", "index", "--workspace", str(tmp_path)])
