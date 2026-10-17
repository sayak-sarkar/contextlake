"""Tests for the store single-writer lock (kb/lock.py)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import contextlake
from contextlake.kb import lock as lock_mod
from contextlake.kb.keyfile import _try_lock, _unlock
from contextlake.kb.lock import GUARD_NAME, LOCK_NAME, OVERRIDE_ENV, StoreBusy, StoreLock


def _write_lock(store_dir, *, pid, host=None):
    (store_dir / LOCK_NAME).write_text(json.dumps({
        "pid": pid, "command": "wiki", "host": host or socket.gethostname(),
        "started": 0,
    }))


def test_acquire_release_roundtrip(tmp_path):
    lock = StoreLock(tmp_path, "index")
    lock.acquire()
    assert (tmp_path / LOCK_NAME).exists()
    lock.release()
    assert not (tmp_path / LOCK_NAME).exists()


def test_same_process_reclaims_not_busy(tmp_path):
    # A leftover lock from *this* pid is never "busy" (bootstrap runs stages in one
    # process); it is reclaimed silently.
    _write_lock(tmp_path, pid=os.getpid())
    StoreLock(tmp_path, "embed").acquire()   # must not raise
    holder = json.loads((tmp_path / LOCK_NAME).read_text())
    assert holder["command"] == "embed"


def test_live_peer_is_busy(tmp_path):
    # A different, live pid (our parent) on the same host -> refuse.
    _write_lock(tmp_path, pid=os.getppid())
    with pytest.raises(StoreBusy):
        StoreLock(tmp_path, "index").acquire()


def test_stale_pid_is_reclaimed(tmp_path):
    _write_lock(tmp_path, pid=2_147_400_000)   # implausible / dead pid
    StoreLock(tmp_path, "index").acquire()     # must not raise
    assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()


def test_other_host_is_reclaimed(tmp_path):
    _write_lock(tmp_path, pid=os.getppid(), host="some-other-host")
    StoreLock(tmp_path, "index").acquire()     # different host -> not our peer


def test_override_env_bypasses_busy(tmp_path, monkeypatch):
    _write_lock(tmp_path, pid=os.getppid())
    monkeypatch.setenv(OVERRIDE_ENV, "1")
    StoreLock(tmp_path, "index").acquire()     # override -> reclaim, no raise


def test_release_only_removes_our_lock(tmp_path):
    lock = StoreLock(tmp_path, "index")
    lock.acquire()
    _write_lock(tmp_path, pid=os.getppid())    # someone else "took over" the file
    lock.release()
    assert (tmp_path / LOCK_NAME).exists()      # we must not delete a foreign lock


# --- two processes deciding at once ---------------------------------------
#
# `acquire` used to read the holder, judge it, and take the lock in three steps with nothing
# between processes. Two that read the same dead holder both took the lock, and one that read
# the file between its creation and its first byte took the lock from a live creator. The
# guard (an OS lock on a sibling file) makes the three steps one.

_DEAD_PID = 2_147_400_000   # implausible / dead pid, as in the stale-pid test above


def _hold_guard(store_dir):
    """Take the guard the way another process in the middle of `acquire` holds it."""
    fd = os.open(store_dir / GUARD_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    assert _try_lock(fd)
    return fd


def _drop_guard(fd):
    _unlock(fd)
    os.close(fd)


def test_acquire_waits_while_another_process_is_deciding(tmp_path):
    _write_lock(tmp_path, pid=_DEAD_PID)
    fd = _hold_guard(tmp_path)
    done = threading.Event()
    t = threading.Thread(target=lambda: (StoreLock(tmp_path, "index").acquire(), done.set()))
    t.start()
    try:
        assert not done.wait(0.3), "acquire went ahead while another process held the guard"
        assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == _DEAD_PID
    finally:
        _drop_guard(fd)
    assert done.wait(5), "acquire never finished after the guard was released"
    t.join()
    assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()


def test_a_stuck_guard_refuses_instead_of_waiting_for_ever(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "GUARD_WAIT", 0.05)
    fd = _hold_guard(tmp_path)
    try:
        with pytest.raises(StoreBusy):
            StoreLock(tmp_path, "index").acquire()
    finally:
        _drop_guard(fd)
    assert not (tmp_path / LOCK_NAME).exists(), "took the lock without holding the guard"


def test_a_filesystem_that_cannot_lock_still_gets_a_lock(tmp_path, monkeypatch):
    def no_locks(fd):
        raise OSError(37, "No locks available")

    monkeypatch.setattr(lock_mod, "_try_lock", no_locks)
    _write_lock(tmp_path, pid=_DEAD_PID)
    StoreLock(tmp_path, "index").acquire()
    assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()


def test_a_lock_file_never_exists_without_its_content(tmp_path, monkeypatch):
    """The file used to be created empty and written a moment later. A process that read it
    in between saw no holder, judged the lock stale and took it from a live creator."""
    real_fdopen = os.fdopen
    at_write, proceed = threading.Event(), threading.Event()

    class _SlowWrite:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

        def write(self, data):
            at_write.set()
            proceed.wait(5)
            return self._f.write(data)

    def fdopen(fd, mode="r", *a, **k):
        f = real_fdopen(fd, mode, *a, **k)
        return _SlowWrite(f) if mode == "wb" else f

    monkeypatch.setattr(lock_mod.os, "fdopen", fdopen)
    t = threading.Thread(target=lambda: StoreLock(tmp_path, "index").acquire())
    t.start()
    try:
        assert at_write.wait(5)
        seen = (tmp_path / LOCK_NAME)
        assert not seen.exists() or seen.read_bytes() != b"", (
            "the lock file is visible with no content, so a peer reads it as stale")
    finally:
        proceed.set()
        t.join()
    assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == os.getpid()


_CONTENDER = """
import os, sys, time
from pathlib import Path
from contextlake.kb.lock import StoreBusy, StoreLock
store, marker, start = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
while time.time() < start:
    pass
try:
    lock = StoreLock(store, "index").acquire()
except StoreBusy:
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


def test_processes_that_find_a_dead_holder_together_do_not_all_take_the_lock(tmp_path):
    """Six processes spin until the same instant, then go for a lock left by a dead pid. At
    most one holds it at a time: each holder claims a marker file with O_EXCL, so two holders
    at once show up as `overlap`. Before the guard, 22 of 30 such trials had more than one."""
    env = {**os.environ, "PYTHONPATH": str(Path(contextlake.__file__).parents[1])}
    overlaps = held = 0
    for trial in range(4):
        store = tmp_path / f"store{trial}"
        store.mkdir()
        _write_lock(store, pid=_DEAD_PID)
        marker = tmp_path / f"held{trial}"
        start = time.time() + 0.3   # after every child has started and imported the module
        procs = [subprocess.Popen([sys.executable, "-c", _CONTENDER, str(store), str(marker),
                                   repr(start)], stdout=subprocess.PIPE, text=True, env=env)
                 for _ in range(6)]
        outcomes = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert all(p.returncode == 0 for p in procs), outcomes
        overlaps += outcomes.count("overlap")
        held += outcomes.count("held")
    assert held >= 4, "no process took the lock in some trials"
    assert overlaps == 0, f"{overlaps} process(es) held the lock at the same time as another"
