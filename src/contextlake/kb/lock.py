"""A cooperative single-writer lock on the knowledge store.

Two contextlake writers on one store race on SQLite and can interleave shard
writes mid-operation (observed: a manual ``wiki`` run and a ``bootstrap`` both
targeting the same ``store_dir``). This advisory lock lets a write command detect
a *live* peer and refuse rather than corrupt, while transparently reclaiming a
lock left behind by a crashed process (stale PID) or another host.

Read commands (query/graph/serve/doctor) never take the lock — concurrent reads
are safe.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .keyfile import _try_lock, _unlock

LOCK_NAME = ".contextlake.lock"
# The OS lock that makes the read, judge and take steps of `acquire` one step between
# processes. It stays on disk, as the key file's lock does: deleting it would let one
# process lock the old file while another creates and locks a new one.
GUARD_NAME = ".contextlake.lock.guard"
# A holder keeps the guard for one read, one liveness check and one small file write,
# which is milliseconds. One still there after this long is stuck, so the caller is refused.
GUARD_WAIT = 5.0
GUARD_POLL = 0.01
# Set this to run a second writer anyway (rarely correct; you own the risk).
OVERRIDE_ENV = "CONTEXTLAKE_ALLOW_CONCURRENT"


class StoreBusy(RuntimeError):
    """Raised when a live peer already holds the store's write lock."""

    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(
            f"store is being written by pid {holder.get('pid')} "
            f"({holder.get('command')})"
        )

    def __reduce__(self):
        """Rebuild from the holder dict, not from ``args``.

        The default ``cls(*self.args)`` passes the formatted message where a
        dict is expected, so unpickling raises ``AttributeError: 'str' object
        has no attribute 'get'``. See ``kb.parse.RepoTooLarge.__reduce__`` for
        what that costs when it happens inside a worker pool.
        """
        return (self.__class__, (self.holder,))


def _alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but not ours to signal
    return True


class StoreLock:
    """Advisory single-writer lock realised as ``<store_dir>/.contextlake.lock``."""

    def __init__(self, store_dir, command: str):
        self.path = Path(store_dir) / LOCK_NAME
        self.command = command
        self._held = False

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @contextmanager
    def _guard(self):
        """Hold the guard while a process decides whether the lock is stale and takes it.

        Without it, two processes that read the same dead holder both judged it stale and
        both took the lock, and a process that read the file between its creation and its
        first byte took the lock from a live creator. An OS lock, so the kernel drops it
        when its holder exits and a guard is never stale. A filesystem that cannot lock
        leaves the step unguarded, which is what it was before the guard.
        """
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path.with_name(GUARD_NAME), flags, 0o644)
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
                    raise StoreBusy(self._read())
            yield
        finally:
            if fd is not None:
                if locked:
                    try:
                        _unlock(fd)
                    except OSError:
                        pass  # closing the descriptor below drops the lock either way
                os.close(fd)

    def acquire(self) -> StoreLock:
        """Take the lock, reclaiming a stale one. Raises :class:`StoreBusy` on a
        live peer (unless ``CONTEXTLAKE_ALLOW_CONCURRENT`` is set)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "pid": os.getpid(), "command": self.command,
            "host": socket.gethostname(), "started": int(time.time()),
        }).encode()
        with self._guard():
            holder = self._read()
            pid = holder.get("pid", 0)
            same_host = holder.get("host") == socket.gethostname()
            live_peer = same_host and pid != os.getpid() and _alive(pid)
            if live_peer and not os.environ.get(OVERRIDE_ENV):
                raise StoreBusy(holder)
            # Free, stale (crashed / other host) or self / overridden: take it. Written to a
            # temp file and renamed in, so no reader sees a lock file that has no content yet.
            tmp = self.path.with_name(f"{LOCK_NAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self._read().get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self._held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
