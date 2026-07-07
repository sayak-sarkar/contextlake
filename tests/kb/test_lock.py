"""Tests for the store single-writer lock (kb/lock.py)."""
import json
import os
import socket

import pytest

from contextlake.kb.lock import LOCK_NAME, OVERRIDE_ENV, StoreBusy, StoreLock


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
