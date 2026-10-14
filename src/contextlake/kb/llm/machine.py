"""What this machine can run a model with: memory, disk and CPUs.

Standard library only: the core install has one dependency, and this runs before any
model library is imported. A value that cannot be read is ``None``, and the callers treat
an unknown as "does not fit" rather than guessing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Machine:
    cpus: int
    ram_total: int | None        # bytes
    ram_available: int | None    # bytes a new process can use now
    disk_free: int | None        # bytes free where models are stored


def read_machine(cache_dir: str | os.PathLike) -> Machine:
    total, available = _memory()
    limit = _cgroup_room()
    if limit is not None:
        # In a container, /proc/meminfo reports the HOST. The cgroup limit is what this
        # process can actually use, and a model sized to the host is killed at load.
        available = limit if available is None else min(available, limit)
    return Machine(cpus=_cpus(), ram_total=total, ram_available=available,
                   disk_free=_disk_free(Path(cache_dir).expanduser()))


def _cpus() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def _memory() -> tuple[int | None, int | None]:
    if sys.platform.startswith("linux"):
        return _linux_memory()
    if sys.platform == "darwin":
        return _mac_memory()
    if sys.platform == "win32":
        return _windows_memory()
    return None, None


def _linux_memory(meminfo: Path = Path("/proc/meminfo")) -> tuple[int | None, int | None]:
    fields = {}
    try:
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                fields[key] = int(parts[0]) * 1024     # the file reports kB
    except OSError:
        return None, None
    return fields.get("MemTotal"), fields.get("MemAvailable")


def _cgroup_room(root: Path = Path("/sys/fs/cgroup"),
                 self_cgroup: Path = Path("/proc/self/cgroup")) -> int | None:
    """Bytes left under the tightest cgroup v2 memory limit on this process's path, or
    ``None`` when no limit applies. A container's namespace shows its limit at the root;
    a systemd scope shows it on a nested path, so every level is read."""
    try:
        line = next((ln for ln in self_cgroup.read_text().splitlines()
                     if ln.startswith("0::")), None)
    except OSError:
        return None
    if line is None:
        return None
    here = root / line[3:].strip().lstrip("/")
    rooms = []
    for level in (here, *here.parents):
        try:
            raw = (level / "memory.max").read_text().strip()
            if raw != "max":
                used = int((level / "memory.current").read_text().strip())
                rooms.append(max(0, int(raw) - used))
        except (OSError, ValueError):
            pass   # no limit file at this level
        if level == root:
            break
    return min(rooms) if rooms else None


def _mac_memory() -> tuple[int | None, int | None]:
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True, errors="replace", check=True,
                                   timeout=5).stdout.strip())
        stat = subprocess.run(["vm_stat"], capture_output=True, text=True, errors="replace",
                              check=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    page = 4096
    pages = {}
    for line in stat.splitlines():
        if "page size of" in line:
            page = int(line.split("page size of")[1].split()[0])
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            pages[key.strip()] = int(value)
    free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative"))
    return total, free * page


def _windows_memory() -> tuple[int | None, int | None]:
    import ctypes

    class _Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = _Status()
    status.dwLength = ctypes.sizeof(_Status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
    except (AttributeError, OSError):
        return None, None
    return status.ullTotalPhys, status.ullAvailPhys


def _disk_free(path: Path) -> int | None:
    """Free bytes on the disk that holds ``path``, from its nearest existing parent."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                return None
    return None
