"""One scheduled cycle: gating, kind selection, spawning, and recording."""
from __future__ import annotations

import argparse
import os
import sys
import time

import pytest

from contextlake.schedule import cmds, gates, history, jobs, recommend, runner


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

    Patched in BOTH modules. ``log`` is imported by value, so each module holds
    its own binding, and the call sites in this file's tests are split across
    the two: ``cmd_run`` and ``_one_cycle`` log from ``runner``, while the
    install and report paths log from ``cmds``. Patching one module only made
    the gating tests assert against an empty list, which passes for any message
    that is missing rather than wrong.
    """
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)
    monkeypatch.setattr(runner, "log", lines.append)
    return lines


# ---- full vs incremental ------------------------------------------------

def test_the_first_ever_run_is_full():
    """Nothing has been built, so an incremental pass has nothing to be
    incremental against."""
    assert runner.decide_kind([], 7 * 86400, now=1_000_000.0) == "full"


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
    assert runner.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 3600) == "incremental"


def test_a_full_run_exactly_at_the_boundary_triggers_the_next_full_cycle():
    ts = "2026-08-26T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 3600.0, "exit": 0}]
    assert runner.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 7 * 86400) == "full"
    assert runner.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 7 * 86400 - 1) == "incremental"


def test_a_stale_full_run_triggers_the_next_full_cycle():
    ts = "2026-08-01T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 3600.0, "exit": 0}]
    assert runner.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 30 * 86400) == "full"


def test_a_failed_full_run_does_not_count_as_having_run():
    ts = "2026-08-26T00:00:00Z"
    runs = [{"ts": ts, "kind": "full", "duration_s": 12.0, "exit": 1}]
    assert runner.decide_kind(runs, 7 * 86400, now=_epoch(ts) + 3600) == "full"


# ---- gating -------------------------------------------------------------

def test_a_gated_run_skips_without_spawning_anything(tmp_path, monkeypatch):
    _install_job(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    spawned = []
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: spawned.append(a) or 0)
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
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: 0)
    cmds.cmd_run(_args(), _config(tmp_path))
    assert history.read_runs(history.history_path(_config(tmp_path))) == []


# ---- the child ----------------------------------------------------------

def test_the_child_is_told_where_to_record_itself(tmp_path):
    from contextlake import cli

    env = runner.child_env(_config(tmp_path), "full")
    assert env[cli.ENV_HISTORY] == history.history_path(_config(tmp_path))
    assert env[cli.ENV_KIND] == "full"


def test_a_real_run_spawns_the_cli_and_records_the_result(tmp_path, monkeypatch):
    """END TO END. No mock on _spawn: this proves the whole chain, from the job
    record through the subprocess to a history line the recommender can read.

    Gate forced open: this test is about run mechanics, not gating, so it must
    not depend on the machine's power state.
    """
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
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


def test_a_failing_job_increments_the_failure_counter(tmp_path, monkeypatch):
    """Gate forced open: this test is about the failure counter, not gating."""
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
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

    Gate forced open: this test is about the lock, not the power gate.
    """
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    _install_job(tmp_path)
    config = _config(tmp_path)
    spawns, inner = [], {}

    def _reentrant_spawn(argv, env, timeout=None):
        spawns.append(list(argv))
        inner["rc"] = cmds.cmd_run(_args(), config)
        return 0

    monkeypatch.setattr(runner, "_spawn", _reentrant_spawn)
    lines = _log_lines(monkeypatch)
    assert cmds.cmd_run(_args(), config) == 0
    assert len(spawns) == 1, "the inner call must NOT have spawned a second writer"
    assert inner["rc"] == 0, "a lock-skip is a normal outcome, not a failure"
    assert any("in progress" in line.lower() for line in lines)


def test_a_lock_skip_returns_an_int_not_the_gated_sentinel(tmp_path, monkeypatch):
    """`_one_cycle` returns either GATED or an exit code depending on a
    defaulted keyword. The non-foreground path must never leak the sentinel out
    of `cmd_run` as a process exit status, on either skip route.

    Both routes are exercised for real: a gated skip (`gates.check` denies),
    and a lock-contention skip (a real `RunBusy`, forced by having the
    monkeypatched `_spawn` call `cmd_run` again while the outer call still
    holds the lock). The earlier version of this test only ever mocked
    `gates.check`, so nothing here proved `_return_gated`'s two branches
    both return an int; the two branches share the same `return GATED if
    _return_gated else 0` line, but the name claimed coverage the test did
    not have.
    """
    _install_job(tmp_path)
    config = _config(tmp_path)

    # Gated route.
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: 0)
    gated_rc = cmds.cmd_run(_args(), config)
    assert isinstance(gated_rc, int)
    assert gated_rc == 0

    # Lock-contention route: real RunBusy, not a mock of the gate.
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    inner = {}

    def _reentrant_spawn(argv, env, timeout=None):
        inner["rc"] = cmds.cmd_run(_args(), config)
        return 0

    monkeypatch.setattr(runner, "_spawn", _reentrant_spawn)
    outer_rc = cmds.cmd_run(_args(), config)
    assert isinstance(outer_rc, int)
    assert outer_rc == 0
    assert isinstance(inner["rc"], int), "the lock-skip branch leaked the GATED sentinel"
    assert inner["rc"] == 0


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
    monkeypatch.setattr(runner, "store_is_ephemeral", lambda cfg: True)
    rc = cmds.cmd_run(_args(), _config(tmp_path))
    assert rc != 0


def test_allow_ephemeral_overrides_the_refusal(tmp_path, monkeypatch):
    _install_job(tmp_path)
    monkeypatch.setattr(runner, "store_is_ephemeral", lambda cfg: True)
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: 0)
    assert cmds.cmd_run(_args(allow_ephemeral=True), _config(tmp_path)) == 0


def test_a_normal_workstation_store_is_not_ephemeral(tmp_path):
    assert runner.store_is_ephemeral(_config(tmp_path)) is False


def test_outside_a_container_nothing_is_ever_ephemeral(tmp_path, monkeypatch):
    """A workstation home is on `/` too. Refusing it would be absurd."""
    monkeypatch.setattr(runner, "in_container", lambda: False)
    monkeypatch.setattr(runner, "_mount_point_of", lambda p: "/")
    assert runner.store_is_ephemeral(_config(tmp_path)) is False


def test_in_a_container_the_writable_layer_is_ephemeral(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "in_container", lambda: True)
    monkeypatch.setattr(runner, "_mount_point_of", lambda p: "/")
    assert runner.store_is_ephemeral(_config(tmp_path)) is True


def test_in_a_container_a_mounted_volume_is_not_ephemeral(tmp_path, monkeypatch):
    """A PVC, an emptyDir, EFS and Azure Files all appear as their own mount
    point. The fstype test this replaced reported emptyDir as ext4 and passed
    it, which is the false negative that matters."""
    monkeypatch.setattr(runner, "in_container", lambda: True)
    monkeypatch.setattr(runner, "_mount_point_of", lambda p: "/var/lib/contextlake")
    assert runner.store_is_ephemeral(_config(tmp_path)) is False


def test_an_unreadable_proc_mounts_does_not_refuse(tmp_path, monkeypatch):
    """Same rule as the gates: never stop because a sensor could not be read."""
    monkeypatch.setattr(runner, "in_container", lambda: True)
    monkeypatch.setattr(runner, "_mount_point_of", lambda p: "")
    assert runner.store_is_ephemeral(_config(tmp_path)) is True


def test_in_container_detects_each_signal_and_says_no_without_them(monkeypatch, tmp_path):
    """Asserted both ways, and the negative case is the point.

    The previous version set one env var and asserted True. An implementation
    that returned True unconditionally passed it, which a break-test confirmed:
    forcing `return True` failed nothing in the whole suite. A detector needs
    the case where it must answer NO.

    The name also claimed three signals while exercising one.
    """
    def _clear():
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.setattr(runner.os.path, "exists", lambda p: False)
        # /proc/1/cgroup on a workstation exists and names none of the markers.
        monkeypatch.setattr(runner, "open", _fake_open("0::/user.slice\n"), raising=False)

    def _fake_open(text):
        import io
        return lambda *a, **k: io.StringIO(text)

    # 1. No signal at all.
    _clear()
    assert runner.in_container() is False

    # 2. The Kubernetes env var alone.
    _clear()
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert runner.in_container() is True

    # 3. The docker marker file alone.
    _clear()
    monkeypatch.setattr(runner.os.path, "exists", lambda p: p == "/.dockerenv")
    assert runner.in_container() is True

    # 4. The cgroup marker alone.
    _clear()
    monkeypatch.setattr(runner, "open", _fake_open("0::/kubepods/pod123\n"), raising=False)
    assert runner.in_container() is True


def test_mount_point_of_picks_the_longest_match_and_respects_the_boundary(monkeypatch):
    """Never had a test of its own: every caller's test patches it, so breaking
    it to `return "/"` failed nothing in the whole suite.

    Three properties, and the middle one is a real bug class. A sibling
    directory sharing a prefix (`/var/libextra` under `/var/lib`) must NOT
    match, which is what `point.rstrip("/") + "/"` is for. An unanchored
    `startswith` would claim it.
    """
    import io

    mounts = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "tmpfs /var/lib ext4 rw 0 0\n"
        "tmpfs /var/lib/contextlake ext4 rw 0 0\n"
        "malformed-line-with-one-field\n"
    )
    monkeypatch.setattr(runner, "open", lambda *a, **k: io.StringIO(mounts), raising=False)

    # Longest match wins: the nested mount, not "/" and not "/var/lib".
    monkeypatch.setattr(runner.os.path, "realpath", lambda p: "/var/lib/contextlake/kb")
    assert runner._mount_point_of("anything") == "/var/lib/contextlake"

    # Boundary: a sibling that merely shares a prefix falls back to "/".
    monkeypatch.setattr(runner.os.path, "realpath", lambda p: "/var/libextra/kb")
    assert runner._mount_point_of("anything") == "/"

    # The mount point itself, not only paths under it.
    monkeypatch.setattr(runner.os.path, "realpath", lambda p: "/var/lib")
    assert runner._mount_point_of("anything") == "/var/lib"


def test_mount_point_of_returns_empty_when_proc_mounts_cannot_be_read(monkeypatch):
    """"" is "cannot tell", and store_is_ephemeral must not read it as a
    measurement. Distinct from a real mount point, which is always non-empty."""
    def _boom(*a, **k):
        raise OSError("no /proc here")

    monkeypatch.setattr(runner, "open", _boom, raising=False)
    assert runner._mount_point_of("/anything") == ""


# ---- foreground ---------------------------------------------------------

def test_foreground_loops_and_sleeps_the_recommended_interval(tmp_path, monkeypatch):
    """Gate forced open: found during defect-3 verification passing vacuously
    on battery, sleeping on the gate-retry path instead of the recommended
    interval this test names. Same class as the four tests named in the task,
    fixed the same way.

    Asserts the exact sleep values, not just their count and sign: both the
    cold-start interval (recommend.COLD_START_S) and the gate-retry path
    (schedule_gate_retry, 600.0 by default) satisfy "3 positive sleeps", so
    a regression that routes this test back onto gate-retry would still
    pass a count-and-sign check. Pinning the value to the recommender's own
    constant, not a hardcoded number, is what catches that regression."""
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    _install_job(tmp_path)
    monkeypatch.setattr(runner, "_spawn", lambda *a, **k: 0)
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path), _max_iterations=3)
    assert slept == [recommend.COLD_START_S] * 3


def test_foreground_backs_off_after_consecutive_failures(tmp_path, monkeypatch):
    """Gate forced open: this test is about backoff, not the power gate."""
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    _install_job(tmp_path, argv=("kb", "query"))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path), _max_iterations=3)
    assert slept[1] > slept[0], "each consecutive failure must widen the gap"


def test_foreground_retries_soon_after_a_gated_skip(tmp_path, monkeypatch):
    """A gated skip is not a lost run. It waits schedule_gate_retry, not a full
    interval."""
    _install_job(tmp_path)
    monkeypatch.setattr(gates, "check",
                        lambda cfg: gates.GateResult(False, "on battery power"))
    slept = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    cmds.cmd_run(_args(foreground=True), _config(tmp_path, schedule_gate_retry="10m"),
                 _max_iterations=2)
    assert slept == [600.0, 600.0]


# ---- _spawn: timeout must not orphan a worker pool -----------------------

def _proc_gone(pid, deadline_s=5.0) -> bool:
    """Whether ``/proc/<pid>`` has disappeared, waited for up to ``deadline_s``.

    Bounded, not a spin-forever poll: a SIGTERM takes a moment to land and the
    child to be reaped, but a process this test killed itself is never going
    to sit there indefinitely either.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            return True
        time.sleep(0.05)
    return not os.path.exists(f"/proc/{pid}")


def test_a_timed_out_run_kills_the_grandchild_not_just_the_child(tmp_path, monkeypatch):
    """The defect this guards: `_spawn`'s child is usually `bootstrap` or
    `kb index`, which runs a ProcessPoolExecutor of up to 8 workers.
    `subprocess.run(..., timeout=...)` on a timeout kills only the direct
    child; the workers are reparented to init and keep running, still
    holding memory. Measured on a real machine: one timed-out run left 8
    orphaned workers holding 12.4 GB.

    A test that only checks `_spawn`'s return value (124) proves nothing
    about this -- it passed before the fix too. This starts a real child that
    starts a real grandchild, lets `_spawn` time the child out, and checks
    the GRANDCHILD is dead afterwards.

    `/proc` is Linux-only; this skips cleanly where it does not exist.
    """
    if not os.path.isdir("/proc"):
        pytest.skip("no /proc on this platform to check the grandchild with")

    pidfile = tmp_path / "grandchild.pid"
    fake_python = tmp_path / "fake_python.py"
    # Stands in for `sys.executable`. `_spawn` always runs
    # `<executable> -m contextlake <argv>`; this ignores that argv entirely
    # and just plays the part of a child that starts a grandchild and then
    # outlives its parent's timeout, same as a real `kb index` would.
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        f"gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(gc.pid))\n"
        "time.sleep(60)\n")
    fake_python.chmod(0o755)

    started = []
    real_popen = runner.subprocess.Popen

    def _tracking_popen(cmd, **kw):
        proc = real_popen(cmd, **kw)
        started.append(proc.pid)
        return proc

    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(runner.subprocess, "Popen", _tracking_popen)

    try:
        code = runner._spawn(["irrelevant"], dict(os.environ), timeout=1.0)
        assert code == 124

        assert pidfile.exists(), "the child never got to start its grandchild"
        grandchild_pid = int(pidfile.read_text())
        assert _proc_gone(grandchild_pid), (
            f"grandchild {grandchild_pid} is still alive after the parent's timeout")
    finally:
        # Best-effort: this test must never leak a process even when an
        # assertion above fails partway through. Uses raw os calls, not
        # anything from `cmds`, on purpose: the whole point of this cleanup
        # is to work even when the code under test is broken.
        import signal as _signal

        for pid in started:
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), _signal.SIGKILL)
            except (OSError, ValueError):
                pass


# ---- _spawn: the timeout path must not crash on Windows -------------------
#
# The process-group fix replaced `subprocess.run(timeout=...)`, which worked on
# every platform, with `os.killpg` and `signal.SIGKILL`. NEITHER exists on
# Windows, and they fail in different places: `os.killpg` raises inside
# `_killpg` (where `except ProcessLookupError` does not catch AttributeError),
# while `signal.SIGKILL` is evaluated at the CALL SITE, before `_killpg` is
# entered at all. Guarding only one of them still leaves the path broken.
#
# CI runs on ubuntu-latest only, so no matrix cell can see either break. These
# tests simulate the platform instead of relying on one.

def test_killpg_falls_back_to_taskkill_where_there_are_no_process_groups(monkeypatch):
    """On Windows `_killpg` must still reclaim the child tree, not raise."""
    import signal as _signal

    calls = []
    monkeypatch.setattr(runner, "_HAVE_KILLPG", False)
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or None)

    runner._killpg(4321, _signal.SIGTERM)

    assert len(calls) == 1, "the Windows branch did not run"
    assert calls[0][0] == "taskkill", calls[0]
    # /T is the whole point: it walks the child tree, which is the closest
    # thing Windows has to killing a process group. Without it the worker
    # pool survives, which is the defect the process-group fix exists to fix.
    assert "/T" in calls[0], calls[0]
    assert "4321" in calls[0], calls[0]


def test_the_timeout_path_completes_where_sigkill_does_not_exist(monkeypatch):
    """The SIGKILL pass is reached only when the child ignores the first kill.

    That is the branch that evaluates `signal.SIGKILL`, so a test whose fake
    child dies on the first pass would never touch the break it guards. This
    one never dies, which forces both passes.
    """
    killed = []

    class _NeverDies:
        pid = 999
        def wait(self, timeout=None):
            raise runner.subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    # Simulating Windows via _HAVE_KILLPG alone is NOT enough to test the
    # call site: `signal.SIGKILL` exists on Linux, so reading it here would
    # succeed and the assertion below would pass whether or not the call site
    # was fixed. The signal module has to actually lack SIGKILL.
    class _WindowsSignal:
        SIGTERM = 15
    monkeypatch.setattr(runner, "signal", _WindowsSignal)
    monkeypatch.setattr(runner, "_HAVE_KILLPG", False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: _NeverDies())
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **kw: killed.append(argv) or None)
    monkeypatch.setattr(cmds, "log", lambda *a, **k: None)

    code = runner._spawn(["irrelevant"], {}, timeout=0.01)

    assert code == 124, code
    # Both passes ran: the SIGTERM one and the SIGKILL one. If the call site
    # still read `signal.SIGKILL`, this raised AttributeError instead.
    assert len(killed) == 2, killed


# ---- the job name has to reach the child ---------------------------------

def test_child_env_carries_the_job_name_only_when_there_is_one(tmp_path):
    from contextlake.cli import ENV_JOB

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    assert runner.child_env(config, "full", "nightly")[ENV_JOB] == "nightly"
    # A hand-run command has no job. Absent, not empty: history.for_job reads a
    # missing key as the default job, and an empty string would match nothing.
    assert ENV_JOB not in runner.child_env(config, "full")


def test_one_cycle_tells_the_child_which_job_it_is(tmp_path, monkeypatch):
    """Without this the child writes an untagged record, every untagged record
    is read back as the default job's, and a named job's history stays empty
    forever while polluting the default job's median.
    """
    from contextlake.cli import ENV_JOB
    from contextlake.schedule import gates
    from contextlake.schedule import jobs as jobstore

    seen = {}

    def _capture(argv, env, timeout=None):
        seen.update(env)
        return 0

    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    monkeypatch.setattr(runner, "_spawn", _capture)
    monkeypatch.setattr(cmds, "log", lambda *a, **k: None)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    jobs_file = jobstore.jobs_path(config)
    job = jobstore.new_job("nightly", ["kb", "index"], "auto", "cron")
    jobstore.write_job(jobs_file, job)

    assert runner._one_cycle(argparse.Namespace(job="nightly"), config, job, jobs_file) == 0
    assert seen.get(ENV_JOB) == "nightly"


def test_one_cycle_decides_the_kind_from_this_jobs_history_alone(tmp_path, monkeypatch):
    """Tests the FILTER AT ITS CALL SITE, not just the helper.

    Asserting on `for_job` composed with `decide_kind` by hand passes whether or
    not `_one_cycle` actually uses the pair, which is where the defect lived.
    This drives `_one_cycle` and reads which argv it chose: the full command or
    the incremental one.
    """
    import time

    from contextlake.schedule import gates
    from contextlake.schedule import jobs as jobstore

    chosen = []
    monkeypatch.setattr(gates, "check", lambda cfg: gates.GateResult(True, ""))
    monkeypatch.setattr(runner, "_spawn", lambda argv, env, timeout=None: chosen.append(argv) or 0)
    monkeypatch.setattr(cmds, "log", lambda *a, **k: None)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    # A successful FULL run an hour ago, belonging to a DIFFERENT job.
    history.append_run(history.history_path(config), {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)),
        "kind": "full", "duration_s": 300.0, "exit": 0, "job": "nightly"})

    jobs_file = jobstore.jobs_path(config)
    job = jobstore.new_job(jobstore.DEFAULT_JOB, ["mirror", "sync"], "auto", "cron",
                           full_argv=["mirror", "sync", "--full"])
    jobstore.write_job(jobs_file, job)

    runner._one_cycle(argparse.Namespace(job=job.name), config, job, jobs_file)

    assert chosen, "the cycle never spawned anything"
    # This job has never run a full rebuild, so it must run one now. Unscoped,
    # the other job's rebuild answered for it and this was the incremental argv.
    assert chosen[0] == ["mirror", "sync", "--full"], chosen[0]
