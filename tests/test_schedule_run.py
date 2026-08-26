"""One scheduled cycle: gating, kind selection, spawning, and recording."""
from __future__ import annotations

import argparse

from contextlake.schedule import cmds, gates, history, jobs


def _config(tmp_path, **kw):
    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    config.update(kw)
    return config


def _args(**kw):
    base = dict(action="run", job=None, foreground=False, allow_ephemeral=False,
                json=False, rest=[], interval=None, quiet=True, verbose=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _install_job(tmp_path, argv=("version",), interval="auto"):
    path = jobs.jobs_path(_config(tmp_path))
    jobs.write_job(path, jobs.new_job(jobs.DEFAULT_JOB, list(argv), interval, "systemd"))
    return path


def _log_lines(monkeypatch):
    """Capture ``cmds.log()`` calls directly rather than through capsys.

    The package logger's ``propagate`` flag turns ``False`` the first time
    anything in the whole pytest session calls ``log()``, and from then on
    pytest's own ``catching_logs`` (see ``_pytest/logging.py``) attaches its
    report handler straight to it for every later test's call phase. That
    handler pre-empts ``get_logger()``'s lazy ``setup_logging()``, so the
    console handler bound to the current capsys buffer never gets attached
    and ``capsys.readouterr().out`` reads empty regardless of what ran. Two
    existing suites in this repository (``tests/kb/test_kb_dashboard_cmd.py``,
    ``tests/kb/test_kb_config.py``) hit and documented the identical failure
    mode and route around it the same way: patch ``log`` at its call site.
    """
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)
    return lines


# ---- full vs incremental ------------------------------------------------

def test_the_first_ever_run_is_full():
    """Nothing has been built, so an incremental pass has nothing to be
    incremental against."""
    assert cmds.decide_kind([], 7 * 86400, now=1_000_000.0) == "full"


def _epoch(ts):
    """Compute it from the same format the implementation parses. A hardcoded
    epoch constant makes these three tests pass for the wrong reason if it is
    off by a day."""
    from datetime import datetime, timezone

    return (datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc).timestamp())


def test_a_recent_full_run_keeps_the_cycle_incremental():
    ts = "2026-08-26T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 3600.0, "exit": 0}]
    assert cmds.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 3600) == "incremental"


def test_a_full_run_exactly_at_the_boundary_triggers_the_next_full_cycle():
    ts = "2026-08-26T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 3600.0, "exit": 0}]
    assert cmds.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 7 * 86400) == "full"
    assert cmds.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 7 * 86400 - 1) == "incremental"


def test_a_stale_full_run_triggers_the_next_full_cycle():
    ts = "2026-08-01T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 3600.0, "exit": 0}]
    assert cmds.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 30 * 86400) == "full"


def test_a_failed_full_run_does_not_count_as_having_run():
    ts = "2026-08-26T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 12.0, "exit": 1}]
    assert cmds.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 3600) == "full"


# ---- gating -------------------------------------------------------------

def test_a_gated_run_skips_without_spawning_anything(tmp_path, monkeypatch):
    _install_job(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    spawned = []
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **k: spawned.append(a) or 0)
    lines = _log_lines(monkeypatch)
    rc = cmds.cmd_run(_args(), _config(tmp_path))
    assert rc == 0, "a gated skip is a normal outcome, not a failure"
    assert spawned == []
    assert any("battery" in line.lower() for line in lines)


def test_a_gated_skip_records_nothing_in_the_history(tmp_path, monkeypatch):
    """It did not measure a run, so it must not write one. A zero-duration
    record would drag the median to nothing."""
    _install_job(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **k: 0)
    cmds.cmd_run(_args(), _config(tmp_path))
    assert history.read_runs(history.history_path(_config(tmp_path))) == []


# ---- the child ----------------------------------------------------------

def test_the_child_is_told_where_to_record_itself(tmp_path):
    from contextlake import cli

    env = cmds.child_env(_config(tmp_path), "full")
    assert env[cli.ENV_HISTORY] == history.history_path(_config(tmp_path))
    assert env[cli.ENV_KIND] == "full"


def test_a_real_run_spawns_the_cli_and_records_the_result(tmp_path):
    """END TO END. No mock on _spawn: this proves the whole chain, from the job
    record through the subprocess to a history line the recommender can read."""
    _install_job(tmp_path, argv=("version",))
    config = _config(tmp_path)
    assert cmds.cmd_run(_args(), config) == 0
    runs = history.read_runs(history.history_path(config))
    assert len(runs) == 1
    assert runs[0]["exit"] == 0
    job = jobs.read_jobs(jobs.jobs_path(config))[jobs.DEFAULT_JOB]
    assert job.failures == 0
    assert job.last_exit == 0
    assert job.last_run is not None


def test_a_failing_job_increments_the_failure_counter(tmp_path):
    _install_job(tmp_path, argv=("kb", "query"))
    config = _config(tmp_path)
    assert cmds.cmd_run(_args(), config) != 0
    assert jobs.read_jobs(jobs.jobs_path(config))[jobs.DEFAULT_JOB].failures == 1


def test_two_overlapping_runs_skip_rather_than_both_proceed(tmp_path, monkeypatch):
    """AWS risk register R8. The second must skip with a logged reason, never
    queue a second writer.

    Asserted on the OBSERVABLE difference: exactly one spawn, and a skip named
    in the output. Asserting only that both calls returned 0 could not tell a
    lock-skip from a second run that proceeded, which is the whole question.
    """
    _install_job(tmp_path)
    config = _config(tmp_path)
    spawns, inner = [], {}

    def _reentrant_spawn(argv, env, timeout=None):
        spawns.append(list(argv))
        inner["rc"] = cmds.cmd_run(_args(), config)
        return 0

    monkeypatch.setattr(cmds, "_spawn", _reentrant_spawn)
    lines = _log_lines(monkeypatch)
    assert cmds.cmd_run(_args(), config) == 0
    assert len(spawns) == 1, "the inner call must NOT have spawned a second writer"
    assert inner["rc"] == 0, "a lock-skip is a normal outcome, not a failure"
    assert any("in progress" in line.lower() for line in lines)


def test_a_lock_skip_returns_an_int_not_the_gated_sentinel(tmp_path, monkeypatch):
    """`_one_cycle` returns either GATED or an exit code depending on a
    defaulted keyword. The non-foreground path must never leak the sentinel out
    of `cmd_run` as a process exit status."""
    _install_job(tmp_path)
    config = _config(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **k: 0)
    assert cmds.cmd_run(_args(), config) == 0
    assert isinstance(cmds.cmd_run(_args(), config), int)


def test_running_an_unknown_job_is_an_error_not_a_silent_no_op(tmp_path):
    assert cmds.cmd_run(_args(job="ghost"), _config(tmp_path)) != 0


def test_run_with_no_job_installed_tells_you_how_to_install_one(tmp_path, monkeypatch):
    lines = _log_lines(monkeypatch)
    assert cmds.cmd_run(_args(), _config(tmp_path)) != 0
    assert any("schedule install" in line for line in lines)


# ---- ephemeral storage --------------------------------------------------

def test_an_ephemeral_store_refuses_to_run(tmp_path, monkeypatch):
    """A container with no persistent store re-indexes the whole fleet every
    run: hours and gigabytes instead of a 7-minute incremental."""
    _install_job(tmp_path)
    monkeypatch.setattr(cmds, "store_is_ephemeral", lambda cfg: True)
    rc = cmds.cmd_run(_args(), _config(tmp_path))
    assert rc != 0


def test_allow_ephemeral_overrides_the_refusal(tmp_path, monkeypatch):
    _install_job(tmp_path)
    monkeypatch.setattr(cmds, "store_is_ephemeral", lambda cfg: True)
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **k: 0)
    assert cmds.cmd_run(_args(allow_ephemeral=True), _config(tmp_path)) == 0


def test_a_normal_workstation_store_is_not_ephemeral(tmp_path):
    assert cmds.store_is_ephemeral(_config(tmp_path)) is False


def test_outside_a_container_nothing_is_ever_ephemeral(tmp_path, monkeypatch):
    """A workstation home is on `/` too. Refusing it would be absurd."""
    monkeypatch.setattr(cmds, "in_container", lambda: False)
    monkeypatch.setattr(cmds, "_mount_point_of", lambda p: "/")
    assert cmds.store_is_ephemeral(_config(tmp_path)) is False


def test_in_a_container_the_writable_layer_is_ephemeral(tmp_path, monkeypatch):
    monkeypatch.setattr(cmds, "in_container", lambda: True)
    monkeypatch.setattr(cmds, "_mount_point_of", lambda p: "/")
    assert cmds.store_is_ephemeral(_config(tmp_path)) is True


def test_in_a_container_a_mounted_volume_is_not_ephemeral(tmp_path, monkeypatch):
    """A PVC, an emptyDir, EFS and Azure Files all appear as their own mount
    point. The fstype test this replaced reported emptyDir as ext4 and passed
    it, which is the false negative that matters."""
    monkeypatch.setattr(cmds, "in_container", lambda: True)
    monkeypatch.setattr(cmds, "_mount_point_of", lambda p: "/var/lib/contextlake")
    assert cmds.store_is_ephemeral(_config(tmp_path)) is False


def test_an_unreadable_proc_mounts_does_not_refuse(tmp_path, monkeypatch):
    """Same rule as the gates: never stop because a sensor could not be read."""
    monkeypatch.setattr(cmds, "in_container", lambda: True)
    monkeypatch.setattr(cmds, "_mount_point_of", lambda p: "")
    assert cmds.store_is_ephemeral(_config(tmp_path)) is True


def test_in_container_detects_the_three_signals(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert cmds.in_container() is True


# ---- foreground ---------------------------------------------------------

def test_foreground_loops_and_sleeps_the_recommended_interval(tmp_path, monkeypatch):
    _install_job(tmp_path)
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **k: 0)
    slept = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path), _max_iterations=3)
    assert len(slept) == 3
    assert all(s > 0 for s in slept)


def test_foreground_backs_off_after_consecutive_failures(tmp_path, monkeypatch):
    _install_job(tmp_path, argv=("kb", "query"))
    monkeypatch.setattr(cmds.time, "sleep", lambda s: None)
    slept = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path), _max_iterations=3)
    assert slept[1] > slept[0], "each consecutive failure must widen the gap"


def test_foreground_retries_soon_after_a_gated_skip(tmp_path, monkeypatch):
    """A gated skip is not a lost run. It waits schedule_gate_retry, not a full
    interval."""
    _install_job(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    slept = []
    monkeypatch.setattr(cmds.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path, schedule_gate_retry="10m"),
                 _max_iterations=2)
    assert slept == [600.0, 600.0]
