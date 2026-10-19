"""The single-writer lock for scheduled runs.

kb/lock.py already does this for the knowledge store, but it lives in the
optional [kb] tier and `schedule run` must work without it. Same design: a
pidfile, a liveness check, and a stale lock reclaimed rather than honoured
forever.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import contextlake
from contextlake.schedule import runlock


def test_the_lock_is_taken_and_released(tmp_path):
    path = str(tmp_path / "run.lock")
    with runlock.RunLock(path, "default"):
        assert os.path.exists(path)
    assert not os.path.exists(path)


def test_a_live_holder_refuses_the_second_taker(tmp_path):
    path = str(tmp_path / "run.lock")
    with runlock.RunLock(path, "default"):
        with pytest.raises(runlock.RunBusy) as excinfo:
            with runlock.RunLock(path, "nightly"):
                pass
    assert excinfo.value.holder["job"] == "default"
    assert str(os.getpid()) in str(excinfo.value)


def test_a_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    path = tmp_path / "run.lock"
    # PID 2**22 is above the default pid_max on Linux, so it cannot be live.
    path.write_text(json.dumps({"pid": 4194304, "job": "ghost", "host": "elsewhere"}),
                    encoding="utf-8")
    with runlock.RunLock(str(path), "default"):
        assert json.loads(path.read_text(encoding="utf-8"))["job"] == "default"


def test_a_corrupt_lock_file_is_reclaimed_not_honoured_forever(tmp_path):
    path = tmp_path / "run.lock"
    path.write_text("{not json", encoding="utf-8")
    with runlock.RunLock(str(path), "default"):
        pass


def test_the_lock_is_released_even_when_the_body_raises(tmp_path):
    path = str(tmp_path / "run.lock")
    with pytest.raises(ValueError):
        with runlock.RunLock(path, "default"):
            raise ValueError("boom")
    assert not os.path.exists(path)


def test_release_leaves_a_lock_it_cannot_prove_is_its_own(tmp_path):
    """An unreadable holder is not proof of ownership. Deleting on a failed
    read would let one process remove a live peer's lock."""
    path = tmp_path / "run.lock"
    lock = runlock.RunLock(str(path), "default")
    path.write_text("{not json", encoding="utf-8")
    lock.release()
    assert path.exists(), "release removed a lock it could not prove was its own"


# --- two processes deciding at once ---------------------------------------
#
# `acquire` used to read the holder, judge it, and take the lock in steps with nothing between
# processes. Two runs that started together both read no holder and both took the lock. The
# guard (an OS lock on a sibling file) makes the steps one.

def _hold_guard(path):
    """Take the guard the way another process in the middle of `acquire` holds it."""
    fd = os.open(path + runlock.GUARD_SUFFIX, os.O_RDWR | os.O_CREAT, 0o600)
    assert runlock._try_lock(fd)
    return fd


def _drop_guard(fd):
    runlock._unlock(fd)
    os.close(fd)


def test_acquire_waits_while_another_process_is_deciding(tmp_path):
    path = str(tmp_path / "run.lock")
    fd = _hold_guard(path)
    done = threading.Event()
    t = threading.Thread(target=lambda: (runlock.RunLock(path, "default").acquire(), done.set()))
    t.start()
    try:
        assert not done.wait(0.3), "acquire went ahead while another process held the guard"
        assert not os.path.exists(path), "the lock was taken without the guard"
    finally:
        _drop_guard(fd)
    assert done.wait(5), "acquire never finished after the guard was released"
    t.join()
    assert json.loads(Path(path).read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_stuck_guard_refuses_instead_of_waiting_for_ever(tmp_path, monkeypatch):
    monkeypatch.setattr(runlock, "GUARD_WAIT", 0.05)
    path = str(tmp_path / "run.lock")
    fd = _hold_guard(path)
    try:
        with pytest.raises(runlock.RunBusy) as excinfo:
            runlock.RunLock(path, "default").acquire()
    finally:
        _drop_guard(fd)
    assert not os.path.exists(path), "took the lock without holding the guard"
    assert str(excinfo.value) == "another scheduled run is in progress"


def test_a_filesystem_that_cannot_lock_still_gets_a_lock(tmp_path, monkeypatch):
    def no_locks(fd):
        raise OSError(37, "No locks available")

    monkeypatch.setattr(runlock, "_try_lock", no_locks)
    path = str(tmp_path / "run.lock")
    runlock.RunLock(path, "default").acquire()
    assert json.loads(Path(path).read_text(encoding="utf-8"))["pid"] == os.getpid()


_CONTENDER = """
import os, sys, time
from pathlib import Path
from contextlake.schedule.runlock import RunBusy, RunLock
lock_path, marker, start = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3])
while time.time() < start:
    pass
try:
    lock = RunLock(lock_path, "job").acquire()
except RunBusy:
    print("busy")
    sys.exit(0)
try:
    os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
except FileExistsError:
    print("overlap")
    sys.exit(0)
time.sleep(0.05)
marker.unlink()
lock.release()
print("held")
"""


def test_runs_that_start_together_do_not_all_take_the_lock(tmp_path):
    """Six processes spin until the same instant, then go for a lock nobody holds. At most
    one holds it at a time: each holder claims a marker file with O_EXCL, so two holders at
    once show up as `overlap`."""
    env = {**os.environ, "PYTHONPATH": str(Path(contextlake.__file__).parents[1])}
    overlaps = held = 0
    for trial in range(4):
        lock_path = tmp_path / f"cache{trial}" / "run.lock"
        marker = tmp_path / f"held{trial}"
        start = time.time() + 0.3   # after every child has started and imported the module
        procs = [subprocess.Popen([sys.executable, "-c", _CONTENDER, str(lock_path),
                                   str(marker), repr(start)],
                                  stdout=subprocess.PIPE, text=True, env=env)
                 for _ in range(6)]
        outcomes = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert all(p.returncode == 0 for p in procs), outcomes
        overlaps += outcomes.count("overlap")
        held += outcomes.count("held")
    assert held >= 4, "no process took the lock in some trials"
    assert overlaps == 0, f"{overlaps} process(es) held the lock at the same time as another"
