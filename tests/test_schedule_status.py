"""`schedule status`: every way a schedule can be silently dead."""
from __future__ import annotations

import argparse
import json as jsonlib
import sys

import pytest

from contextlake import config as cfgmod
from contextlake.schedule import adapters, history, jobs, report
from contextlake.schedule.platform import cloud, k8s


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
    assert report.cmd_status(_args(), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" in out.lower()
    assert "schedule install" in out


def test_filter_on_a_name_with_no_jobs_at_all_says_no_schedule(tmp_path, capsys):
    # No jobs exist at all, filtered or not: the reinstall hint still applies.
    assert report.cmd_status(_args(job="typo"), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" in out.lower()
    assert "schedule install" in out


def test_filter_on_a_name_that_does_not_match_names_the_real_jobs(tmp_path, capsys):
    # A job named "default" is installed and working; asking for "typo" must
    # not say "No schedule installed" and invite a reinstall over it.
    _install_record(tmp_path)
    assert report.cmd_status(_args(job="typo"), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no schedule" not in out.lower()
    assert "typo" in out
    assert "default" in out


def test_a_record_with_no_unit_is_reported_as_a_disagreement(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(installed=False))
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out.lower()
    assert "not installed" in out or "disagree" in out


def test_a_unit_that_could_not_be_asked_is_not_reported_as_missing(
        tmp_path, capsys, monkeypatch):
    """None is "cannot tell". The adapter's own note says why, and `status` must not add
    "NOT installed. Re-run install" on top: install needs the same access as the read that
    failed, so that advice cannot help."""
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(
        installed=None, notes=["could not read the aws state: ExpiredTokenException"]))
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "ExpiredTokenException" in out
    assert "NOT installed" not in out
    assert "schedule install" not in out


def test_status_json_says_null_for_a_unit_that_could_not_be_asked(
        tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(installed=None))
    report.cmd_status(_args(json=True), _config(tmp_path))
    job = jsonlib.loads(capsys.readouterr().out)["jobs"][0]
    assert job["unit_installed"] is None


def test_an_expired_aws_login_is_not_reported_as_a_missing_schedule(
        tmp_path, capsys, monkeypatch):
    """The whole path with the real AWS adapter: a failed `get-schedule` that is not a
    ResourceNotFoundException shows its error, and a NotFound still reports the schedule
    missing."""
    import subprocess

    from contextlake.schedule.platform import cloud

    _install_record(tmp_path, platform="aws")
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: subprocess.CompletedProcess(
        a, 254, stdout="", stderr="An error occurred (ExpiredTokenException) when calling "
                                  "the GetSchedule operation: The security token is expired"))
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "could not read the aws state" in out
    assert "ExpiredTokenException" in out
    assert "NOT installed" not in out

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: subprocess.CompletedProcess(
        a, 254, stdout="", stderr="An error occurred (ResourceNotFoundException) when "
                                  "calling the GetSchedule operation: does not exist."))
    report.cmd_status(_args(), _config(tmp_path))
    assert "NOT installed" in capsys.readouterr().out


def test_a_missing_cloud_cli_shows_its_own_error_and_no_install_advice(
        tmp_path, capsys, monkeypatch):
    """The real AWS adapter with no `aws` on PATH: the OSError text is the note, the unit is
    "cannot tell" (null in JSON), and `status` does not send the user to `schedule install`."""
    from contextlake.schedule.platform import cloud

    def _no_cli(binary, *argv, input=None):  # noqa: A002 - matches cloud._cli
        raise OSError(f"{binary} is not on PATH")

    _install_record(tmp_path, platform="aws")
    monkeypatch.setattr(cloud, "_cli", _no_cli)
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "aws is not on PATH" in out
    assert "NOT installed" not in out
    assert "schedule install" not in out

    report.cmd_status(_args(json=True), _config(tmp_path))
    job = jsonlib.loads(capsys.readouterr().out)["jobs"][0]
    assert job["unit_installed"] is None


def test_a_missing_interpreter_in_the_installed_unit_is_reported(tmp_path, capsys, monkeypatch):
    """The unit's ExecStart holds the interpreter chosen at install time. If that
    venv is deleted and contextlake is reinstalled elsewhere, the unit still
    points at the old path and fails on every fire.

    Asserts through the adapter's state(), not through exec_argv_for: an earlier
    version checked sys.executable, which is the interpreter running this very
    check and therefore can never be missing.
    """
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(exec_path="/gone/venv/bin/python"))
    report.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" in capsys.readouterr().out


def test_a_present_interpreter_in_the_installed_unit_is_not_reported(tmp_path, capsys, monkeypatch):
    """Break-test the row above: a path that DOES exist must not fire the
    missing-interpreter note. A row that always fires is as useless as one
    that never does."""
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(exec_path=sys.executable))
    report.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" not in capsys.readouterr().out


def test_an_unknown_interpreter_is_not_reported_as_missing(tmp_path, capsys, monkeypatch):
    """None means cannot tell. An adapter that cannot read its unit back must
    not make status accuse a healthy install."""
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(exec_path=None))
    report.cmd_status(_args(), _config(tmp_path))
    assert "moved or been deleted" not in capsys.readouterr().out


def test_executable_missing_checks_the_file_not_just_the_string():
    assert adapters.executable_missing([sys.executable, "-m", "contextlake"]) is None
    assert (adapters.executable_missing(["/definitely/not/here", "-m", "x"])
            == "/definitely/not/here")


def test_drift_beyond_the_threshold_is_reported(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    path = history.history_path(_config(tmp_path))
    for i in range(5):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 3000.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    report.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="0.5"))
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
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    report.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="10"))
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
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(interval_s=3600.0))
    report.cmd_status(_args(), _config(tmp_path))
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
    report.cmd_status(_args(), _config(tmp_path))
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
    report.cmd_status(_args(), _config(tmp_path, schedule_adjust_threshold="0.5"))
    out = capsys.readouterr().out.lower()
    assert "drift" in out or "ideal" in out


def test_a_cold_start_interval_is_labelled_as_a_default(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    report.cmd_status(_args(), _config(tmp_path))
    assert "default" in capsys.readouterr().out.lower()


def test_an_adapter_that_cannot_catch_up_says_so(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path, platform="cron")
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(catches_up=False))
    report.cmd_status(_args(), _config(tmp_path))
    assert "asleep" in capsys.readouterr().out.lower()


def test_a_cannot_catch_up_note_from_the_adapter_is_not_duplicated(tmp_path, capsys, monkeypatch):
    # The real cron adapter's own `state()` already carries this note when
    # installed, and `catches_up_after_sleep` is False for cron too. Reading
    # both without deduplication prints the identical sentence twice.
    _install_record(tmp_path, platform="cron")
    monkeypatch.setattr(
        adapters, "_adapter_for",
        lambda *a, **k: _FakeAdapter(
            catches_up=False,
            notes=["cron does not replay a run missed while this machine "
                   "was asleep or off."]))
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out.lower()
    assert out.count("does not replay a run missed") == 1


def test_a_broken_adapter_read_reports_the_record_without_a_false_catch_up_claim(
        tmp_path, capsys, monkeypatch):
    # systemd sets Persistent=true and does catch up after sleep. A `state()`
    # failure must not turn that into a claim that it does not: the adapter
    # was built, so its class-level `catches_up_after_sleep` is still known
    # even though the live read failed.
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _BrokenStateAdapter())
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "default" in out
    assert "systemctl timed out" in out
    assert "does not replay a run missed" not in out.lower()
    # The read failed, so nothing says the unit is missing.
    assert "NOT installed" not in out


def test_a_broken_adapter_build_reports_the_record_without_a_false_catch_up_claim(
        tmp_path, capsys, monkeypatch):
    # Here the adapter never got built, so its catch-up behaviour is not
    # knowable at all. The record must still print; no guess either way.
    def _boom(*a, **k):
        raise RuntimeError("no scheduler found on this machine")

    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", _boom)
    report.cmd_status(_args(), _config(tmp_path))
    out = capsys.readouterr().out
    assert "default" in out
    assert "could not build the systemd adapter: no scheduler found on this machine" in out
    assert "does not replay a run missed" not in out.lower()
    assert "NOT installed" not in out


def test_adapter_notes_are_surfaced(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for",
                        lambda *a, **k: _FakeAdapter(notes=["Linger is off, so ..."]))
    report.cmd_status(_args(), _config(tmp_path))
    assert "linger" in capsys.readouterr().out.lower()


def test_status_json_carries_every_field(tmp_path, monkeypatch, capsys):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert report.cmd_status(_args(json=True), _config(tmp_path)) == 0
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
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    report.cmd_status(_args(), _config(tmp_path))
    after = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    assert before == after


def test_require_idle_on_but_undetectable_says_the_gate_is_inert(
        tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(report.gates, "user_is_idle", lambda: None)
    report.cmd_status(_args(), _config(tmp_path, schedule_require_idle="true"))
    out = capsys.readouterr().out.lower()
    assert "inert" in out


def test_require_idle_off_says_nothing_about_it(tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(report.gates, "user_is_idle", lambda: None)
    report.cmd_status(_args(), _config(tmp_path))
    assert "inert" not in capsys.readouterr().out.lower()


def test_require_idle_on_and_detectable_says_nothing_about_being_inert(
        tmp_path, capsys, monkeypatch):
    _install_record(tmp_path)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter())
    monkeypatch.setattr(report.gates, "user_is_idle", lambda: True)
    report.cmd_status(_args(), _config(tmp_path, schedule_require_idle="true"))
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
                exec_path=sys.executable, launch=False, reads_host_config=None):
        self.id = "fake"
        self.catches_up_after_sleep = catches_up
        self._state = {"installed": installed, "interval_s": interval_s,
                       "next_run": None, "exec_path": exec_path, "notes": list(notes)}
        # Left out unless given: a state with no `launch` key is the path an adapter that
        # predates it, or a failed read, takes.
        if launch is not False:
            self._state["launch"] = launch
        # Left out unless given: an adapter that does not say reads the config on this machine.
        if reads_host_config is not None:
            self.reads_host_config = reads_host_config

    def state(self, job):
        return dict(self._state)


# ---- the config a job's unit reads ------------------------------------------

@pytest.fixture
def _here(tmp_path, monkeypatch):
    """A global config of its own, no opt-out, and a plain directory to run from. A
    developer's own `~/.contextlake.ini` or an ancestor config must not decide these tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv(cfgmod.NO_LOCAL_CONFIG_ENV, raising=False)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", str(home / ".contextlake.ini"))
    monkeypatch.chdir(home)
    return home


def _ini(directory, name=".contextlake.ini"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("[contextlake]\ngitlab_group = g\n", encoding="utf-8")
    return path


def _record(tmp_path, name=jobs.DEFAULT_JOB, interval="auto", platform="systemd", **origin):
    path = jobs.jobs_path(_config(tmp_path))
    jobs.write_job(path, jobs.new_job(name, ["bootstrap"], interval, platform, origin=origin))


def _status(tmp_path, monkeypatch, capsys, launch=False, reads_host_config=None, **args):
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(
        launch=launch, reads_host_config=reads_host_config))
    assert report.cmd_status(_args(**args), _config(tmp_path)) == 0
    return capsys.readouterr().out


UNIT_WITHOUT_CONFIG = {"argv": [sys.executable, "-m", "contextlake", "schedule", "run",
                                "--job", "default"], "cwd": None}


def test_status_shows_the_config_and_directory_the_job_was_installed_with(
        tmp_path, monkeypatch, capsys, _here):
    ini, kb = _ini(tmp_path / "a", "custom.ini"), _ini(tmp_path / "a", "kb.toml")
    _record(tmp_path, config=str(ini), config_source=jobs.EXPLICIT, kb_config=str(kb),
            kb_config_source=jobs.EXPLICIT, cwd=str(tmp_path / "ws"))
    out = _status(tmp_path, monkeypatch, capsys, config=str(ini))
    assert f"    config: {ini} (in the unit's command as --config)\n" in out
    assert f"    knowledge config: {kb} (in the unit's command as --kb-config)\n" in out
    assert f"    working directory: {tmp_path / 'ws'} " in out


def test_status_prints_no_config_lines_for_a_job_that_has_none(
        tmp_path, monkeypatch, capsys, _here):
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys)
    assert "config:" not in out and "working directory" not in out


def test_status_json_carries_the_recorded_config_and_directory(
        tmp_path, monkeypatch, capsys, _here):
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path, config=str(ini), config_source=jobs.EXPLICIT)
    _record(tmp_path, "old")
    out = _status(tmp_path, monkeypatch, capsys, json=True, config=str(ini))
    entries = {job["name"]: job for job in jsonlib.loads(out)["jobs"]}
    assert {key: entries["default"][key] for key in jobs.ORIGIN_KEYS} == {
        "config": str(ini), "config_source": jobs.EXPLICIT, "kb_config": None,
        "kb_config_source": None, "cwd": None}
    assert all(entries["old"][key] is None for key in jobs.ORIGIN_KEYS)


def test_loaded_configs_lists_the_local_and_named_files_but_not_the_global_one(
        tmp_path, monkeypatch, _here):
    assert adapters.loaded_configs(argparse.Namespace()) == []
    glob = _ini(_here)
    local = _ini(tmp_path / "ws")
    named = _ini(tmp_path / "other", "custom.ini")
    monkeypatch.chdir(tmp_path / "ws")
    assert adapters.loaded_configs(argparse.Namespace(config=None)) == [str(local)]
    assert adapters.loaded_configs(argparse.Namespace(config=str(named))) == [
        str(local), str(named)]
    # Named twice, or named as the global file: once, or not at all.
    assert adapters.loaded_configs(argparse.Namespace(config=str(local))) == [str(local)]
    assert adapters.loaded_configs(argparse.Namespace(config=str(glob))) == [str(local)]
    monkeypatch.setenv(cfgmod.NO_LOCAL_CONFIG_ENV, "1")
    assert adapters.loaded_configs(argparse.Namespace(config=None)) == []


# A unit that carries neither --config nor a working directory loads the global config.

def test_status_warns_when_the_unit_has_no_config_but_this_command_loaded_one(
        tmp_path, monkeypatch, capsys, _here):
    """The state of every unit installed before the config was kept, found through
    `--config`, the only way to reach its record."""
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(ini))
    assert "starts with no --config and no working directory" in out
    assert f"This command loaded {ini}." in out
    assert f"install it again from this directory: contextlake --config {ini} schedule install" \
        in out


@pytest.mark.parametrize("found_by_walking_up", [False, True],
                         ids=["config-named-with-dash-dash-config", "config-found-from-here"])
def test_the_no_config_warning_says_what_the_unit_reads_and_not_how_it_came_to_read_it(
        tmp_path, monkeypatch, capsys, _here, found_by_walking_up):
    """Three jobs give the same record and the same unit: one installed by an earlier version,
    one installed from a directory with no config, and one installed from the global config
    alone, checked from a directory that has a local config. The warning is true for all
    three only if it names the unit and its config and not a cause."""
    if found_by_walking_up:
        _ini(tmp_path / "ws")
        monkeypatch.chdir(tmp_path / "ws")
        named = None
    else:
        named = str(_ini(tmp_path / "a", "custom.ini"))
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=named)
    assert ("The unit for this job starts with no --config and no working directory, so it "
            "reads only the global config. This command loaded ") in out
    assert "earlier version" not in out
    assert "install it again from this directory: contextlake" in out


def test_status_quotes_a_config_path_with_a_space_in_the_command_it_suggests(
        tmp_path, monkeypatch, capsys, _here):
    ini = _ini(tmp_path / "my configs", "custom.ini")
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(ini))
    assert f"contextlake --config '{ini}' schedule install" in out


def test_status_names_the_job_in_the_command_it_suggests(
        tmp_path, monkeypatch, capsys, _here):
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path, "wiki")
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(ini))
    assert f"contextlake --config {ini} schedule --job wiki install" in out


def test_status_suggests_installing_from_here_when_the_config_was_found_by_walking_up(
        tmp_path, monkeypatch, capsys, _here):
    _ini(tmp_path / "ws")
    monkeypatch.chdir(tmp_path / "ws")
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG)
    assert "install it again from this directory: contextlake schedule install" in out
    assert "--config" not in out.split("install it again")[1]


@pytest.mark.parametrize("launch, why", [
    (None, "the unit could not be read back, which is not a finding"),
    (False, "the adapter returned no launch key"),
    ({"argv": UNIT_WITHOUT_CONFIG["argv"][:3] + ["--config", "/x.ini"] + ["schedule", "run"],
      "cwd": None}, "the unit carries --config"),
    ({**UNIT_WITHOUT_CONFIG, "cwd": "/ws"}, "the unit starts in a directory"),
])
def test_status_does_not_warn_when_the_unit_is_unreadable_or_has_a_config_or_directory(
        tmp_path, monkeypatch, capsys, _here, launch, why):
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=launch, config=str(ini))
    assert "starts with no --config" not in out, why


def test_status_does_not_warn_when_this_command_loaded_only_the_global_config(
        tmp_path, monkeypatch, capsys, _here):
    glob = _ini(_here)
    _record(tmp_path)
    for named in (None, str(glob)):
        out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=named)
        assert "starts with no --config" not in out


def test_status_warns_about_the_cron_line_earlier_versions_wrote(
        tmp_path, monkeypatch, capsys, _here):
    """The real cron read-back of a line with no `cd` and no `--config`, through to the note.
    The line is the one a pre-fix `schedule install` wrote."""
    from contextlake.schedule.platform import cron

    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path)
    line = "0 */6 * * * /opt/venv/bin/python -m contextlake schedule run --job default " \
           ">/dev/null 2>&1\n"
    crontab = cron.BEGIN.format(name="default") + "\n" + line + cron.END.format(name="default")
    monkeypatch.setattr(cron, "_read_crontab", lambda: crontab + "\n")
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: cron.CronAdapter())
    assert report.cmd_status(_args(config=str(ini)), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "starts with no --config and no working directory" in out
    assert "earlier version" not in out


# A job whose record names a config.

def test_status_warns_when_the_unit_reads_a_config_this_command_did_not_load(
        tmp_path, monkeypatch, capsys, _here):
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path, config=str(ini), config_source=jobs.EXPLICIT)
    out = _status(tmp_path, monkeypatch, capsys)
    assert f"The unit for this job reads {ini} (named with --config). This command loaded " \
           f"no config besides the global one." in out
    assert f"run: contextlake --config {ini} schedule status" in out
    assert "The unit for this job reads" not in _status(
        tmp_path, monkeypatch, capsys, config=str(ini))


def test_status_warns_about_a_config_found_by_walking_up_and_names_where_to_run(
        tmp_path, monkeypatch, capsys, _here):
    ws = tmp_path / "my ws"
    ini = _ini(ws)
    _record(tmp_path, config=str(ini), config_source=jobs.DISCOVERED, cwd=str(ws))
    out = _status(tmp_path, monkeypatch, capsys)
    assert f"reads {ini} (found from {ws})" in out
    assert f"run: cd '{ws}' && contextlake schedule status" in out
    assert "--config" not in out.split("run: ")[1]
    monkeypatch.chdir(ws)
    assert "The unit for this job reads" not in _status(tmp_path, monkeypatch, capsys)


def test_one_cause_gives_one_config_warning(tmp_path, monkeypatch, capsys, _here):
    """A record that names a config next to a unit that lacks it: the note that reads the
    unit is the one printed."""
    other = _ini(tmp_path / "b", "other.ini")
    _record(tmp_path, config=str(tmp_path / "a" / "custom.ini"), config_source=jobs.EXPLICIT)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(other))
    assert "starts with no --config" in out
    assert "The unit for this job reads" not in out


def test_a_config_warning_survives_an_adapter_that_could_not_be_built(
        tmp_path, monkeypatch, capsys, _here):
    """The fallback state has no `launch` key."""
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path, config=str(ini), config_source=jobs.EXPLICIT)

    def no_adapter(*a, **k):
        raise RuntimeError("no adapter here")

    monkeypatch.setattr(adapters, "_adapter_for", no_adapter)
    assert report.cmd_status(_args(), _config(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "could not build the systemd adapter" in out
    assert "The unit for this job reads" in out


# A job whose unit starts in a directory that no longer exists: status says so. It says the job
# cannot start only for systemd, cron and Task Scheduler, the platforms where that was checked.

UNIT_ARGV = [sys.executable, "-m", "contextlake", "schedule", "run", "--job", "default"]


def _cron_unit(tmp_path, monkeypatch, job):
    from contextlake.schedule.platform import cron

    line = cron.CronAdapter().render(job, 3600.0, UNIT_ARGV)["crontab"]
    text = cron.BEGIN.format(name="default") + "\n" + line + cron.END.format(name="default")
    monkeypatch.setattr(cron, "_read_crontab", lambda: text + "\n")
    return cron.CronAdapter()


def _systemd_unit(tmp_path, monkeypatch, job):
    import subprocess

    from contextlake.schedule.platform import systemd

    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path))
    monkeypatch.setattr(systemd, "_systemctl",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(systemd, "_linger_status", lambda: "Linger=yes\n")
    adapter = systemd.SystemdAdapter()
    adapter.install(job, 3600.0, UNIT_ARGV)
    return adapter


def _launchd_unit(tmp_path, monkeypatch, job):
    from contextlake.schedule.platform import launchd

    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    adapter = launchd.LaunchdAdapter()
    name = launchd.plist_name("default")
    (tmp_path / name).write_text(adapter.render(job, 900, UNIT_ARGV)[name], encoding="utf-8")
    return adapter


def _windows_unit(tmp_path, monkeypatch, job):
    import subprocess

    from contextlake.schedule.platform import windows

    command = windows._task_command(job, UNIT_ARGV)
    monkeypatch.setattr(windows, "_schtasks", lambda *a: subprocess.CompletedProcess(
        a, 0, stdout=f"TaskName:      \\contextlake\\default\nTask To Run:   {command}\n",
        stderr=""))
    return windows.WindowsAdapter()


UNITS = {"cron": _cron_unit, "systemd": _systemd_unit, "launchd": _launchd_unit,
         "windows": _windows_unit}


def _installed_unit(platform, tmp_path, monkeypatch, directory, **origin):
    """The real adapter of ``platform``, reading back a unit written for a job that starts in
    ``directory``, and the job record to match. Nothing is run: each read-back parses text."""
    job = jobs.new_job(jobs.DEFAULT_JOB, ["bootstrap"], "auto", platform,
                       origin={"cwd": str(directory), **origin})
    jobs.write_job(jobs.jobs_path(_config(tmp_path)), job)
    adapter = UNITS[platform](tmp_path, monkeypatch, job)
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: adapter)
    return adapter


def _run_status(tmp_path, capsys, **args):
    assert report.cmd_status(_args(**args), _config(tmp_path)) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("platform", sorted(UNITS))
def test_status_warns_when_the_directory_the_unit_starts_in_is_gone(
        tmp_path, monkeypatch, capsys, _here, platform):
    """Each of the four platforms that write a directory, read back off the unit it wrote.
    systemd, launchd, cron and Task Scheduler all change to it before the command runs."""
    gone = tmp_path / "ws gone"
    _installed_unit(platform, tmp_path, monkeypatch, gone)
    out = _run_status(tmp_path, capsys)
    head = f"The working directory of this job's unit, {gone}, is not an existing directory."
    if platform == "launchd":
        # Nothing here ran launchd, so the note states no effect.
        assert f"{head} What launchd does with a missing directory was not checked." in out
        assert "cannot start" not in out and "cannot run" not in out
    else:
        assert f"{head} The job cannot start there." in out
        assert "was not checked" not in out
    assert "Install the job again from a directory that exists: contextlake schedule install" \
        in out

    gone.mkdir()
    assert "not an existing directory" not in _run_status(tmp_path, capsys)


def test_the_missing_directory_warning_is_in_the_json_notes(
        tmp_path, monkeypatch, capsys, _here):
    gone = tmp_path / "ws gone"
    _installed_unit("cron", tmp_path, monkeypatch, gone)
    notes = jsonlib.loads(_run_status(tmp_path, capsys, json=True))["jobs"][0]["notes"]
    assert sum("is not an existing directory" in note for note in notes) == 1


def test_the_install_hint_for_a_missing_directory_keeps_the_job_the_interval_and_the_config(
        tmp_path, monkeypatch, capsys, _here):
    """Installing again from the hint must not unpin the job or drop a config it was given."""
    ini = _ini(tmp_path / "a", "custom.ini")
    gone = tmp_path / "ws gone"
    jobs.write_job(jobs.jobs_path(_config(tmp_path)), jobs.new_job(
        "wiki", ["bootstrap"], "2h", "systemd",
        origin={"cwd": str(gone), "config": str(ini), "config_source": jobs.EXPLICIT}))
    monkeypatch.setattr(adapters, "_adapter_for", lambda *a, **k: _FakeAdapter(
        launch={**UNIT_WITHOUT_CONFIG, "cwd": str(gone),
                "argv": UNIT_ARGV[:3] + ["--config", str(ini)] + UNIT_ARGV[3:]}))
    out = _run_status(tmp_path, capsys, config=str(ini))
    parsed = _hint(out, after="Install the job again from a directory that exists: ")
    assert (parsed.command, parsed.action, parsed.job, parsed.interval, parsed.config) == (
        "schedule", "install", "wiki", "2h", str(ini))


def test_status_does_not_warn_about_a_directory_the_unit_does_not_use(
        tmp_path, monkeypatch, capsys, _here):
    """The record holds a directory that is gone, but the unit that was read starts in none, or
    in one that exists: the job can start."""
    here = tmp_path / "here"
    here.mkdir()
    _record(tmp_path, cwd=str(tmp_path / "gone"))
    for launch in (UNIT_WITHOUT_CONFIG, {**UNIT_WITHOUT_CONFIG, "cwd": str(here)}):
        assert "not an existing directory" not in _status(
            tmp_path, monkeypatch, capsys, launch=launch)


def test_status_warns_about_the_unit_directory_even_when_the_record_names_another(
        tmp_path, monkeypatch, capsys, _here):
    here = tmp_path / "here"
    here.mkdir()
    _record(tmp_path, cwd=str(here))
    out = _status(tmp_path, monkeypatch, capsys,
                  launch={**UNIT_WITHOUT_CONFIG, "cwd": str(tmp_path / "gone")})
    assert f"unit, {tmp_path / 'gone'}, is not an existing directory" in out


@pytest.mark.parametrize("launch", [None, False], ids=["unit-unreadable", "no-launch-key"])
def test_status_checks_the_recorded_directory_when_the_unit_could_not_be_read(
        tmp_path, monkeypatch, capsys, _here, launch):
    gone = tmp_path / "ws gone"
    _record(tmp_path, cwd=str(gone))
    out = _status(tmp_path, monkeypatch, capsys, launch=launch)
    assert (f"The working directory recorded for this job, {gone}, is not an existing "
            f"directory. A unit that starts in it cannot run the job.") in out
    gone.mkdir()
    assert "not an existing directory" not in _status(
        tmp_path, monkeypatch, capsys, launch=launch)


@pytest.mark.parametrize("platform", ["systemd", "cron", "windows", "launchd", "", "future"])
def test_the_effect_of_a_recorded_missing_directory_is_stated_only_where_it_was_checked(
        tmp_path, monkeypatch, capsys, _here, platform):
    """The unit could not be read, so the record decides. systemd, cron and Task Scheduler
    were checked and the note says the unit cannot run the job. Any other platform, launchd
    included, gets a sentence that says what it does was not checked."""
    gone = tmp_path / "ws gone"
    _record(tmp_path, platform=platform, cwd=str(gone))
    out = _status(tmp_path, monkeypatch, capsys, launch=None)
    head = f"The working directory recorded for this job, {gone}, is not an existing directory."
    if platform in ("systemd", "cron", "windows"):
        assert f"{head} A unit that starts in it cannot run the job." in out
        assert "was not checked" not in out
    else:
        name = platform or "this platform"
        assert f"{head} What {name} does with a missing directory was not checked." in out
        assert "cannot run the job" not in out and "cannot start" not in out


def test_status_does_not_warn_about_a_directory_for_a_job_that_has_none(
        tmp_path, monkeypatch, capsys, _here):
    _record(tmp_path)
    for launch in (None, False, UNIT_WITHOUT_CONFIG):
        assert "not an existing directory" not in _status(
            tmp_path, monkeypatch, capsys, launch=launch)


def test_a_file_in_place_of_the_directory_counts_as_gone(tmp_path, monkeypatch, capsys, _here):
    not_a_directory = tmp_path / "ws"
    not_a_directory.write_text("", encoding="utf-8")
    _record(tmp_path, cwd=str(not_a_directory))
    assert "is not an existing directory" in _status(tmp_path, monkeypatch, capsys, launch=None)


def test_a_missing_directory_is_the_one_warning_and_names_no_command_that_enters_it(
        tmp_path, monkeypatch, capsys, _here):
    """A config found by walking up from a directory that is gone, and deleted with it: the
    mismatch note would send the reader to `cd` into it, and the command fails. The directory
    and the config are one cause, so one warning. The file is gone, so the warning does not
    ask for a directory that finds it, and its install command does not `cd` either."""
    ws = tmp_path / "my ws"
    ini = _ini(ws)
    _record(tmp_path, config=str(ini), config_source=jobs.DISCOVERED, cwd=str(ws))
    ini.unlink()
    ws.rmdir()
    other = _ini(tmp_path / "b", "other.ini")
    out = _status(tmp_path, monkeypatch, capsys,
                  launch={**UNIT_WITHOUT_CONFIG, "cwd": str(ws)}, config=str(other))
    assert "The unit for this job reads" not in out
    assert "starts with no --config" not in out
    # The recorded files print under the job and still name the file. The warning is one line.
    warning = next(line for line in out.splitlines() if "is not an existing directory" in line)
    assert "The config it reads" not in warning and "finds that file" not in warning
    assert str(ini) not in warning
    assert "Install the job again from a directory that exists: contextlake schedule install" \
        in warning
    assert "cd " not in out


def test_a_found_config_that_still_exists_is_named_when_its_directory_is_gone(
        tmp_path, monkeypatch, capsys, _here):
    """The config was found from a directory below the one that holds it. Only the lower
    directory is gone, so the file still exists and a directory that finds it is the fix."""
    top = tmp_path / "top"
    ini = _ini(top)
    sub = top / "sub dir"
    _record(tmp_path, config=str(ini), config_source=jobs.DISCOVERED, cwd=str(sub))
    other = _ini(tmp_path / "b", "other.ini")
    out = _status(tmp_path, monkeypatch, capsys,
                  launch={**UNIT_WITHOUT_CONFIG, "cwd": str(sub)}, config=str(other))
    assert "The unit for this job reads" not in out
    assert f"The config it reads, {ini}, was found from that directory." in out
    assert "Install the job again from a directory that finds that file: contextlake " \
           "schedule install" in out
    assert "that exists" not in out
    assert "cd " not in out


# A job that runs in a container reads the config in its image, not a file on this machine.

@pytest.mark.parametrize("reads_host_config", [
    pytest.param(True, id="control-a-host-unit"),
    pytest.param(k8s.K8sAdapter.reads_host_config, id=k8s.K8sAdapter.id),
    pytest.param(cloud.AwsAdapter.reads_host_config, id=cloud.AwsAdapter.id),
    pytest.param(cloud.AzureAdapter.reads_host_config, id=cloud.AzureAdapter.id),
])
def test_status_gives_a_container_job_no_host_config_lines_or_warnings(
        tmp_path, monkeypatch, capsys, _here, reads_host_config):
    """The record of a container job can hold a host path, because `install` captures it from
    the run. Printing it, or telling the reader to rerun `status` with it, is false for a unit
    that never reads that file. The control case shows the same records do print for a unit
    that starts a process here."""
    ini = _ini(tmp_path / "a", "custom.ini")
    other = _ini(tmp_path / "b", "other.ini")
    (tmp_path / "ws").mkdir()
    _record(tmp_path, config=str(ini), config_source=jobs.EXPLICIT, cwd=str(tmp_path / "ws"))
    # One: the record names a file this command did not load. Two: the unit has no --config and
    # this command did load a file. Three: the unit starts in a directory that is gone.
    in_gone_directory = {**UNIT_WITHOUT_CONFIG, "cwd": str(tmp_path / "gone")}
    for launch, loaded, warning in (
            (None, None, "The unit for this job reads"),
            (UNIT_WITHOUT_CONFIG, str(other), "starts with no --config"),
            (in_gone_directory, None, "is not an existing directory")):
        out = _status(tmp_path, monkeypatch, capsys, launch=launch,
                      reads_host_config=reads_host_config, config=loaded)
        assert (f"    config: {ini} " in out) is reads_host_config
        assert ("    working directory: " in out) is reads_host_config
        assert (warning in out) is reads_host_config
        if not reads_host_config:
            assert "contextlake --config" not in out and "install it again" not in out


# `install` sets the interval to auto unless it is given one.

def _hint(out, after="install it again from this directory: "):
    import shlex

    from contextlake.cli import build_parser

    words = shlex.split(out.split(after)[1].splitlines()[0])
    assert words[0] == "contextlake"
    return build_parser().parse_args(words[1:])


@pytest.mark.parametrize("name", [jobs.DEFAULT_JOB, "wiki"])
def test_the_install_hint_for_a_pinned_job_keeps_its_interval(
        tmp_path, monkeypatch, capsys, _here, name):
    """A hint that left the interval out would pin nothing: the install it names resets a job
    pinned at 2h to auto."""
    ini = _ini(tmp_path / "my configs", "custom.ini")
    _record(tmp_path, name, interval="2h")
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(ini))
    assert "--interval 2h install" in out
    parsed = _hint(out)
    assert (parsed.command, parsed.action, parsed.interval) == ("schedule", "install", "2h")
    assert parsed.config == str(ini)
    if name != jobs.DEFAULT_JOB:
        assert parsed.job == name


def test_the_install_hint_for_an_auto_job_has_no_interval_flag(
        tmp_path, monkeypatch, capsys, _here):
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path)
    out = _status(tmp_path, monkeypatch, capsys, launch=UNIT_WITHOUT_CONFIG, config=str(ini))
    assert "install it again" in out
    assert "--interval" not in out


def test_the_status_hint_for_a_pinned_job_has_no_interval_flag(
        tmp_path, monkeypatch, capsys, _here):
    """`status` does not read the interval, so only the `install` hint carries it."""
    ini = _ini(tmp_path / "a", "custom.ini")
    _record(tmp_path, interval="2h", config=str(ini), config_source=jobs.EXPLICIT)
    out = _status(tmp_path, monkeypatch, capsys)
    assert f"run: contextlake --config {ini} schedule status" in out
    assert "--interval" not in out
