"""`schedule reset`: back to auto, and optionally throw away the measurements."""
from __future__ import annotations

import argparse

from contextlake.schedule import cmds, history, jobs


def _config(tmp_path):
    return {"cache_dir": str(tmp_path), "cache_file": "p.txt"}


def _args(**kw):
    base = dict(action="reset", job=None, history=False, yes=True, json=False,
                platform=None, quiet=True, verbose=False, interval=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _pinned_job(tmp_path, failures=3):
    path = jobs.jobs_path(_config(tmp_path))
    job = jobs.new_job(jobs.DEFAULT_JOB, ["bootstrap"], "2h", "systemd")
    jobs.write_job(path, job._replace(failures=failures, last_exit=1))
    return path


def _seed_history(tmp_path, n=4):
    path = history.history_path(_config(tmp_path))
    for i in range(n):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 420.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    return path


def test_reset_clears_a_fixed_pin(tmp_path, monkeypatch):
    path = _pinned_job(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert cmds.cmd_reset(_args(), _config(tmp_path)) == 0
    assert jobs.read_jobs(path)[jobs.DEFAULT_JOB].interval == "auto"


def test_reset_clears_the_failure_backoff(tmp_path, monkeypatch):
    path = _pinned_job(tmp_path, failures=5)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    cmds.cmd_reset(_args(), _config(tmp_path))
    assert jobs.read_jobs(path)[jobs.DEFAULT_JOB].failures == 0


def test_reset_reinstalls_the_unit_at_the_recomputed_interval(tmp_path, monkeypatch):
    _pinned_job(tmp_path)
    adapter = _FakeAdapter()
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: adapter)
    cmds.cmd_reset(_args(), _config(tmp_path))
    assert adapter.installed_with, "reset must rewrite the unit, not only the record"


def test_reset_keeps_the_history_by_default(tmp_path, monkeypatch):
    _pinned_job(tmp_path)
    path = _seed_history(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    cmds.cmd_reset(_args(), _config(tmp_path))
    assert len(history.read_runs(path)) == 4


def test_reset_history_discards_the_measurements(tmp_path, monkeypatch):
    _pinned_job(tmp_path)
    path = _seed_history(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert cmds.cmd_reset(_args(history=True), _config(tmp_path)) == 0
    assert history.read_runs(path) == []


def test_reset_history_says_what_it_is_about_to_destroy(tmp_path, monkeypatch, capsys):
    """Days of 7-minute runs is expensive to earn back. The count and the span
    go on screen before anything is deleted."""
    _pinned_job(tmp_path)
    _seed_history(tmp_path, n=6)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    cmds.cmd_reset(_args(history=True), _config(tmp_path))
    out = capsys.readouterr().out
    assert "6" in out
    assert "day" in out.lower()


def test_reset_history_needs_confirmation_when_not_told_yes(tmp_path, monkeypatch):
    _pinned_job(tmp_path)
    path = _seed_history(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    # Task 8's real `_confirm` takes `(args, prompt)`, not the single-argument
    # `(prompt)` this plan drafted before that signature existed. A one-arg
    # stub here raises a TypeError inside `_discard_history` instead of
    # standing in for a declined prompt, which would make this assertion
    # pass or fail for the wrong reason.
    monkeypatch.setattr(cmds, "_confirm", lambda args, prompt: False)
    assert cmds.cmd_reset(_args(history=True, yes=False), _config(tmp_path)) == 1
    assert len(history.read_runs(path)) == 4


def test_reset_history_with_no_history_is_not_an_error(tmp_path, monkeypatch):
    _pinned_job(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert cmds.cmd_reset(_args(history=True), _config(tmp_path)) == 0


def test_reset_with_no_job_installed_can_still_clear_history(tmp_path):
    """The measurements belong to the machine, not to a job. Clearing them must
    not require a job to exist."""
    path = _seed_history(tmp_path)
    assert cmds.cmd_reset(_args(history=True), _config(tmp_path)) == 0
    assert history.read_runs(path) == []


def test_reset_of_an_unknown_job_is_an_error(tmp_path):
    assert cmds.cmd_reset(_args(job="ghost"), _config(tmp_path)) != 0


def test_a_failed_install_does_not_destroy_the_measurements(tmp_path, monkeypatch):
    """A partial failure must leave a state the user can retry. Discarding
    first and then failing to install loses days of runs for a reset that did
    not happen.
    """
    _pinned_job(tmp_path, failures=5)
    path = _seed_history(tmp_path, n=6)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _RefusingAdapter())
    assert cmds.cmd_reset(_args(history=True), _config(tmp_path)) == 1
    assert len(history.read_runs(path)) == 6, "measurements destroyed by a failed reset"
    job = jobs.read_jobs(jobs.jobs_path(_config(tmp_path)))[jobs.DEFAULT_JOB]
    assert job.interval == "2h", "the pin was cleared despite the failure"
    assert job.failures == 5


def test_a_reset_that_cannot_rewrite_the_unit_leaves_the_record_alone(tmp_path, monkeypatch):
    """The unit must be rewritten before the record is, so a failed install
    never leaves the job claiming auto with a cleared backoff while the
    installed unit still runs the old pinned, backed-off interval."""
    path = _pinned_job(tmp_path, failures=5)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _RefusingAdapter())
    assert cmds.cmd_reset(_args(), _config(tmp_path)) == 1
    stored = jobs.read_jobs(path)[jobs.DEFAULT_JOB]
    assert stored.interval == "2h"
    assert stored.failures == 5


def test_reset_survives_a_state_read_failure_after_a_successful_install(
        tmp_path, monkeypatch):
    """The reset itself (record write, unit install) already succeeded by the
    time `_report_installed` reads state back. A `state()` raise there must
    degrade to a note, the way `cmd_status` handles the same call, not
    abort the command and read to a caller as a failed reset.
    """
    path = _pinned_job(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _StateRaisingAdapter())
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)
    rc = cmds.cmd_reset(_args(), _config(tmp_path))
    out = "\n".join(lines)
    assert rc == 0
    assert "Reset job" in out
    assert jobs.read_jobs(path)[jobs.DEFAULT_JOB].interval == "auto"
    assert "could not read" in out and "state" in out


def test_reset_reports_the_interval_actually_installed_not_the_recommendation(
        tmp_path, monkeypatch):
    """Finding 2 / the R28 defect landing on a third call site: cmd_reset
    printed the auto recommendation it computed and asked for, never what
    the adapter's render() says it actually installed. With no history
    seeded, the recommendation is the 6h cold-start default; the fake
    adapter here installs 1h, so the two are never accidentally equal.

    ``cmds.log`` is patched directly rather than read through capsys: see
    ``_log_lines`` in ``tests/test_schedule_run.py`` for why capsys reads
    back empty here once any earlier test in the session has called
    ``log()``.
    """
    _pinned_job(tmp_path)
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _RoundingFakeAdapter())
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)
    cmds.cmd_reset(_args(), _config(tmp_path))
    out = "\n".join(lines)
    assert "every 1h" in out
    assert "every 6h" not in out


class _RoundingFakeAdapter:
    """An adapter that installs a different interval than the one asked for,
    the way cron rounds down to the nearest expressible spec."""
    id = "fake"
    catches_up_after_sleep = True

    def install(self, job, interval_s, exec_argv, **options):
        return ["/tmp/fake.unit"]

    def render(self, job, interval_s, exec_argv, **options):
        return {"fake.unit": "", "interval_s": 3600.0}

    def state(self, job):
        return {"installed": True, "interval_s": 3600.0, "next_run": None,
                "exec_path": None, "notes": []}


class _RefusingAdapter:
    id = "refusing"
    catches_up_after_sleep = True

    def install(self, job, interval_s, exec_argv, **options):
        raise OSError("read-only home")

    def render(self, job, interval_s, exec_argv, **options):
        return {}

    def state(self, job):
        return {"installed": False, "interval_s": None, "next_run": None,
                "exec_path": None, "notes": []}


class _StateRaisingAdapter:
    """Install succeeds; reading state back afterward does not.

    Models the bus vanishing between `usable()` proving it answered at
    detect time and `_report_installed` reading state back: the same
    time-of-check-to-time-of-use gap `install()` already guards against.
    """
    id = "flaky"
    catches_up_after_sleep = True

    def install(self, job, interval_s, exec_argv, **options):
        return ["/tmp/flaky.unit"]

    def render(self, job, interval_s, exec_argv, **options):
        return {"flaky.unit": ""}

    def state(self, job):
        raise RuntimeError("bus vanished after the write")


class _FakeAdapter:
    def __init__(self):
        self.id = "fake"
        self.catches_up_after_sleep = True
        self.installed_with = None

    def install(self, job, interval_s, exec_argv, **options):
        self.installed_with = (job.name, interval_s)
        return ["/tmp/fake.unit"]

    def render(self, job, interval_s, exec_argv, **options):
        return {"fake.unit": ""}

    def state(self, job):
        return {"installed": True, "interval_s": 3600.0, "next_run": None,
                "exec_path": None, "notes": []}
