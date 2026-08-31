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

    def __reduce__(self):
        """Rebuild from the holder dict, not from ``args``.

        The default ``cls(*self.args)`` passes the formatted message where a
        dict is expected, so unpickling raises ``AttributeError: 'str' object
        has no attribute 'get'``. See ``kb.parse.RepoTooLarge.__reduce__``.
        """
        return (self.__class__, (self.holder,))


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
            # A corrupt file, a dead pid, or a lock written on another host.
            # The host check scopes this guarantee to one machine: on shared
            # storage (NFS, EFS, a shared PVC) a peer on another host can be
            # a live writer this lock cannot see. See RunBusy's own docstring
            # ("on this machine").
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "job": self.job,
                       "host": socket.gethostname()}, fh)
        os.replace(tmp, self.path)
        return self

    def release(self):
        holder = self._read()
        # Positive ownership only. `_read` returns None for an unreadable file,
        # a truncated one, or valid JSON that is not an object, and none of
        # those prove the lock is ours. Deleting on None would let a process
        # remove a live peer's lock on a slow or flaky filesystem, which is the
        # single failure this lock exists to prevent.
        #
        # Nothing leaks by leaving it: `acquire` already reclaims a lock it
        # cannot read or whose pid is dead, so the next run clears it.
        if holder is not None and holder.get("pid") == os.getpid():
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
