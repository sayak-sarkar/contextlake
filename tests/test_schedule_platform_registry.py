"""Every registered adapter's `state()` must return the five contract keys.

`cmds.py` reads each with `.get()`, so a backend that omits one fails
silently: the missing-interpreter row simply never fires, and nothing says
why. `base.STATE_KEYS` names the contract; this file enforces it against
every adapter in `base._registry()` rather than trusting the docstring,
so a sixth backend (Plan 2 adds six) that forgets a key fails a test instead
of shipping quiet.

Real `state()` calls shell out (`crontab -l`, `systemctl show`), so every
adapter here gets its subprocess boundary stubbed rather than invoked for
real: `cron._read_crontab` and `systemd._systemctl` are the seams.
"""
from __future__ import annotations

import subprocess

import pytest

from contextlake.schedule import jobs
from contextlake.schedule.platform import base, cron, k8s, launchd, systemd, windows


def _stub_cron_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(cron, "_read_crontab", lambda: "")


def _stub_cron_installed(monkeypatch, tmp_path):
    name = "default"
    block = (cron.BEGIN.format(name=name) + "\n"
             + 'MAILTO=""\n'
             + "0 * * * * /usr/bin/python3 -m contextlake schedule run --job default\n"
             + cron.END.format(name=name) + "\n")
    monkeypatch.setattr(cron, "_read_crontab", lambda: block)


def _stub_systemd_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")


def _stub_systemd_installed(monkeypatch, tmp_path):
    name = "default"
    timer = systemd.unit_name(name) + ".timer"
    service = systemd.unit_name(name) + ".service"
    (tmp_path / timer).write_text(
        systemd.HEADER + "[Timer]\nOnUnitInactiveSec=3600s\n", encoding="utf-8")
    (tmp_path / service).write_text(systemd.HEADER + "[Service]\n", encoding="utf-8")
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))

    def _fake_systemctl(*argv, check=False):
        if "NextElapseUSecRealtime" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="NextElapseUSecRealtime=Mon 2026-08-31 00:00:00 UTC\n", stderr="")
        if "ExecStart" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="ExecStart={ path=/usr/bin/python3 ; argv[]=... }\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "_systemctl", _fake_systemctl)
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")


def _stub_launchd_not_installed(monkeypatch, tmp_path):
    """launchd's state() reads the plist off disk and shells out to nothing,
    so pointing the agent directory at an empty tmp_path is the whole stub."""
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))


def _stub_launchd_installed(monkeypatch, tmp_path):
    import plistlib

    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    (tmp_path / launchd.plist_name("default")).write_bytes(plistlib.dumps({
        "Label": launchd.label("default"),
        "ProgramArguments": ["/usr/bin/python3", "-m", "contextlake",
                             "schedule", "run", "--job", "default"],
        "StartInterval": 3600,
    }))


def _stub_windows_not_installed(monkeypatch, tmp_path):
    """schtasks /Query exits non-zero when the task is absent."""
    monkeypatch.setattr(windows, "_schtasks",
                        lambda *a: subprocess.CompletedProcess(a, 1, stdout="", stderr=""))


def _stub_windows_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "_schtasks", lambda *a: subprocess.CompletedProcess(
        a, 0,
        stdout=("TaskName:      \\contextlake\\default\n"
                "Next Run Time: 31/08/2026 00:00:00\n"
                "Task To Run:   C:\\venv\\python.exe -m contextlake bootstrap\n"),
        stderr=""))


def _stub_k8s_not_installed(monkeypatch, tmp_path):
    """`kubectl get cronjob` exits non-zero when the resource is absent."""
    monkeypatch.setattr(k8s, "_run",
                        lambda *a: subprocess.CompletedProcess(a, 1, stdout="", stderr=""))


def _stub_k8s_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(k8s, "_run", lambda *a: subprocess.CompletedProcess(
        a, 0, stdout="0 * * * *|2026-08-28T00:00:00Z", stderr=""))


# One (not-installed, installed) stub pair per known adapter id. An adapter
# name in `base._registry()` with no entry here fails the test below by
# design: better a loud test failure than `state()` shelling out for real.
_STUBS = {
    "cron": (_stub_cron_not_installed, _stub_cron_installed),
    "k8s": (_stub_k8s_not_installed, _stub_k8s_installed),
    "launchd": (_stub_launchd_not_installed, _stub_launchd_installed),
    "systemd": (_stub_systemd_not_installed, _stub_systemd_installed),
    "windows": (_stub_windows_not_installed, _stub_windows_installed),
}


@pytest.mark.parametrize("name", sorted(base._registry()))
def test_every_registered_adapter_has_a_stub_pair(name):
    """A new adapter with no stub pair here would otherwise be silently
    skipped by pytest's parametrize instead of failing, which defeats the
    point of enumerating the registry in the first place."""
    assert name in _STUBS, (
        f"adapter {name!r} is registered but has no subprocess stub in "
        f"tests/test_schedule_platform_registry.py; add a (not-installed, "
        f"installed) stub pair before shipping it, or state() will shell "
        f"out for real under test.")


@pytest.mark.parametrize("name", sorted(_STUBS))
@pytest.mark.parametrize("installed", [False, True], ids=["not-installed", "installed"])
def test_state_returns_every_contract_key(name, installed, monkeypatch, tmp_path):
    stub_not_installed, stub_installed = _STUBS[name]
    (stub_installed if installed else stub_not_installed)(monkeypatch, tmp_path)

    adapter = base._registry()[name]()
    job = jobs.new_job("default", ["bootstrap"], "auto", name)
    state = adapter.state(job)

    missing = [key for key in base.STATE_KEYS if key not in state]
    assert not missing, f"{name} state() (installed={installed}) is missing keys: {missing}"
    assert state["installed"] is installed


def test_detect_never_picks_a_cluster_adapter_implicitly(monkeypatch):
    """k8s answers usable() whenever kubectl is on PATH, which is true on
    plenty of workstations that are not running in a cluster. Scheduling a
    laptop job into whatever cluster the kubeconfig points at is not a guess
    detect() is allowed to make.

    Forces every machine-local adapter to refuse, so k8s is the ONLY usable
    one. If detect() consulted the registry rather than its own list, it would
    return "k8s" here instead of raising.
    """
    import pytest as _pytest

    for mod in (systemd, cron, launchd, windows):
        monkeypatch.setattr(mod, "shutil", type("S", (), {"which": staticmethod(lambda _c: None)}),
                            raising=False)
    monkeypatch.setattr(base, "get", lambda name: type(
        "A", (), {"usable": staticmethod(lambda: name == "k8s")})())

    with _pytest.raises(base.NoAdapter):
        base.detect()
