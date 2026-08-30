"""The schedule commands that change something.

Split out of ``cmds.py``. ``install``, ``uninstall`` and ``reset`` write a job
record, write or remove a platform unit, and can retire the measurement store.
They are apart from the read-only commands for that reason.

``_discard_history`` is the destructive one. It retires measurements that take
days of real runs to earn, and it is gated by ``_confirm``, which refuses on a
non-TTY. A subagent destroyed the real store through this path on 2026-08-27
by passing ``--purge --yes``, so the guard is load-bearing and has its own
break-test.

Core tier. Nothing here may import ``contextlake.kb`` at module level, which
``tests/test_schedule_source_tier.py`` enforces.
"""
from __future__ import annotations

import sys

from ..logging_setup import log, report_line
from . import adapters, history, recommend
from . import jobs as jobstore
from .settings import resolve_interval


def cmd_install(args, config) -> int:
    """Measure, decide, and install. Idempotent: run it again to change the interval."""
    from .. import style
    from .platform import base

    name = getattr(args, "job", None) or jobstore.DEFAULT_JOB
    try:
        base.check_name(name)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2

    pin = getattr(args, "interval", None)
    interval_setting = "auto"
    if pin and str(pin).strip().lower() != "auto":
        try:
            recommend.parse_duration(pin)
        except ValueError as e:
            log(style.fail(str(e)))
            return 2
        interval_setting = str(pin).strip()

    jobs_file = jobstore.jobs_path(config)
    existing = jobstore.read_jobs(jobs_file).get(name)
    argv = existing.argv if existing else list(jobstore.DEFAULT_ARGV)
    full_argv = existing.full_argv if existing else list(jobstore.DEFAULT_FULL_ARGV)

    try:
        adapter = adapters._adapter_for(args, existing)
    except base.NoAdapter as e:
        log(style.fail(str(e)))
        return 2

    interval_s, why = resolve_interval(config, interval_setting)
    job = jobstore.new_job(name, argv, interval_setting, adapter.id, full_argv=full_argv,
                           created=existing.created if existing else None)
    on_battery = config.get("schedule_on_battery", "skip")
    try:
        written = adapter.install(job, interval_s, adapters.exec_argv_for(name),
                                  on_battery=on_battery)
    except OSError as e:
        # Degrade, never fail: print the unit and say how to install it.
        # Same on_battery as the install attempt. A user who set
        # schedule_on_battery=run must not be handed a unit with
        # ConditionACPower=true, which on a read-only home is the only
        # artefact they get.
        log(style.fail(f"Could not install the {adapter.id} unit: {e}"))
        rendered = adapter.render(job, interval_s, adapters.exec_argv_for(name),
                                  on_battery=on_battery)
        for filename, text in rendered.items():
            if filename in adapter.metadata_keys:
                continue
            log(f"\n----- {filename} -----\n{text}")
        log("Install these yourself, or run `contextlake schedule run --foreground`.")
        return 0
    jobstore.write_job(jobs_file, job)

    adapters._report_installed(
        adapter, job, interval_s, adapters.exec_argv_for(name), on_battery, why, written,
        lambda interval_str: (f"{style.ok()} Installed job {name!r} on "
                              f"{adapter.id}, every {interval_str}."))
    return 0


def _confirm(args, prompt) -> bool:
    """Whether the caller may proceed with a destructive action.

    ``--yes`` (or ``-y``) skips the prompt for a script or a CI run. Without a
    terminal to ask on, the safe default is to refuse and say why, not to
    guess: an unattended `--yes` is the only way this runs with nobody
    watching, so refusing to guess is what keeps a typo from discarding
    history nobody meant to touch.
    """
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        log(f"{prompt} Skipped: no terminal to confirm on, and --yes was not given.")
        return False
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def _discard_history(args, config) -> int:
    """Throw away the measured run history. Shared by ``uninstall --purge``
    and ``reset --history``.

    A useful median takes days of real runs to earn back, so the count and
    the span it covers are printed before anything is deleted, whether or
    not ``--yes`` is set. ``report_line`` rather than ``log``: this action
    must land in ``--log-file`` too, not only the console, and plain
    ``print`` would miss that entirely.
    ``report_line`` keeps the console half a caller's own ``print`` would have
    been, so ``capsys`` still sees it. It does not route through ``log``,
    whose output is not reliably readable back through ``capsys`` once
    anything earlier in the same pytest session has called it (see
    ``tests/test_schedule_run.py``'s ``_log_lines`` for the mechanism).

    Declining returns 1, not 0: nothing was destroyed, but the caller asked
    for a discard and did not get one, which a caller composing this into a
    script needs to be able to tell apart from a real success.
    """
    from .. import style

    path = history.history_path(config)
    summary = history.summarize(history.read_runs(path))
    if summary["count"] == 0:
        report_line("No measured runs to discard.")
        return 0
    report_line(f"About to discard {summary['count']} measured run(s) spanning "
               f"{summary['days']:.1f} day(s) ({summary['first_ts']} to "
               f"{summary['last_ts']}).")
    report_line("  The recommender starts cold and re-learns from the next run.")
    if not _confirm(args, "Discard them?"):
        report_line("Kept. Nothing was deleted.")
        return 1
    dropped = history.clear_runs(path)
    report_line(f"{style.ok()} Discarded {dropped} measured run(s).")
    report_line(f"  Saved to {path}{history.DISCARDED_SUFFIX} in case you want it back.")
    return 0


def cmd_uninstall(args, config) -> int:
    from .. import style
    from .platform import base

    jobs_file = jobstore.jobs_path(config)
    mapping = jobstore.read_jobs(jobs_file)
    if getattr(args, "all", False):
        targets = list(mapping)
    else:
        targets = [getattr(args, "job", None) or jobstore.DEFAULT_JOB]

    if not targets:
        log("No scheduled jobs to remove.")
        return 0

    missing = [t for t in targets if t not in mapping]
    for name in missing:
        log(f"No job named {name!r}. See `contextlake schedule list`.")
    if missing and not getattr(args, "all", False):
        return 1

    for name in targets:
        job = mapping[name]
        try:
            adapter = base.get(job.platform or base.detect())
            removed = adapter.uninstall(job)
        except (base.NoAdapter, OSError) as e:
            log(f"{style.warn()} Could not remove the {job.platform} unit for "
                f"{name!r}: {e}. Removing the job record anyway.")
            removed = []
        jobstore.delete_job(jobs_file, name)
        log(f"{style.ok()} Removed job {name!r}.")
        for path in removed:
            log(f"  removed {path}")

    if getattr(args, "purge", False):
        return _discard_history(args, config)
    log("  The measured run history was kept, so a future install starts warm. "
        "Use --purge to discard it.")
    return 0


def cmd_reset(args, config) -> int:
    """Back to auto: clear a fixed pin, clear the backoff, recompute, reinstall.

    ``--history`` additionally throws away the measurements, which is a
    separate and more destructive action, so it is a separate flag rather
    than part of the default behaviour.

    Install before discarding. Both orders lose something on a partial
    failure, and this one loses less: a failed install leaves the
    measurements intact and the job record untouched, which is a clean
    no-op the caller can retry. Discarding first and then failing to install
    would destroy days of accumulated runs for a reset that did not happen.

    The job record is written last, after both the install and the discard
    have gone through: if the adapter cannot install, or the discard is
    declined, this returns without touching the record, so a failed or
    aborted reset never leaves the job claiming ``auto`` with a cleared
    backoff while the installed unit still runs the old pinned, backed-off
    interval. A discard decline therefore still aborts the whole reset,
    exactly as before the reorder: ``_discard_history`` returning non-zero
    short-circuits ahead of ``write_job``.
    """
    from .. import style
    from .platform import base

    wants_history = bool(getattr(args, "history", False))
    jobs_file = jobstore.jobs_path(config)
    mapping = jobstore.read_jobs(jobs_file)
    name = getattr(args, "job", None) or jobstore.DEFAULT_JOB
    job = mapping.get(name)

    if job is None:
        if getattr(args, "job", None):
            log(f"No job named {name!r}. See `contextlake schedule list`.")
            return 1
        if wants_history:
            # The measurements describe the machine, not a job, so this is
            # legitimate with nothing installed.
            return _discard_history(args, config)
        log("No schedule installed. Nothing to reset.")
        return 1

    updated = job._replace(interval="auto", failures=0)
    interval_s, why = resolve_interval(config, "auto")
    on_battery = config.get("schedule_on_battery", "skip")
    try:
        adapter = adapters._adapter_for(args, updated)
        written = adapter.install(updated, interval_s, adapters.exec_argv_for(name),
                                  on_battery=on_battery)
    except (base.NoAdapter, OSError) as e:
        log(style.fail(f"Could not rewrite the {updated.platform} unit: {e}"))
        return 1

    if wants_history:
        code = _discard_history(args, config)
        if code != 0:
            return code

    jobstore.write_job(jobs_file, updated)

    adapters._report_installed(
        adapter, updated, interval_s, adapters.exec_argv_for(name), on_battery, why, written,
        lambda interval_str: f"{style.ok()} Reset job {name!r} to auto, every {interval_str}.")
    return 0
