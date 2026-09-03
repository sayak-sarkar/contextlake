"""A broken worker pool must fall back to serial, not fail every repository.

`_index_workspace` has always carried a serial fallback for a pool it cannot
use. That fallback was unreachable for the one failure mode it was written for.

`BrokenProcessPool` subclasses `RuntimeError`, so the per-future
`except Exception` caught it, counted one failure against the repository whose
future happened to raise it, and continued. Every pending future raises the same
exception when a pool breaks, so a 656-repo run reported 640 failures in about a
minute and the outer `except (BrokenProcessPool, OSError)` never ran once.

These tests drive `_index_workspace` with a stand-in executor, because a real
pool break needs a real worker death and cannot be asked for on demand.
"""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import threading
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool

import pytest

from contextlake import cli

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text("def foo():\n    return 1\n")
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)


def _kb(tmp_path):
    """A config naming this test's own store, never the user's real one."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


class _PoolThatBreaks:
    """Every future raises `BrokenProcessPool`, which is what a real break does.

    CPython's `_terminate_broken` sets that one exception on every pending
    future, so the parent sees it once per repository rather than once.
    """

    def __init__(self, *_a, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def submit(self, *_a, **_kw):
        fut = Future()
        exc = BrokenProcessPool(
            "A process in the process pool was terminated abruptly while the "
            "future was running or pending.")
        exc.__cause__ = RuntimeError("the underlying reason the pool broke")
        fut.set_exception(exc)
        return fut


class _PoolThatFailsThenBreaks(_PoolThatBreaks):
    """One repository genuinely fails, then the pool breaks.

    The first future is already done when `as_completed` starts, so it is
    yielded first; the second resolves from a timer, so it is yielded second.
    That ordering is what makes the double-count case reachable.
    """

    def __init__(self, *_a, **_kw):
        super().__init__()
        self._submitted = 0

    def submit(self, *_a, **_kw):
        fut = Future()
        self._submitted += 1
        if self._submitted == 1:
            fut.set_exception(ValueError("this repository really did fail"))
            return fut
        exc = BrokenProcessPool("terminated abruptly")
        threading.Timer(0.05, fut.set_exception, args=(exc,)).start()
        return fut


class _PoolThatSucceedsThenBreaks(_PoolThatBreaks):
    """One repository is indexed and PERSISTED, then the pool breaks.

    Distinct from `_PoolThatFailsThenBreaks`, where the first future raises and `_persist`
    never runs. Here the first future carries a real shard, so everything `_persist` does
    for that repository has already happened when the serial pass re-runs the full
    work-list -- which is the state a double-count is reachable from.
    """

    def __init__(self, *_a, **_kw):
        super().__init__()
        self._submitted = 0

    def submit(self, fn, *a, **kw):
        fut = Future()
        self._submitted += 1
        if self._submitted == 1:
            fut.set_result(fn(*a, **kw))
            return fut
        exc = BrokenProcessPool("terminated abruptly")
        threading.Timer(0.05, fut.set_exception, args=(exc,)).start()
        return fut


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    for name in ("alpha", "beta"):
        _git_repo(ws / name)
    return ws


def _run(workspace, tmp_path, capsys):
    """`cli.main` reports through `sys.exit`, so the code arrives as SystemExit."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["kb", "index", "--config", str(_kb(tmp_path)),
                  "--workspace", str(workspace), "--workers", "2"])
    return excinfo.value.code, capsys.readouterr().out


def test_a_broken_pool_falls_back_to_serial_instead_of_failing_every_repo(
        workspace, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _PoolThatBreaks)
    rc, out = _run(workspace, tmp_path, capsys)

    assert "falling back to serial" in out
    # The defect's signature: one "failed" line per repository and no fallback.
    assert "0 failed" in out
    assert rc == 0
    # Both repositories indexed by the serial pass. The id carries an @<sha>.
    for name in ("alpha", "beta"):
        assert f"{name}@" in out


def test_the_reason_the_pool_broke_is_reported(workspace, tmp_path, capsys, monkeypatch):
    """`str(BrokenProcessPool)` is a fixed sentence naming no cause.

    CPython attaches the real reason as `__cause__`. Logging only `str(e)` is
    what left the cause of a reproducible break unidentified across several
    investigations.
    """
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _PoolThatBreaks)
    _rc, out = _run(workspace, tmp_path, capsys)
    assert "the underlying reason the pool broke" in out


def test_a_failure_before_the_break_is_not_counted_twice(
        workspace, tmp_path, capsys, monkeypatch):
    """The serial pass re-runs the full work-list, including the repository that
    already failed in the pool phase. Without resetting the counter, that
    repository is counted once by each pass."""
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _PoolThatFailsThenBreaks)
    _rc, out = _run(workspace, tmp_path, capsys)

    assert "falling back to serial" in out
    # Both repositories are valid, so the serial pass indexes both and the only
    # honest total is zero. A retained count would report "1 failed".
    assert "0 failed" in out


def test_a_repo_reported_before_the_break_is_documented_once(
        workspace, tmp_path, capsys, monkeypatch):
    """The docs target list has the same double-count exposure the counters had.

    The serial pass re-runs the full work-list, so `_persist` fires a second time for every
    repository the pool phase already persisted. Appending unconditionally would hand the
    docs step four targets for two repositories, and the summary would read "2 written,
    2 unchanged" for a workspace holding two repos.
    """
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                        _PoolThatSucceedsThenBreaks)
    _rc, out = _run(workspace, tmp_path, capsys)

    assert "falling back to serial" in out
    assert "Documents: 2 written, 0 unchanged, documents failed for 0 repo(s)" in out
