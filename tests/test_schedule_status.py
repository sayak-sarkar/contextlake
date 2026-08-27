"""`schedule status`: every way a schedule can be silently dead."""
from __future__ import annotations

import argparse
import json as jsonlib
import sys

from contextlake.schedule import cmds, history, jobs


def _config(tmp_path, **kw):
    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    config.update(kw)
    return config


def _args(**kw):
    base = dict(action="status", job=None, json=False, platform=None,
                quiet=True, verbose=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _install_record(tmp_path, interval="auto", platform="systemd"):
    path = jobs.jobs_path(_config(tmp_path))
    jobs.write_job(path, jobs.new_job(jobs.DEFAULT_JOB, ["bootstrap"], interval, platform))
    return path


def test_no_schedule_says_so_and_says_how_to_make_one(tmp_path, capsys):
    assert cmds.cmd_status(_args(), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" in out.lower()
    assert "schedule install" in out


def test_filter_on_a_name_with_no_jobs_at_all_says_no_schedule(tmp_path, capsys):
    # No jobs exist at all, filtered or not: the reinstall hint still applies.
    assert cmds.cmd_status(_args(job="typo"), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" in out.lower()
    assert "schedule install" in out


def test_filter_on_a_name_that_does_not_match_names_the_real_jobs(tmp_path, capsys):
    # A job named "default" is installed and working; asking for "typo" must
    # not say "No schedule installed" and invite a reinstall over it.
    _install_record(tmp_path)
    assert cmds.cmd_status(_args(job="typo"), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" not in out.lower()
    assert "typo" in out
    assert "default" in out


def test_a_record_with_no_unit_is_reported_as_a_disagreement(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter(installed=False))
    cmds.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out.lower()
    assert "not installed" in out or "disagree" in out


def test_a_missing_interpreter_in_the_installed_unit_is_reported(tmp_path, capsys, monkeypatch):
    """The unit's ExecStart holds the interpreter chosen at install time. If that
    venv is deleted and contextlake is reinstalled elsewhere, the unit still
    points at the old path and fails on every fire.

    Asserts through the adapter's state(), not through exec_argv_for: an earlier
    version checked sys.executable, which is the interpreter running this very
    check and therefore can never be missing.
    """
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(exec_path="/gone/venv/bin/python"))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" in capsys.readouterr().out


def test_a_present_interpreter_in_the_installed_unit_is_not_reported(tmp_path, capsys, monkeypatch):
    """Break-test the row above: a path that DOES exist must not fire the
    missing-interpreter note. A row that always fires is as useless as one
    that never does."""
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(exec_path=sys.executable))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" not in capsys.readouterr().out


def test_an_unknown_interpreter_is_not_reported_as_missing(tmp_path, capsys, monkeypatch):
    """None means cannot tell. An adapter that cannot read its unit back must
    not make status accuse a healthy install."""
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter(exec_path=None))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" not in capsys.readouterr().out


def test_executable_missing_checks_the_file_not_just_the_string():
    assert cmds.executable_missing([sys.executable, "-m", "contextlake"]) is None
    assert cmds.executable_missing(["/definitely/not/here", "-m", "x"]) == "/definitely/not/here"


def test_drift_beyond_the_threshold_is_reported(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    path = history.history_path(_config(tmp_path))
    for i in range(5):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 3000.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    cmds.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="0.5"))
    out = capsys.readouterr().out.lower()
    assert "drift" in out or "ideal" in out


def test_drift_within_a_wider_threshold_is_not_reported(tmp_path, capsys, monkeypatch):
    # Same diverging fixture as test_drift_beyond_the_threshold_is_reported
    # (ratio 7.33), a wider threshold this time. Passing requires both that
    # schedule_adjust_threshold is read from config AND that the comparison
    # is the right way round; a hardcoded 0.5 or a missing read would fail
    # this one while still passing the other two drift tests.
    _install_record(tmp_path)
    path = history.history_path(_config(tmp_path))
    for i in range(5):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 3000.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    cmds.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="10"))
    assert "drift" not in capsys.readouterr().out.lower()


def test_no_drift_is_not_reported_as_drift(tmp_path, capsys, monkeypatch):
    # No `repos_changed`/`repos_total`: a fixture that carries them here (as an
    # earlier draft of this test did) feeds the activity floor a real change
    # rate, which pushes the recommendation to 8h regardless of duration and
    # makes this "no drift" fixture drift against a 1h unit. Leaving activity
    # unmeasured keeps this test on the one floor (duty) it means to exercise:
    # a 360s median at the default 10% duty cycle is exactly 3600s, matching
    # the fake adapter's installed interval, so there is nothing to drift.
    _install_record(tmp_path)
    path = history.history_path(_config(tmp_path))
    for i in range(5):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 360.0, "exit": 0})
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "drift" not in capsys.readouterr().out.lower()


def test_cron_state_makes_the_on_disk_line_live(tmp_path, capsys, monkeypatch):
    """Finding 1: `cron.state()` hardcoded ``interval_s`` to ``None``, so the
    "on disk" line never appeared for cron even though cron installs a
    rounded spec that can differ from the pinned setting. Uses the real
    `CronAdapter`, not `_FakeAdapter`, to prove the wiring end to end."""
    from contextlake.schedule.platform import cron

    _install_record(tmp_path, interval="70m", platform="cron")
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "0 * * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    monkeypatch.setattr(cron, "_read_crontab", lambda: text)
    cmds.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "70m" in out
    assert "on disk:   1h" in out


def test_cron_state_makes_drift_live_for_auto_jobs(tmp_path, capsys, monkeypatch):
    """Same defect as above, checked through the drift note instead of the
    "on disk" line: with `interval_s` always `None`, an auto job on cron
    could drift forever with no signal."""
    from contextlake.schedule.platform import cron

    _install_record(tmp_path, interval="auto", platform="cron")
    path = history.history_path(_config(tmp_path))
    for i in range(5):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 3000.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "0 * * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    monkeypatch.setattr(cron, "_read_crontab", lambda: text)
    cmds.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="0.5"))
    out = capsys.readouterr().out.lower()
    assert "drift" in out or "ideal" in out


def test_a_cold_start_interval_is_labelled_as_a_default(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "default" in capsys.readouterr().out.lower()


def test_an_adapter_that_cannot_catch_up_says_so(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path, platform="cron")
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(catches_up=False))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "asleep" in capsys.readouterr().out.lower()


def test_a_cannot_catch_up_note_from_the_adapter_is_not_duplicated(tmp_path, capsys, monkeypatch):
    # The real cron adapter's own `state()` already carries this note when
    # installed, and `catches_up_after_sleep` is False for cron too. Reading
    # both without deduplication prints the identical sentence twice.
    _install_record(tmp_path, platform="cron")
    monkeypatch.setattr(
        cmds, "_adapter_for",
        lambda *a, **k: _FakeAdapter(
            catches_up=False,
            notes=["cron does not replay a run missed while this machine "
                   "was asleep or off."]))
    cmds.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out.lower()
    assert out.count("does not replay a run missed") == 1


def test_a_broken_adapter_read_reports_the_record_without_a_false_catch_up_claim(
        tmp_path, capsys, monkeypatch):
    # systemd sets Persistent=true and does catch up after sleep. A `state()`
    # failure must not turn that into a claim that it does not: the adapter
    # was built, so its class-level `catches_up_after_sleep` is still known
    # even though the live read failed.
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _BrokenStateAdapter())
    cmds.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "default" in out
    assert "systemctl timed out" in out
    assert "does not replay a run missed" not in out.lower()


def test_a_broken_adapter_build_reports_the_record_without_a_false_catch_up_claim(
        tmp_path, capsys, monkeypatch):
    # Here the adapter never got built, so its catch-up behaviour is not
    # knowable at all. The record must still print; no guess either way.
    def _boom(*a, **k):
        raise RuntimeError("no scheduler found on this machine")

    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", _boom)
    cmds.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "default" in out
    assert "no scheduler found on this machine" in out
    assert "does not replay a run missed" not in out.lower()


def test_adapter_notes_are_surfaced(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(notes=["Linger is off, so ..."]))
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "linger" in capsys.readouterr().out.lower()


def test_status_json_carries_every_field(tmp_path, monkeypatch, capsys):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert cmds.cmd_status(_args(json=True), _config(tmp_path)) == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    job = payload["jobs"][0]
    for key in ("name", "interval_setting", "effective_interval", "unit_installed",
                "adapter", "recommendation", "last_run", "failures", "notes"):
        assert key in job, key
    # Presence alone lets a regression that writes None into every field
    # pass. Pin the fields whose value is knowable from the fixture.
    assert job["name"] == "default"
    assert job["interval_setting"] == "auto"
    assert job["adapter"] == "fake"
    assert job["unit_installed"] is True
    assert job["failures"] == 0


def test_status_writes_nothing(tmp_path, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    cmds.cmd_status(_args(), _config(tmp_path))
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert before == after


def test_require_idle_on_but_undetectable_says_the_gate_is_inert(
        tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(cmds.gates, "user_is_idle", lambda: None)
    cmds.cmd_status(_args(), _config(tmp_path, schedule_require_idle="true"))
    out = capsys.readouterr().out.lower()
    assert "inert" in out


def test_require_idle_off_says_nothing_about_it(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(cmds.gates, "user_is_idle", lambda: None)
    cmds.cmd_status(_args(), _config(tmp_path))
    assert "inert" not in capsys.readouterr().out.lower()


def test_require_idle_on_and_detectable_says_nothing_about_being_inert(
        tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(cmds.gates, "user_is_idle", lambda: True)
    cmds.cmd_status(_args(), _config(tmp_path, schedule_require_idle="true"))
    assert "inert" not in capsys.readouterr().out.lower()


class _BrokenStateAdapter:
    id = "systemd"
    catches_up_after_sleep = True

    def state(self, job):
        raise RuntimeError("systemctl timed out")


class _FakeAdapter:
    # A present, real path by default: tests that do not care about the
    # interpreter row must not incidentally trip the missing-interpreter
    # note just because the fake needs some string there.
    def __init__(self, installed=True, interval_s=3600.0, catches_up=True, notes=(),
                exec_path=sys.executable):
        self.id = "fake"
        self.catches_up_after_sleep = catches_up
        self._state = {"installed": installed, "interval_s": interval_s,
                       "next_run": None, "exec_path": exec_path, "notes": list(notes)}

    def state(self, job):
        return dict(self._state)
