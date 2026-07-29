"""Ctrl-C during `init`'s interactive prompt must exit clean (130, "Operation
cancelled by user"), not fall through to a raw traceback -- the exact incident
this guards against: only the mirror-pipeline commands (fetch/clone/sync/...)
had a KeyboardInterrupt catch, `init` did not.

The equivalent regression test for the knowledge-layer command path lives in
tests/kb/ (it needs the [kb] extra, which the core CI job never installs)."""

import pytest

from contextlake import cli


def test_init_keyboard_interrupt_exits_clean_not_traceback(monkeypatch, capsys):
    def _raise(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("contextlake.init_cmd.cmd_init", _raise)
    with pytest.raises(SystemExit) as exc:
        cli.main(["init"])
    assert exc.value.code == 130
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "Traceback" not in out
