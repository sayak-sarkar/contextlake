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

# Seconds to wait after SIGTERM, and again after SIGKILL, for the child and its group to end.
_STOP_GRACE_S = 5


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


def _parse_command(argv):
    """The parsed namespace of a job's command with the namespace collapsed (``command`` is
    the verb), or ``None`` when it does not parse. Prints nothing: a record edited by hand to
    something invalid is reported by the child that runs it, once."""
    import contextlib
    import io

    from ..cli import _resolve_command, build_parser

    parser = build_parser()
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            parsed = parser.parse_args(list(argv))
            _resolve_command(parsed, parser)
    except SystemExit:
        return None
    return parsed


def child_argv(argv, config=None, kb_config=None) -> list:
    """``argv``, the command a job runs, with the config files this run was given in front.

    ``config`` and ``kb_config`` are what the running process got on ITS OWN command line:
    for a unit, the ``--config`` and ``--kb-config`` it was installed with. They are never a
    path read from the job record. The record is a file on disk and ``reset`` renders the
    unit from it, so a path taken from there would reach the child without the install
    having put it in the unit. A config found by walking up is not here either: the child
    finds it the same way, from the working directory it inherits.

    Each command class gets the file it reads: ``bootstrap`` takes both, a ``kb`` verb the
    knowledge config as ``--config``, a ``mirror`` verb the sync INI as ``--config``. Any
    other command, or one that does not parse, gets nothing. A flag the job's own command
    already sets is left alone.
    """
    if not (config or kb_config):
        return list(argv)
    parsed = _parse_command(argv)
    if parsed is None:
        return list(argv)
    from ..cli import _ALIASES, _KB_COMMANDS, _MIRROR_NS, _NAMESPACE_OF

    command = _ALIASES.get(parsed.command, parsed.command)
    lead = []
    if command == "bootstrap":
        if config and not parsed.config:
            lead += ["--config", str(config)]
        if kb_config and not parsed.kb_config:
            lead += ["--kb-config", str(kb_config)]
    elif command in _KB_COMMANDS:
        if kb_config and not parsed.config:
            lead += ["--config", str(kb_config)]
    elif _NAMESPACE_OF.get(command) == _MIRROR_NS:
        if config and not parsed.config:
            lead += ["--config", str(config)]
    return lead + list(argv)


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
        # stdin from /dev/null: a scheduled run has no one to answer a prompt. Started by
        # `schedule run --foreground` in a terminal, the child would otherwise inherit it,
        # and `kb wiki` would stop at its first-run model question until someone typed.
        proc = subprocess.Popen(
            [sys.executable, "-m", "contextlake"] + list(argv),
            env=env, start_new_session=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        log(f"Could not start the scheduled run: {e}")
        return 127
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"The scheduled run exceeded its timeout of {timeout}s and was killed.")
        _stop_group(proc)
        return 124
    except KeyboardInterrupt:
        # Ctrl-C reaches only this process: the child is in a session of its own (above), so
        # the terminal never signals it. Left alone it would keep writing after the command
        # said "cancelled", and the run lock below would be released under it, letting a
        # second run start beside it. Stop the group first, then let the interrupt go on.
        log("Interrupted. Stopping the scheduled run.")
        try:
            _stop_group(proc)
        except KeyboardInterrupt:
            # A second Ctrl-C while waiting for the SIGTERM: skip the grace period. Still wait
            # for the group, because the run lock is released as the interrupt leaves.
            _killpg(proc.pid, _SIGKILL)
            _wait_after_kill(proc)
        raise


def _stop_group(proc) -> None:
    """Stop the child's whole process group: SIGTERM, then SIGKILL if it stays.

    Kill the GROUP, not the process. The child is usually `bootstrap`
    or `kb index`, which runs a ProcessPoolExecutor with up to 8
    workers; killing only the direct child leaves that whole pool
    alive, reparented to init and still holding memory. Measured on
    this machine: one timed-out run left 8 orphaned workers holding
    12.4 GB, with the box down to 1.3 GB available.

    SIGTERM before SIGKILL: the store takes an advisory lock while it
    writes, and an abrupt SIGKILL mid-write leaves a stale lock for the
    next run to reclaim. SIGTERM gives the child, and anything it
    started, a chance to release that lock; SIGKILL alone never does.
    SIGKILL is still the fallback, so a child that ignores SIGTERM
    cannot hold the group open forever.

    Returns once the child and every member of its group are gone, or
    after SIGKILL and 5 s more. The run lock is released as the caller
    returns, so it must not return while a member still runs.
    """
    _killpg(proc.pid, signal.SIGTERM)
    # One deadline covers the child's exit and the rest of the group's.
    deadline = time.monotonic() + _STOP_GRACE_S
    try:
        proc.wait(timeout=_STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    else:
        if _group_gone(proc.pid, deadline):
            return
    _killpg(proc.pid, _SIGKILL)
    _wait_after_kill(proc)


def _wait_after_kill(proc) -> None:
    """Wait up to 5 s for the child and its group to end after SIGKILL, and say so if any stay."""
    deadline = time.monotonic() + _STOP_GRACE_S
    try:
        proc.wait(timeout=_STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        log(f"  pid {proc.pid} did not die; it may still be running.")
        return
    if not _group_gone(proc.pid, deadline):
        log(f"  Processes started by pid {proc.pid} did not die; they may still be running.")


def _group_gone(pgid, deadline) -> bool:
    """Whether process group ``pgid`` has no member left, checked until the monotonic ``deadline``.

    The child leads its group (``start_new_session``), so its pid is the group id. Waiting for
    the child alone is not enough: a worker that ignores SIGTERM outlives it, and the run lock
    would be released with that worker still writing. The group stays findable until its last
    member is gone, even after the child has been reaped.

    Checks at least once. Without process groups (Windows) there is nothing to poll.
    """
    if not _HAVE_KILLPG:
        return True
    while True:
        _reap_group(pgid)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # A member we may not signal: waiting cannot end it, and neither can SIGKILL.
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _reap_group(pgid) -> None:
    """Collect the exited members of group ``pgid`` that are children of this process.

    Orphans go to init. When this process is init (a container's PID 1) or a subreaper, they
    come here instead, and an exited one stays a zombie that ``killpg`` still counts as a member
    until someone collects it. Only the group's own members match, never another child of ours.
    """
    try:
        while os.waitpid(-pgid, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def _killpg(pid, sig) -> None:
    """Send ``sig`` to the whole process group that ``pid`` leads. Silent if the group is
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
        # The child leads its own group (start_new_session), so its pid is the group id. Not
        # os.getpgid(pid): that fails once the child has been reaped, while members of the
        # group can still be running.
        os.killpg(pid, sig)
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
    if job is None and name == jobstore.DEFAULT_JOB and (
            getattr(args, "foreground", False) or in_container()):
        # Where there is no scheduler to install into, `schedule install` itself says to use
        # --foreground, and a container or CronJob pod never ran install at all. Run the
        # built-in cycle. Nothing is recorded, so `status` and `list` stay empty and a
        # failed run adds no backoff.
        job = jobstore.new_job(name, jobstore.DEFAULT_ARGV, "auto", "")
        log("No schedule record here, so running the built-in default cycle "
            f"(contextlake {' '.join(job.argv)}).")
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
    # The child gets the config this run itself was given (see child_argv), so a unit
    # installed with `--config X` runs its command with X, not with the global config.
    argv = child_argv(job.full_argv if kind == "full" else job.argv,
                      getattr(args, "config", None), getattr(args, "kb_config", None))

    try:
        with runlock.RunLock(runlock.runlock_path(config), job.name):
            log(f"Running {kind} cycle: contextlake {' '.join(argv)}")
            code = _spawn(argv, child_env(config, kind, job.name))
    except runlock.RunBusy as e:
        # Skip, never queue. A second writer is the corruption this prevents.
        log(f"Skipping this run: {e}")
        return GATED if _return_gated else 0

    try:
        jobstore.record_outcome(jobs_file, job.name, code, history.utc_now_iso())
    except jobstore.JobStoreUnreadable as e:
        # The run happened; only its bookkeeping cannot be written. Rewriting the store
        # from nothing would drop every other job, so leave it for the user to repair.
        log(f"Not recording this run's outcome: {e}")
    if code != 0:
        log(f"The scheduled run exited {code}. See the log above.")
    return code
