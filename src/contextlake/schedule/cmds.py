"""The `contextlake schedule` actions.

Core tier. ``run`` is the only action that can reach the knowledge layer, and it
does so by spawning a subprocess, so nothing here imports ``contextlake.kb`` at
all. That is what lets `schedule recommend` and `schedule status` work on an
install without the ``[kb]`` extra.
"""
from __future__ import annotations

import json as jsonlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from ..logging_setup import log
from . import history, recommend

ACTIONS = ("recommend", "install", "uninstall", "status", "run", "list", "reset", "interval")


def _float_or(config, key, default, *, low=None, high=None, exclusive_high=False):
    """One config value as a float, or the default with a warning.

    A typo in one INI key must not stop the scheduler. Falling back and saying
    so is strictly better than refusing to run. ``exclusive_high`` rejects a
    value equal to ``high`` too, for a bound where the edge itself is invalid
    (a duty cycle of 1.0 means "run continuously", not merely "on the high
    side").
    """
    raw = str(config.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log(f"WARNING: {key}={raw!r} is not a number; using {default}")
        return default
    above_high = high is not None and (value >= high if exclusive_high else value > high)
    if (low is not None and value < low) or above_high:
        log(f"WARNING: {key}={raw!r} is outside the usable range; using {default}")
        return default
    return value


def _duration_or(config, key, default):
    raw = str(config.get(key, "")).strip()
    if not raw:
        return default
    try:
        return recommend.parse_duration(raw)
    except ValueError as e:
        log(f"WARNING: {key}: {e}; using {recommend.format_duration(default)}")
        return default


def settings_from_config(config) -> dict:
    """The settings dict :func:`recommend.recommend` takes, read from the INI.

    Every value falls back to the built-in default with a warning rather than
    raising, because this is read on a scheduled run at 3am where there is
    nobody to fix a typo.
    """
    settings = dict(recommend.DEFAULT_SETTINGS)
    # Exclusive at both ends: 0 is a divide-by-zero and 1.0 means "run
    # continuously", neither of which is a duty cycle anybody wants by accident.
    settings["duty_cycle"] = _float_or(config, "schedule_duty_cycle", 0.10,
                                       low=0.0001, high=1.0, exclusive_high=True)
    if settings["duty_cycle"] <= 0:
        settings["duty_cycle"] = 0.10
    settings["min_s"] = _duration_or(config, "schedule_min", 3600.0)
    settings["max_s"] = _duration_or(config, "schedule_max", 86400.0)
    if settings["min_s"] > settings["max_s"]:
        log(f"WARNING: schedule_min ({recommend.format_duration(settings['min_s'])}) is "
            f"above schedule_max ({recommend.format_duration(settings['max_s'])}); "
            f"using the defaults for both")
        settings["min_s"], settings["max_s"] = 3600.0, 86400.0

    raw = str(config.get("schedule_interval", "auto")).strip().lower()
    if raw in ("", "auto"):
        settings["fixed_s"] = None
    else:
        try:
            settings["fixed_s"] = recommend.parse_duration(raw)
        except ValueError as e:
            log(f"WARNING: schedule_interval: {e}; falling back to auto")
            settings["fixed_s"] = None
    return settings


def current_recommendation(config):
    """``(Recommendation, runs)`` for this workspace. The one place that pairs
    the stored history with the configured settings, so `recommend`, `status`,
    `install` and `run` can never disagree about the number."""
    runs = history.read_runs(history.history_path(config))
    return recommend.recommend(runs, settings_from_config(config)), runs


def cmd_recommend(args, config) -> int:
    """Print the interval and why, and change nothing."""
    rec, runs = current_recommendation(config)
    summary = history.summarize(runs)
    if getattr(args, "json", False):
        print(jsonlib.dumps({
            "interval": recommend.format_duration(rec.interval_s),
            "interval_seconds": rec.interval_s,
            "basis": rec.basis, "reason": rec.reason, "measured": rec.measured,
            "samples": rec.samples, "clamped": rec.clamped,
            "floor_duty_seconds": rec.floor_duty_s,
            "floor_activity_seconds": (None if rec.floor_activity_s in (None, float("inf"))
                                       else rec.floor_activity_s),
            "history": summary,
        }, indent=2, sort_keys=True))
        return 0
    from .. import style

    print(f"{style.ok() if rec.measured else style.warn()} "
          f"Recommended interval: {recommend.format_duration(rec.interval_s)}")
    print(f"  Because: {rec.reason}")
    if rec.measured:
        print(f"  From {rec.samples} measured run(s) over {summary['days']:.1f} day(s)")
        if rec.floor_duty_s is not None:
            print(f"    duty-cycle floor: {recommend.format_duration(rec.floor_duty_s)}")
        if rec.floor_activity_s is not None:
            print("    activity floor:   "
                  + ("no change measured"
                     if rec.floor_activity_s == float("inf")
                     else recommend.format_duration(rec.floor_activity_s)))
    else:
        print("  Nothing has been measured yet. Run `contextlake mirror sync` or "
              "`contextlake bootstrap` once, or install the schedule and let the "
              "first run replace this default.")
    print("\n  Install it:  contextlake schedule install")
    return 0


def cmd_list(args, config) -> int:
    """Every job this tool installed. Reads only."""
    from . import jobs as jobstore

    path = jobstore.jobs_path(config)
    mapping = jobstore.read_jobs(path)
    if getattr(args, "json", False):
        print(jsonlib.dumps({name: job._asdict() for name, job in sorted(mapping.items())},
                            indent=2, sort_keys=True))
        return 0
    if not mapping:
        print("No scheduled jobs. Create one with `contextlake schedule install`.")
        return 0
    width = max(3, max(len(name) for name in mapping))
    print(f"{'JOB'.ljust(width)}  INTERVAL  ADAPTER   LAST RUN              COMMAND")
    for name, job in sorted(mapping.items()):
        last = job.last_run or "never"
        mark = "" if job.last_exit in (0, None) else f" (exit {job.last_exit})"
        print(f"{name.ljust(width)}  {job.interval:<8}  {job.platform:<8}  "
              f"{last:<20}  contextlake {' '.join(job.argv)}{mark}")
    return 0


def child_env(config, kind) -> dict:
    """The environment a scheduled child runs in.

    Two variables carry everything the child needs to record itself, so it does
    not have to re-derive the history location from a config it may never load
    (a `kb index` job never reads the mirror INI).
    """
    from ..cli import ENV_HISTORY, ENV_KIND

    env = dict(os.environ)
    env[ENV_HISTORY] = history.history_path(config)
    env[ENV_KIND] = kind
    return env


def _spawn(argv, env, timeout=None) -> int:
    """Run one contextlake command as a child. Returns its exit code.

    ``sys.executable -m contextlake`` rather than a bare ``contextlake``: the
    unit may run with a different PATH, and a venv that moved would otherwise
    fail silently forever.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "contextlake"] + list(argv),
            env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        log(f"The scheduled run exceeded its timeout of {timeout}s and was killed.")
        return 124
    except OSError as e:
        log(f"Could not start the scheduled run: {e}")
        return 127
    return completed.returncode


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
            rec, _ = current_recommendation(config)
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

    runs = history.read_runs(history.history_path(config))
    kind = decide_kind(runs, _duration_or(config, "schedule_full_every", 7 * 86400.0))
    argv = job.full_argv if kind == "full" else job.argv

    try:
        with runlock.RunLock(runlock.runlock_path(config), job.name):
            log(f"Running {kind} cycle: contextlake {' '.join(argv)}")
            code = _spawn(argv, child_env(config, kind))
    except runlock.RunBusy as e:
        # Skip, never queue. A second writer is the corruption this prevents.
        log(f"Skipping this run: {e}")
        return GATED if _return_gated else 0

    jobstore.record_outcome(jobs_file, job.name, code, history.utc_now_iso())
    if code != 0:
        log(f"The scheduled run exited {code}. See the log above.")
    return code


def dispatch(args, config) -> int:
    """Route one `schedule` invocation. Actions land here in Tasks 5 to 12."""
    action = args.action
    if action == "recommend":
        return cmd_recommend(args, config)
    if action == "list":
        return cmd_list(args, config)
    if action == "run":
        return cmd_run(args, config)
    log(f"`schedule {action}` is not implemented yet.")
    return 1
