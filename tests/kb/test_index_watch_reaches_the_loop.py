"""`kb index --watch` must actually watch on the single-source path, not just with --workspace.

Found by the stability campaign, 2026-08-25. `--watch` was read only inside the
`if workspace:` branch of `cmd_index`. Given `--source PATH` (or the zero-config current
directory), the flag parsed, one index pass ran, and the command exited 0 -- without watching
and without saying it would not. Its own help promises "keep re-running ... on an interval"
with no such condition, so this was the silent-success shape: a flag that reports success
having done nothing.

Measured before the fix: `kb index --source REPO --watch` exited 0 in under a second, while
`kb index --workspace DIR --watch` printed its watch banner and stayed up.

The test asserts the DISPATCH, because that is where the bug was: does `--watch` reach a loop
at all. Running a real loop would test `_watch_loop`, which already has its own tests.
"""

from __future__ import annotations

import pytest

from contextlake import cli
from contextlake.kb.cmds import index as index_cmd


def _args(*argv: str):
    return cli.build_parser().parse_args(argv)


@pytest.fixture()
def spy(monkeypatch):
    calls = []

    def fake_watch_loop(run_once, *, interval=60, **kw):
        calls.append(interval)
        return 1                       # do NOT run the body: one pass is not what is under test

    monkeypatch.setattr(index_cmd, "_watch_loop", fake_watch_loop)
    monkeypatch.setattr(index_cmd, "_cmd_index_once", lambda args: 0)
    return calls


def test_watch_reaches_the_loop_with_source(spy, tmp_path):
    rc = index_cmd.cmd_index(_args("kb", "index", "--source", str(tmp_path), "--watch"))
    assert rc == 0
    assert spy == [60], "--watch on the single-source path never reached a watch loop"


def test_watch_honours_the_interval(spy, tmp_path):
    index_cmd.cmd_index(
        _args("kb", "index", "--source", str(tmp_path), "--watch", "--interval", "5"))
    assert spy == [5]


def test_zero_config_current_directory_also_watches(spy):
    index_cmd.cmd_index(_args("kb", "index", "--watch"))
    assert spy == [60], "the zero-config (no --source) path must watch too"


def test_without_watch_there_is_no_loop(spy, tmp_path):
    rc = index_cmd.cmd_index(_args("kb", "index", "--source", str(tmp_path)))
    assert rc == 0
    assert spy == [], "a plain index must not loop"


def test_workspace_keeps_its_own_loop(spy, tmp_path):
    """The wrapper must not double-wrap: --workspace handles --watch inside _cmd_index_once."""
    index_cmd.cmd_index(_args("kb", "index", "--workspace", str(tmp_path), "--watch"))
    assert spy == [], "the wrapper stole the workspace branch's own watch handling"
