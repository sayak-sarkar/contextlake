"""The single-writer lock for scheduled runs.

kb/lock.py already does this for the knowledge store, but it lives in the
optional [kb] tier and `schedule run` must work without it. Same design: a
pidfile, a liveness check, and a stale lock reclaimed rather than honoured
forever.
"""
from __future__ import annotations

import json
import os

import pytest

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
