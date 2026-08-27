"""Running one scheduled cycle, and the foreground loop.

Split out of ``cmds.py``. This is where the SCHEDULED WORK runs: the only place
that starts a contextlake command as a child, and the only one that needs a
process group to kill. The adapters and ``gates`` also shell out, to
``systemctl``, ``crontab`` and the battery probes, but those are short reads
whose output is parsed, not long-running children that own a worker pool.
The process-group kill, the container and ephemeral-store probes, and the cycle
itself live together for that reason.

Core tier. Nothing here may import ``contextlake.kb`` at module level, which
``tests/test_schedule_source_tier.py`` enforces. The scheduled child reaches the
knowledge layer by being a SUBPROCESS, never by import.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..logging_setup import log
from . import history, recommend
from .settings import _duration_or, current_recommendation, settings_from_config

_HAVE_KILLPG = hasattr(os, "killpg") and hasattr(os, "getpgid")


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def child_env(config, kind, job_name=None) -> dict:
    """The environment a scheduled child runs in.

    Three variables carry everything the child needs to record itself, so it
    does not have to re-derive the history location from a config it may never
    load (a `kb index` job never reads the mirror INI). The job name is what
    lets one shared history file be read back per job.
    """
    from ..cli import ENV_HISTORY, ENV_JOB, ENV_KIND

    env = dict(os.environ)
    env[ENV_HISTORY] = history.history_path(config)
    env[ENV_KIND] = kind
    if job_name:
        env[ENV_JOB] = job_name
    return env


def _spawn(argv, env, timeout=None) -> int:
    """Run one contextlake command as a child. Returns its exit code.

    ``sys.executable -m contextlake`` rather than a bare ``contextlake``: the
    unit may run with a different PATH, and a venv that moved would otherwise
    fail silently forever.

    ``start_new_session=True`` puts the child in a new session and process
    group of its own, detached from this one. That is what makes it safe to
    kill the child's *group* on timeout below: without it, the child shares
    this process's group, and killing the group would kill the scheduler too.
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "contextlake"] + list(argv),
            env=env, start_new_session=True)
    except OSError as e:
        log(f"Could not start the scheduled run: {e}")
        return 127
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the GROUP, not the process. The child is usually `bootstrap`
        # or `kb index`, which runs a ProcessPoolExecutor with up to 8
        # workers; killing only the direct child leaves that whole pool
        # alive, reparented to init and still holding memory. Measured on
        # this machine: one timed-out run left 8 orphaned workers holding
        # 12.4 GB, with the box down to 1.3 GB available.
        #
        # SIGTERM before SIGKILL: the store takes an advisory lock while it
        # writes, and an abrupt SIGKILL mid-write leaves a stale lock for the
        # next run to reclaim. SIGTERM gives the child, and anything it
        # started, a chance to release that lock; SIGKILL alone never does.
        # SIGKILL is still the fallback, so a child that ignores SIGTERM
        # cannot hold the group open forever.
        log(f"The scheduled run exceeded its timeout of {timeout}s and was killed.")
        _killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, _SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log(f"  pid {proc.pid} did not die; it may still be running.")
        return 124


def _killpg(pid, sig) -> None:
    """Send ``sig`` to ``pid``'s whole process group. Silent if the group is
    already gone: it can exit between the timeout firing and this running,
    and that is success, not a failure worth logging."""
    if not _HAVE_KILLPG:
        # Windows: no process groups, so `taskkill /T` walks the child tree
        # instead and the worker pool is still reclaimed. `/T` always forces
        # the descendants, so the SIGTERM pass cannot be graceful here; that
        # is the platform's limit, not a choice. Before the process-group fix
        # this path used subprocess.run(timeout=...), which worked on Windows,
        # so leaving os.killpg unguarded would REGRESS a platform pyproject.toml
        # claims ("Operating System :: OS Independent").
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        pass


def decide_kind(runs, full_every_s, now=None) -> str:
    """``"full"`` or ``"incremental"`` for the next cycle.

    A full rebuild happens when the last SUCCESSFUL one is older than
    ``schedule_full_every``, and on the first run, where an incremental
    pass has nothing to be incremental against. A failed full run does not
    count as having run, or one broken night would postpone the rebuild by a
    whole cycle.
    """
    now = time.time() if now is None else now
    stamps = []
    for record in runs:
        if record.get("kind") != "full" or record.get("exit") != 0:
            continue
        try:
            stamps.append(datetime.strptime(str(record.get("ts")), "%Y-%m-%dT%H:%M:%SZ")
                          .replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            continue
    if not stamps:
        return "full"
    return "full" if (now - max(stamps)) >= float(full_every_s) else "incremental"


def in_container() -> bool:
    """Whether this process is running inside a container.

    Three independent signals, because no single one covers Docker, Kubernetes,
    OpenShift and plain podman. Any hit counts; none of them can false-positive
    on a workstation.
    """
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    return any(marker in text for marker in ("docker", "kubepods", "containerd", "libpod"))


def _mount_point_of(path) -> str:
    """The longest mount point in /proc/mounts that contains ``path``."""
    real = os.path.realpath(path)
    best = ""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        point = parts[1]
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) > len(best):
            best = point
    return best


def store_is_ephemeral(config) -> bool:
    """Whether this run's state lives on a container layer that will be discarded.

    **The test is "is it on its own mount?", not "what filesystem type is it?".**
    A first draft checked /proc/mounts for tmpfs and overlay. That is wrong in
    both directions, and both were verified on real numbers:

    - **False negative, the dangerous one.** A Kubernetes pod with any mounted
      volume gets the node's filesystem, so it reports ``ext4`` and passes the
      fstype test even when the volume is an ``emptyDir``.
    - **False positive.** This machine's own cache directory was measured and
      sits on ``ext4`` at ``/``, but plenty of hosts present a home directory on
      an overlay mount and would have been refused for nothing.

    What distinguishes them: **a mounted volume is always its own mount
    point.** A PVC, an emptyDir, a bind mount, EFS and Azure Files all appear in
    /proc/mounts with their own target. The container's own writable layer does
    not; it is part of ``/``. So inside a container, state whose longest-prefix
    mount is ``/`` itself is on the throwaway layer, and state on any other mount
    point is on something somebody deliberately attached.

    Outside a container this always returns ``False``. A workstation's home
    directory is on ``/`` too, and refusing it would be absurd.
    """
    path = os.path.dirname(history.history_path(config)) or "."
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # Cannot even create the directory. Nothing will persist here.
        return True
    if not in_container():
        return False
    point = _mount_point_of(path)
    # An empty result means /proc/mounts was unreadable. Do not refuse on a
    # sensor we could not read: that is the same rule the gates follow.
    return point in ("/", "")


def cmd_run(args, config, _max_iterations=None) -> int:
    """One cycle, or the foreground loop."""
    from . import jobs as jobstore

    name = getattr(args, "job", None) or jobstore.DEFAULT_JOB
    jobs_file = jobstore.jobs_path(config)
    job = jobstore.read_jobs(jobs_file).get(name)
    if job is None:
        if name == jobstore.DEFAULT_JOB:
            log("No schedule is installed. Create one with "
                "`contextlake schedule install`.")
        else:
            log(f"No job named {name!r}. See `contextlake schedule list`.")
        return 2

    if store_is_ephemeral(config) and not getattr(args, "allow_ephemeral", False):
        log("Refusing to run: this container's state does not survive a restart, "
            "so every run would re-index the whole fleet from scratch.")
        log("  Mount a volume at the cache directory (a PVC on Kubernetes, EFS on "
            "AWS, Azure Files on Azure), or pass --allow-ephemeral if that "
            "is what you want.")
        return 2

    if not getattr(args, "foreground", False):
        return _one_cycle(args, config, job, jobs_file)

    # The config is the one resolved at dispatch and is NOT re-read each
    # iteration. A container running --foreground for a week does not pick up an
    # edited schedule_interval; restart it to apply one. Deliberate: re-reading
    # would let a half-written INI change the interval mid-loop, and a restart
    # is the normal way to reconfigure a container anyway.
    log(f"Running job {job.name!r} in the foreground. Ctrl-C to stop.")
    iterations = 0
    while _max_iterations is None or iterations < _max_iterations:
        gated = _one_cycle(args, config, job, jobs_file, _return_gated=True)
        job = jobstore.read_jobs(jobs_file).get(job.name, job)
        if gated is GATED:
            delay = _duration_or(config, "schedule_gate_retry", 600.0)
        else:
            rec, _ = current_recommendation(config, job.name)
            delay = recommend.backoff_interval(
                rec.interval_s, job.failures, settings_from_config(config)["max_s"])
        log(f"Next run in {recommend.format_duration(delay)}.")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log("Stopped.")
            return 0
        iterations += 1
    return 0


GATED = object()


def _one_cycle(args, config, job, jobs_file, _return_gated=False):
    from . import gates, runlock
    from . import jobs as jobstore

    verdict = gates.check(config)
    if not verdict.allowed:
        log(f"Skipping this run: {verdict.reason}. Retrying in "
            f"{recommend.format_duration(_duration_or(config, 'schedule_gate_retry', 600.0))}.")
        # NOT recorded. A skip measured nothing, and a zero-duration record
        # would drag the median toward zero.
        return GATED if _return_gated else 0

    # Scoped to THIS job. Every job appends to one file, so unscoped reads let
    # another job's full rebuild satisfy this job's schedule_full_every and
    # postpone a rebuild this job never had.
    runs = history.for_job(history.read_runs(history.history_path(config)), job.name)
    kind = decide_kind(runs, _duration_or(config, "schedule_full_every", 7 * 86400.0))
    argv = job.full_argv if kind == "full" else job.argv

    try:
        with runlock.RunLock(runlock.runlock_path(config), job.name):
            log(f"Running {kind} cycle: contextlake {' '.join(argv)}")
            code = _spawn(argv, child_env(config, kind, job.name))
    except runlock.RunBusy as e:
        # Skip, never queue. A second writer is the corruption this prevents.
        log(f"Skipping this run: {e}")
        return GATED if _return_gated else 0

    jobstore.record_outcome(jobs_file, job.name, code, history.utc_now_iso())
    if code != 0:
        log(f"The scheduled run exited {code}. See the log above.")
    return code
