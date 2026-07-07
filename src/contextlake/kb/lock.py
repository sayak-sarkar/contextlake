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
from pathlib import Path

LOCK_NAME = ".contextlake.lock"
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

    def acquire(self) -> StoreLock:
        """Take the lock, reclaiming a stale one. Raises :class:`StoreBusy` on a
        live peer (unless ``CONTEXTLAKE_ALLOW_CONCURRENT`` is set)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "pid": os.getpid(), "command": self.command,
            "host": socket.gethostname(), "started": int(time.time()),
        }).encode()
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = self._read()
            pid = holder.get("pid", 0)
            same_host = holder.get("host") == socket.gethostname()
            live_peer = same_host and pid != os.getpid() and _alive(pid)
            if live_peer and not os.environ.get(OVERRIDE_ENV):
                raise StoreBusy(holder) from None
            # Stale (crashed / other host) or self / overridden: reclaim it.
            fd = os.open(str(self.path), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
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
