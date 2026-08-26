"""One scheduled run at a time.

`kb/lock.py` already does this for the knowledge store, but it lives in the
optional ``[kb]`` tier and ``schedule run`` has to work without it. Same design,
same reasoning: a pidfile with a liveness check, so a lock left behind by a
crashed process is reclaimed rather than honoured forever.

Advisory, like its sibling. Two runs that overlap SKIP; they never queue a
second writer.
"""
from __future__ import annotations

import json
import os
import socket

FILENAME = "schedule-run.lock"


class RunBusy(RuntimeError):
    """Another scheduled run is already in progress on this machine."""

    def __init__(self, holder):
        self.holder = holder
        super().__init__(
            f"another scheduled run is in progress: pid {holder.get('pid')} "
            f"running job {holder.get('job')!r}")


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, but it exists. Honour it.
        return True
    except OSError:
        return False
    return True


class RunLock:
    """Context manager. Raises :class:`RunBusy` when a live peer holds it."""

    def __init__(self, path, job):
        self.path = str(path)
        self.job = str(job)

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                holder = json.load(fh)
        except (OSError, ValueError):
            return None
        return holder if isinstance(holder, dict) else None

    def acquire(self):
        holder = self._read()
        if holder is not None:
            same_host = holder.get("host") == socket.gethostname()
            if same_host and _alive(holder.get("pid")):
                raise RunBusy(holder)
            # A corrupt file, a dead pid, or a lock written on another host
            # against a shared directory. None of those is a live writer.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "job": self.job,
                       "host": socket.gethostname()}, fh)
        os.replace(tmp, self.path)
        return self

    def release(self):
        holder = self._read()
        # Only remove our own. Reclaiming a stale lock and then being outrun by
        # a third process must not delete that process's live lock.
        if holder is None or holder.get("pid") == os.getpid():
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def runlock_path(config) -> str:
    from .history import history_path

    return os.path.join(os.path.dirname(history_path(config)) or ".", FILENAME)
