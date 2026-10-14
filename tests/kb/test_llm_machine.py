"""The machine check: memory, cgroup room and disk, read with the standard library."""

from __future__ import annotations

from contextlake.kb.llm import machine

GB = 1024 ** 3


def test_meminfo_is_read_in_bytes(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemFree:  1 kB\n"
                       "MemAvailable:   12000000 kB\n")
    assert machine._linux_memory(meminfo) == (16000000 * 1024, 12000000 * 1024)


def test_unreadable_meminfo_is_unknown_not_zero(tmp_path):
    assert machine._linux_memory(tmp_path / "absent") == (None, None)


def _cgroup(tmp_path, path, levels):
    """A cgroup v2 tree: ``levels`` maps a path below the root to (max, current)."""
    root = tmp_path / "cgroup"
    for rel, (limit, used) in levels.items():
        d = root / rel.lstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.max").write_text(f"{limit}\n")
        (d / "memory.current").write_text(f"{used}\n")
    self_cgroup = tmp_path / "self-cgroup"
    self_cgroup.write_text(f"0::{path}\n")
    return root, self_cgroup


def test_a_container_limit_at_the_root_is_the_room(tmp_path):
    root, me = _cgroup(tmp_path, "/", {"/": (4 * GB, 1 * GB)})
    assert machine._cgroup_room(root, me) == 3 * GB


def test_the_tightest_limit_on_the_path_wins(tmp_path):
    """A systemd scope nests its limit below a parent that may set its own."""
    root, me = _cgroup(tmp_path, "/user.slice/app.slice/run-x.scope", {
        "/user.slice": (8 * GB, 2 * GB),
        "/user.slice/app.slice/run-x.scope": (2 * GB, 0),
    })
    assert machine._cgroup_room(root, me) == 2 * GB


def test_no_limit_anywhere_is_none(tmp_path):
    root, me = _cgroup(tmp_path, "/user.slice", {"/user.slice": ("max", 0)})
    assert machine._cgroup_room(root, me) is None


def test_the_cgroup_room_caps_what_meminfo_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(machine, "_memory", lambda: (64 * GB, 60 * GB))
    monkeypatch.setattr(machine, "_cgroup_room", lambda: 3 * GB)
    found = machine.read_machine(tmp_path)
    assert (found.ram_total, found.ram_available) == (64 * GB, 3 * GB)


def test_disk_is_read_from_the_nearest_existing_folder(tmp_path):
    assert machine._disk_free(tmp_path / "not" / "made" / "yet") is not None
