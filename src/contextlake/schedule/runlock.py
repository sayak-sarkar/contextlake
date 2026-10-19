"""One scheduled run at a time.

`kb/lock.py` already does this for the knowledge store, but it lives in the
optional ``[kb]`` tier and ``schedule run`` has to work without it. Same design,
same reasoning: a pidfile with a liveness check, so a lock left behind by a
crashed process is reclaimed rather than honoured forever.

Advisory, like its sibling. Two runs that overlap SKIP; they never queue a
second writer.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import time
from contextlib import contextmanager

FILENAME = "schedule-run.lock"
# The OS lock that makes the read, judge and take steps of `acquire` one step between
# processes. Its file sits beside the lock and stays on disk, as `kb/lock.py`'s does:
# deleting it would let one process lock the old file while another creates and locks a new one.
GUARD_SUFFIX = ".guard"
# A holder keeps the guard for one read, one liveness check and one small file write,
# which is milliseconds. One still there after this long is stuck, so the caller is refused.
GUARD_WAIT = 5.0
GUARD_POLL = 0.01


class RunBusy(RuntimeError):
    """Another scheduled run is already in progress on this machine."""

    def __init__(self, holder):
        self.holder = holder
        # A holder that could not be read (a stuck guard) names nobody.
        who = (f": pid {holder.get('pid')} running job {holder.get('job')!r}"
               if holder.get("pid") else "")
        super().__init__(f"another scheduled run is in progress{who}")

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


def _try_lock(fd) -> bool:
    """Take an OS lock on ``fd`` without waiting. False when another holder has it.

    ``flock`` and not ``lockf``: a ``flock`` belongs to the open file description, so two
    descriptors conflict even inside one process. The kernel drops the lock when the holder
    exits, so a crashed run leaves nothing to clean up. Same as ``kb.keyfile._try_lock``,
    which this core-tier module cannot import. ``fcntl`` and ``msvcrt`` are imported here
    because only one of them exists on a given platform.
    """
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EACCES)):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


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

    @contextmanager
    def _guard(self):
        """Hold the guard while a process decides whether the lock is free and takes it.

        Without it, two runs that started together both read no holder, both judged the
        lock free and both took it. An OS lock, so the kernel drops it when its holder
        exits and a guard is never stale. A filesystem that cannot lock leaves the step
        unguarded, which is what it was before the guard.
        """
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path + GUARD_SUFFIX, flags, 0o600)
        except OSError:
            fd = None
        locked = False
        try:
            if fd is not None:
                for _ in range(round(GUARD_WAIT / GUARD_POLL)):
                    try:
                        locked = _try_lock(fd)
                    except OSError:
                        break
                    if locked:
                        break
                    time.sleep(GUARD_POLL)
                else:
                    raise RunBusy(self._read() or {})
            yield
        finally:
            if fd is not None:
                if locked:
                    try:
                        _unlock(fd)
                    except OSError:
                        pass  # closing the descriptor below drops the lock either way
                os.close(fd)

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._guard():
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
