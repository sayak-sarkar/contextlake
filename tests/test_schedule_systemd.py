"""The systemd adapter: what it renders, and that it really installs.

Golden-file assertions on `render` (pure, so no root, no systemd), plus a real
install/fire/uninstall cycle, because systemd IS init on this machine and a
renderable-but-broken unit is what golden files cannot catch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from contextlake.schedule import jobs
from contextlake.schedule.platform import base, systemd

HAS_SYSTEMD = shutil.which("systemctl") is not None and base.get("systemd").usable()
needs_systemd = pytest.mark.skipif(not HAS_SYSTEMD, reason="systemd is not init here")


def _job(name="default", argv=("version",)):
    return jobs.new_job(name, list(argv), "auto", "systemd")


def _exec_argv(name="default"):
    return [sys.executable, "-m", "contextlake", "schedule", "run", "--job", name]


# ---- render (pure) ------------------------------------------------------

def test_render_produces_a_timer_and_a_service():
    files = systemd.SystemdAdapter().render(_job(), 4200.0, _exec_argv())
    assert sorted(files) == ["contextlake-default.service", "contextlake-default.timer"]


def test_the_timer_uses_the_exact_interval_not_a_rounded_one():
    files = systemd.SystemdAdapter().render(_job(), 4200.0, _exec_argv())
    assert "OnUnitInactiveSec=4200s" in files["contextlake-default.timer"]


def test_the_timer_catches_up_after_the_machine_sleeps():
    timer = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.timer"]
    assert "Persistent=true" in timer
    assert systemd.SystemdAdapter().catches_up_after_sleep is True


def test_the_service_lets_systemd_do_the_battery_gate():
    """ConditionACPower needs no code at all, so it is strictly better than
    reading /sys ourselves."""
    service = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.service"]
    assert "ConditionACPower=true" in service


def test_battery_gating_can_be_turned_off_in_the_unit():
    adapter = systemd.SystemdAdapter()
    service = adapter.render(_job(), 3600.0, _exec_argv(),
                             on_battery="run")["contextlake-default.service"]
    assert "ConditionACPower" not in service


def test_the_service_runs_the_resolved_interpreter_not_a_bare_name():
    """A moved venv otherwise fails silently forever."""
    service = systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())[
        "contextlake-default.service"]
    assert f"ExecStart={sys.executable} -m contextlake schedule run --job default" in service


def test_a_job_name_with_a_slash_or_a_space_is_refused():
    """The name becomes a filename and a unit name. Anything that could escape
    the directory or split the unit name is refused at render time."""
    for bad in ("../evil", "with space", "with/slash", "", "a" * 200):
        with pytest.raises(ValueError):
            systemd.SystemdAdapter().render(_job(name=bad), 3600.0, _exec_argv())


def test_render_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    systemd.SystemdAdapter().render(_job(), 3600.0, _exec_argv())
    assert list(tmp_path.iterdir()) == []


def test_the_rendered_unit_matches_the_golden_file():
    """Pins the whole text, so an accidental change to any directive is caught
    rather than only the three lines other tests assert on."""
    files = systemd.SystemdAdapter().render(_job(), 4200.0,
                                            ["/opt/py", "-m", "contextlake",
                                             "schedule", "run", "--job", "default"])
    assert files["contextlake-default.timer"] == (
        "# Managed by contextlake. Edits are overwritten by `contextlake schedule install`.\n"
        "[Unit]\n"
        "Description=contextlake scheduled run (default)\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2m\n"
        "OnUnitInactiveSec=4200s\n"
        "Persistent=true\n"
        "AccuracySec=1m\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n")


# ---- detection ----------------------------------------------------------

def test_detect_returns_an_id_that_get_accepts():
    assert base.get(base.detect()) is not None


def test_an_unknown_platform_name_is_a_clear_error():
    with pytest.raises(base.NoAdapter) as excinfo:
        base.get("windows-3.1")
    assert "systemd" in str(excinfo.value), "the error must list what IS available"


def test_available_never_claims_an_unusable_adapter():
    for name in base.available():
        assert base.get(name).usable() is True


# ---- the real thing -----------------------------------------------------

@needs_systemd
def test_install_then_fire_then_uninstall(tmp_path, monkeypatch):
    """END TO END on the real init system. A unit that renders but does not
    fire is the failure golden files cannot see."""
    # The suite-wide `_isolated_home` fixture (tests/conftest.py) points HOME
    # at a throwaway directory so no test can write into the real one. This
    # test is the sanctioned exception: it must reach the systemd --user
    # instance already running for the real user, and that instance resolves
    # unit files under the real home, not this test's fake one. `pwd`, not
    # the environment, gives the real value regardless of what HOME is set to.
    import pwd

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)

    name = "selftest"
    unit = systemd.unit_name(name)  # contextlake-selftest
    job = jobs.new_job(name, ["version"], "1m", "systemd")
    adapter = systemd.SystemdAdapter()
    written = adapter.install(job, 60.0, _exec_argv(name))
    try:
        assert written
        assert adapter.state(job)["installed"] is True
        out = subprocess.run(["systemctl", "--user", "list-timers", f"{unit}.timer"],
                             capture_output=True, text=True, check=False)
        assert unit in out.stdout
        # Fire it now rather than waiting a minute for the timer.
        subprocess.run(["systemctl", "--user", "start", f"{unit}.service"], check=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            result = subprocess.run(
                ["systemctl", "--user", "show", f"{unit}.service",
                 "-p", "ExecMainStatus", "-p", "ActiveState"],
                capture_output=True, text=True, check=False)
            if "ActiveState=inactive" in result.stdout:
                break
            time.sleep(1)
        else:
            pytest.fail("the unit never completed within 60s")
        assert "ExecMainStatus=0" in result.stdout
    finally:
        adapter.uninstall(job)
    assert adapter.state(job)["installed"] is False
    leftovers = subprocess.run(["systemctl", "--user", "list-unit-files", f"{unit}*"],
                               capture_output=True, text=True, check=False)
    assert unit not in leftovers.stdout


@needs_systemd
def test_uninstalling_something_that_is_not_there_is_not_an_error():
    adapter = systemd.SystemdAdapter()
    assert adapter.uninstall(_job(name="never-installed")) == []


def test_the_unit_name_is_always_prefixed_exactly_once():
    """One naming rule. An earlier draft prefixed conditionally, which gave two
    rules that were each individually tested and each passing."""
    assert systemd.unit_name("default") == "contextlake-default"
    assert systemd.unit_name("selftest") == "contextlake-selftest"


def test_render_install_uninstall_and_state_agree_on_the_filename(monkeypatch, tmp_path):
    """The four methods must derive the same names, or uninstall leaves files
    behind and state reports a unit that is really there as missing."""
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    # `state()` reads `.stdout` off whatever `_systemctl` returns, the same
    # as the real `subprocess.run` result it stands in for here. A stub
    # that returns `None` crashes `state()` before the naming assertions
    # below run at all, so this test would fail the same way whether the
    # naming agreed or not. That is not what this test is for.
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    job = _job(name="agree")
    adapter = systemd.SystemdAdapter()
    rendered = set(adapter.render(job, 3600.0, _exec_argv("agree")))
    written = {p.rsplit("/", 1)[-1] for p in adapter.install(job, 3600.0, _exec_argv("agree"))}
    assert rendered == written
    assert adapter.timer_unit(job) in rendered
    assert adapter.state(job)["installed"] is True
    removed = {p.rsplit("/", 1)[-1] for p in adapter.uninstall(job)}
    assert removed == written
    assert adapter.state(job)["installed"] is False
    assert list(tmp_path.iterdir()) == []


@needs_systemd
def test_state_reports_whether_linger_is_enabled():
    """A user timer does NOT fire while logged out unless linger is on. This
    machine has Linger=no, so the adapter must SAY so rather than let the user
    believe a schedule is running."""
    notes = systemd.SystemdAdapter().state(_job())["notes"]
    assert any("linger" in note.lower() for note in notes)
