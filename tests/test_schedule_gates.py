"""Gates: skip a run when the machine should be left alone.

The property that matters most is the negative one. Every detector returns None
when it cannot tell, and None must ALWAYS pass. A gate that blocks on a sensor
it could not read would silently stop the scheduler on every headless box.
"""
from __future__ import annotations

import pytest

from contextlake.schedule import gates


def test_the_default_config_gates_nothing_on_a_machine_with_no_battery(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({}).allowed is True


@pytest.mark.parametrize("detector", ["on_battery", "user_is_idle", "load_average"])
def test_an_undetectable_signal_always_passes(monkeypatch, detector):
    """THE property. Break this and a container never runs again.

    Each case makes ONE detector undetectable and leaves the other two
    answering safely, so a case fails only for the sensor it names. An
    earlier version patched all three to None in every case, so the three
    cases ran identical code.
    """
    safe = {"on_battery": lambda: False, "user_is_idle": lambda: True,
            "load_average": lambda: 0.0}
    for name, fn in safe.items():
        monkeypatch.setattr(gates, name, (lambda: None) if name == detector else fn)
    config = {"schedule_on_battery": "skip", "schedule_require_idle": "true",
              "schedule_max_load": "2.0"}
    assert gates.check(config).allowed is True


def test_all_three_signals_undetectable_at_once_still_passes(monkeypatch):
    """The real container scenario: no battery, no session, no getloadavg."""
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    config = {"schedule_on_battery": "skip", "schedule_require_idle": "true",
              "schedule_max_load": "2.0"}
    assert gates.check(config).allowed is True


def test_on_battery_skips_by_default(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: True)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    result = gates.check({})
    assert result.allowed is False
    assert "battery" in result.reason.lower()


def test_on_battery_can_be_told_to_run_anyway(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: True)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({"schedule_on_battery": "run"}).allowed is True


def test_mains_power_passes(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: False)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({}).allowed is True


def test_idle_gating_is_off_unless_asked_for(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: False)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({}).allowed is True
    result = gates.check({"schedule_require_idle": "true"})
    assert result.allowed is False
    assert "idle" in result.reason.lower()


def test_load_gating_is_off_unless_a_threshold_is_set(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: 9.0)
    assert gates.check({}).allowed is True
    assert gates.check({"schedule_max_load": "2.0"}).allowed is False
    assert gates.check({"schedule_max_load": "12.0"}).allowed is True


def test_an_unparseable_max_load_disables_the_gate_rather_than_blocking(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: 9.0)
    assert gates.check({"schedule_max_load": "banana"}).allowed is True


def test_the_reason_names_the_gate_that_blocked(monkeypatch):
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: 9.0)
    reason = gates.check({"schedule_max_load": "2.0"}).reason
    assert "load" in reason.lower()
    assert "9" in reason


def test_a_detector_that_raises_reads_as_undetectable(monkeypatch):
    def boom():
        raise OSError("no such sensor")

    monkeypatch.setattr(gates, "on_battery", boom)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({"schedule_on_battery": "skip"}).allowed is True


def test_load_average_is_none_where_the_platform_has_none(monkeypatch):
    monkeypatch.delattr("os.getloadavg", raising=False)
    assert gates.load_average() is None


def test_on_battery_reads_the_sysfs_flag(monkeypatch, tmp_path):
    ac = tmp_path / "AC" / "online"
    ac.parent.mkdir()
    (ac.parent / "type").write_text("Mains\n", encoding="utf-8")
    ac.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(gates, "_POWER_SUPPLY", str(tmp_path))
    assert gates.on_battery() is True
    ac.write_text("1\n", encoding="utf-8")
    assert gates.on_battery() is False


def test_on_battery_is_none_when_sysfs_has_no_mains_supply(monkeypatch, tmp_path):
    bat = tmp_path / "BAT0" / "type"
    bat.parent.mkdir()
    bat.write_text("Battery\n", encoding="utf-8")
    monkeypatch.setattr(gates, "_POWER_SUPPLY", str(tmp_path))
    assert gates.on_battery() is None


def test_an_inert_idle_gate_says_so(monkeypatch, gls_logs):
    """A setting that cannot take effect must not be silently ignored.
    XDG_SESSION_ID is unset under systemd timers and cron, so user_is_idle
    returns None there and this gate never fires."""
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: None)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    result = gates.check({"schedule_require_idle": "true"})
    assert result.allowed is True
    assert "inert" in gls_logs.text.lower()


def test_a_working_idle_gate_says_nothing(monkeypatch, gls_logs):
    """No warning when the sensor works. Otherwise the message is noise on
    every run of a machine where the gate is doing its job."""
    monkeypatch.setattr(gates, "on_battery", lambda: None)
    monkeypatch.setattr(gates, "user_is_idle", lambda: True)
    monkeypatch.setattr(gates, "load_average", lambda: None)
    assert gates.check({"schedule_require_idle": "true"}).allowed is True
    assert "inert" not in gls_logs.text.lower()
