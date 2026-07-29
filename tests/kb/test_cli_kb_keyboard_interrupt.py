"""Ctrl-C anywhere in a knowledge-layer command must exit clean (130,
"Operation cancelled by user"), not a raw traceback. Needs the [kb] extra, so
it lives here rather than in tests/ (the core CI job runs with
--ignore=tests/kb and no [kb] install)."""

import pytest

from contextlake import cli


def test_kb_dispatch_keyboard_interrupt_exits_clean_not_traceback(monkeypatch, capsys):
    def _raise(_command, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr("contextlake.kb.commands.dispatch", _raise)
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor"])
    assert exc.value.code == 130
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "Traceback" not in out
