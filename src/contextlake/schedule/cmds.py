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

from ..logging_setup import log, report_line
from . import gates, history, recommend
from .platform.base import NO_CATCH_UP_PHRASE

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


def _adapter_for(args, job=None):
    from .platform import base

    name = getattr(args, "platform", None) or (job.platform if job else None) or base.detect()
    return base.get(name)


def exec_argv_for(name) -> list:
    """The command line the unit runs.

    ``sys.executable`` rather than a bare ``contextlake``: a unit runs with a
    different PATH from a login shell, and a venv that moved would otherwise
    fail silently forever. `status` resolves this back and reports it missing.
    """
    return [sys.executable, "-m", "contextlake", "schedule", "run", "--job", name]


def _report_installed(adapter, job, interval_s, exec_argv, on_battery, why, written,
                      headline) -> None:
    """Log the outcome of a successful install.

    Calls `render` again, which is free (`render` is pure) and is the only
    way to learn a fact `install` itself does not return: `install` returns
    the paths it wrote, not the rendered facts, and cron's `render` can
    round the interval requested down to the nearest one it can express.
    Reporting the interval requested instead of the interval installed is
    the R28 defect; this is the one place it can happen now.

    `headline` takes the interval INSTALLED, already formatted, and returns
    the caller's opening line: the job name, the command, and the adapter
    differ per call site, but everything below the headline (the reason,
    the paths written, the rounding note, the adapter's own state() notes,
    and the no-catch-up warning when state() has not already said it) is
    identical. Shared by `cmd_install`, `cmd_interval` and `cmd_reset`,
    because three separate copies is what let two of them drift.
    """
    from .. import style

    rendered = adapter.render(job, interval_s, exec_argv, on_battery=on_battery)
    actual_interval_s = rendered.get("interval_s", interval_s)
    log(headline(recommend.format_duration(actual_interval_s)))
    log(f"  {why}")
    for path in written:
        log(f"  wrote {path}")
    rounding_note = rendered.get("notes")
    if rounding_note:
        log(f"  {style.warn()} {rounding_note}")
    # The install above already succeeded and is not undone by anything
    # below. A `state()` failure here (the bus answered at detect time and
    # is gone by the time this reads it back) must degrade to a note, the
    # same way `cmd_status` treats the same call, not abort a completed
    # operation and leave the caller thinking the install failed.
    try:
        state_notes = adapter.state(job).get("notes", [])
    except Exception as e:  # noqa: BLE001 - a live read must not undo a completed install
        state_notes = []
        log(f"  {style.warn()} could not read the {adapter.id} state: {e}")
    for note in state_notes:
        log(f"  {style.warn()} {note}")
    # cron's own state() already carries this note when installed; printing
    # it again unconditionally duplicated the sentence on every cron install.
    if (not adapter.catches_up_after_sleep
            and not any(NO_CATCH_UP_PHRASE in n for n in state_notes)):
        log(f"  {style.warn()} {adapter.id} {NO_CATCH_UP_PHRASE} while this "
            f"machine was asleep or off.")


def cmd_install(args, config) -> int:
    """Measure, decide, and install. Idempotent: run it again to change the interval."""
    from .. import style
    from . import jobs as jobstore
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
        adapter = _adapter_for(args, existing)
    except base.NoAdapter as e:
        log(style.fail(str(e)))
        return 2

    interval_s, why = resolve_interval(config, interval_setting)
    job = jobstore.new_job(name, argv, interval_setting, adapter.id, full_argv=full_argv,
                           created=existing.created if existing else None)
    on_battery = config.get("schedule_on_battery", "skip")
    try:
        written = adapter.install(job, interval_s, exec_argv_for(name),
                                  on_battery=on_battery)
    except OSError as e:
        # Degrade, never fail: print the unit and say how to install it.
        # Same on_battery as the install attempt. A user who set
        # schedule_on_battery=run must not be handed a unit with
        # ConditionACPower=true, which on a read-only home is the only
        # artefact they get.
        log(style.fail(f"Could not install the {adapter.id} unit: {e}"))
        rendered = adapter.render(job, interval_s, exec_argv_for(name), on_battery=on_battery)
        for filename, text in rendered.items():
            if filename in adapter.metadata_keys:
                continue
            log(f"\n----- {filename} -----\n{text}")
        log("Install these yourself, or run `contextlake schedule run --foreground`.")
        return 0
    jobstore.write_job(jobs_file, job)

    _report_installed(
        adapter, job, interval_s, exec_argv_for(name), on_battery, why, written,
        lambda interval_str: (f"{style.ok()} Installed job {name!r} on "
                              f"{adapter.id}, every {interval_str}."))
    return 0


def resolve_interval(config, interval_setting):
    """``(seconds, one-line explanation)`` for a job's interval setting."""
    if str(interval_setting).strip().lower() != "auto":
        seconds = recommend.parse_duration(interval_setting)
        return seconds, (f"Fixed at {recommend.format_duration(seconds)}; "
                         f"auto-adjust is off for this job.")
    rec, _ = current_recommendation(config)
    return rec.interval_s, rec.reason


def executable_missing(exec_argv):
    """The interpreter path in ``exec_argv`` if it no longer exists, else None.

    A unit that references a deleted venv fails on every fire, forever, and
    nothing surfaces it: systemd logs a start failure the user never reads.
    Resolving the actual file is the check; the string being present is not.
    """
    if not exec_argv:
        return None
    candidate = str(exec_argv[0])
    return None if os.path.exists(candidate) else candidate


def cmd_status(args, config) -> int:
    """What is installed, what it will do, and every way it might be dead.

    Reads only, writes nothing: three sources (the job record, the platform
    unit, and the measured history) are read and compared, and never
    reconciled here. `install` is the only action that changes any of them.
    """
    from .. import style
    from . import jobs as jobstore

    mapping = jobstore.read_jobs(jobstore.jobs_path(config))
    only = getattr(args, "job", None)
    all_names = sorted(mapping)
    if only:
        mapping = {k: v for k, v in mapping.items() if k == only}

    rec, runs = current_recommendation(config)
    summary = history.summarize(runs)
    threshold = _float_or(config, "schedule_adjust_threshold", 0.5, low=0.0)

    payload = {"history": summary,
               "recommendation": {
                   "interval": recommend.format_duration(rec.interval_s),
                   "interval_seconds": rec.interval_s, "basis": rec.basis,
                   "measured": rec.measured, "reason": rec.reason},
               "jobs": []}

    if not mapping and not getattr(args, "json", False):
        if only and all_names:
            # Jobs exist, just not this one: saying "No schedule installed"
            # here would invite a reinstall over a schedule that is working.
            print(f"No job named {only!r}. Installed jobs: {', '.join(all_names)}.")
            return 0
        print("No schedule installed.")
        print(f"  Recommended interval: {recommend.format_duration(rec.interval_s)} "
              f"({'measured' if rec.measured else 'a default, not a measurement'})")
        print("  Install it:  contextlake schedule install")
        return 0

    require_idle = str(config.get("schedule_require_idle", "false")).strip().lower() \
        in ("true", "yes", "1")
    idle_inert = False
    if require_idle:
        try:
            idle_inert = gates.user_is_idle() is None
        except Exception:  # noqa: BLE001 - a sensor is never worth failing status over
            idle_inert = True

    for name, job in sorted(mapping.items()):
        notes = []
        # Constructing the adapter and reading its state are two separate
        # failure points. A construction failure means the catch-up property
        # is not knowable (``catches_up = None``, no claim printed either
        # way). A `state()` failure still leaves a constructed adapter, whose
        # `catches_up_after_sleep` is a class attribute, not a read of live
        # state, so it is known even when `state()` itself failed. Collapsing
        # both into one `except` and defaulting to ``False`` printed a false
        # "does not replay a run missed" for systemd, which sets
        # ``Persistent=true`` and does replay one.
        adapter_id, catches_up = job.platform or "unknown", None
        try:
            adapter = _adapter_for(args, job)
        except Exception as e:  # noqa: BLE001 - a broken adapter must not hide the record
            state = {"installed": False, "interval_s": None, "next_run": None,
                     "notes": [f"could not build the {job.platform} adapter: {e}"]}
        else:
            adapter_id, catches_up = adapter.id, adapter.catches_up_after_sleep
            try:
                state = adapter.state(job)
            except Exception as e:  # noqa: BLE001 - ditto, for a live read
                state = {"installed": False, "interval_s": None, "next_run": None,
                         "notes": [f"could not read the {job.platform} state: {e}"]}
        for note in (state.get("notes") or []):
            if note not in notes:
                notes.append(note)

        if not state.get("installed"):
            notes.append("This job is recorded but its unit is NOT installed. "
                         "Re-run `contextlake schedule install` to put it back.")
        # The interpreter the INSTALLED unit references, not the one running
        # this check: `exec_argv_for` builds the argv for a fresh install and
        # always resolves to sys.executable, so checking it here would ask
        # whether the interpreter running `status` right now exists, which is
        # never missing. `exec_path` comes from the adapter reading the unit
        # back (systemd's ExecStart, cron's crontab line), and `None` means
        # the adapter could not tell, which must not read as "missing".
        exec_path = state.get("exec_path")
        gone = executable_missing([exec_path]) if exec_path else None
        if gone:
            notes.append(f"The interpreter this job runs ({gone}) has moved or been "
                         f"deleted, so every run fails silently. Re-run "
                         f"`contextlake schedule install` from the current install.")
        # The adapter's own state() may already say this (cron's does, on every
        # installed job); matching on the phrase rather than the whole
        # sentence keeps the line from printing twice for the one backend
        # that reports both, regardless of which noun starts its sentence.
        # ``catches_up is False`` (not merely falsy): ``None`` means unknown,
        # which is a different fact than "known not to catch up".
        if catches_up is False and not any(NO_CATCH_UP_PHRASE in n for n in notes):
            notes.append(f"{adapter_id} {NO_CATCH_UP_PHRASE} while this machine "
                         f"was asleep or off.")
        if idle_inert:
            notes.append("schedule_require_idle is on, but user idleness cannot "
                         "be detected here (no login session), so the gate is "
                         "inert: this job runs whether or not you are at the "
                         "keyboard.")

        effective_s, why = resolve_interval(config, job.interval)
        live_s = state.get("interval_s")
        if (job.interval.lower() == "auto" and live_s and rec.measured
                and abs(live_s - rec.interval_s) / max(live_s, 1.0) > threshold):
            notes.append(
                f"Drift: this job runs every {recommend.format_duration(live_s)}, "
                f"but the measurements now suggest "
                f"{recommend.format_duration(rec.interval_s)}. The next run "
                f"rewrites the unit.")

        payload["jobs"].append({
            "name": name, "interval_setting": job.interval,
            "effective_interval": recommend.format_duration(effective_s), "why": why,
            "unit_interval": (recommend.format_duration(live_s) if live_s else None),
            "unit_installed": bool(state.get("installed")), "adapter": adapter_id,
            "next_run": state.get("next_run"), "command": job.argv,
            "recommendation": recommend.format_duration(rec.interval_s),
            "last_run": job.last_run, "last_exit": job.last_exit,
            "failures": job.failures, "notes": notes})

    if getattr(args, "json", False):
        print(jsonlib.dumps(payload, indent=2, sort_keys=True))
        return 0

    for entry in payload["jobs"]:
        glyph = style.ok() if (entry["unit_installed"] and not entry["notes"]) else style.warn()
        print(f"{glyph} {entry['name']}  ({entry['adapter']})")
        print(f"    runs:      contextlake {' '.join(entry['command'])}")
        print(f"    interval:  {entry['effective_interval']}"
              + (f"  (set to {entry['interval_setting']})"
                 if entry["interval_setting"] != "auto" else "  (auto)"))
        if entry["interval_setting"] != "auto":
            # `why` is `rec.reason` for an auto job, already printed once
            # below for the whole command; repeating it per job would be
            # noise. A pinned job's reason ("auto-adjust is off") is not
            # printed anywhere else, so it belongs here.
            print(f"               {entry['why']}")
        if entry["unit_interval"] and entry["unit_interval"] != entry["effective_interval"]:
            print(f"    on disk:   {entry['unit_interval']}")
        print(f"    next run:  {entry['next_run'] or 'unknown'}")
        print(f"    last run:  {entry['last_run'] or 'never'}"
              + (f"  (exit {entry['last_exit']})"
                 if entry["last_exit"] not in (0, None) else ""))
        if entry["failures"]:
            print(f"    {style.warn()} {entry['failures']} consecutive failure(s); "
                  f"the interval is backing off.")
        for note in entry["notes"]:
            print(f"    {style.warn()} {note}")

    # Built as a plain string first. A nested same-quote f-string is PEP 701,
    # which is Python 3.12; on the 3.10 and 3.11 matrix cells it is a
    # SyntaxError that takes the whole CLI down at import.
    if rec.measured:
        provenance = (f"from {rec.samples} measured run(s) over "
                      f"{summary['days']:.1f} day(s)")
    else:
        provenance = "a built-in default, not a measurement"
    print(f"\n  Recommended interval: "
          f"{recommend.format_duration(rec.interval_s)} ({provenance})")
    print(f"    {rec.reason}")
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
    from . import jobs as jobstore
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
    from . import jobs as jobstore
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
        adapter = _adapter_for(args, updated)
        written = adapter.install(updated, interval_s, exec_argv_for(name),
                                  on_battery=on_battery)
    except (base.NoAdapter, OSError) as e:
        log(style.fail(f"Could not rewrite the {updated.platform} unit: {e}"))
        return 1

    if wants_history:
        code = _discard_history(args, config)
        if code != 0:
            return code

    jobstore.write_job(jobs_file, updated)

    _report_installed(
        adapter, updated, interval_s, exec_argv_for(name), on_battery, why, written,
        lambda interval_str: f"{style.ok()} Reset job {name!r} to auto, every {interval_str}.")
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


# Anything a shell would interpret. A job argument containing one of these is
# somebody expecting a shell, and these values land in unit files that run
# unattended, so it is refused rather than quoted and hoped for.
_SHELL_CHARS = set(";|&$`><\n\r\\\"'*?()[]{}!#~")
_SAFE_GLOB = "*?[]"  # allowed, because --repos takes a glob


def parse_interval_spec(rest):
    """``["6h", "run", "kb", "wiki"]`` to ``("6h", ["kb", "wiki"])``.

    Raises ``ValueError`` with a message naming the problem, because this is
    typed by a person and the error is the whole user interface for it.
    """
    words = [str(w) for w in (rest or [])]
    if not words:
        raise ValueError(
            "missing interval. Usage: contextlake schedule interval "
            "<duration|auto> run <contextlake command...>")
    setting = words[0].strip()
    if setting.lower() != "auto":
        try:
            recommend.parse_duration(setting)
        except ValueError as e:
            raise ValueError(f"{e}. Give a duration (45s, 30m, 2h, 7d) or `auto`.") from None
        setting = setting.lower()
    else:
        setting = "auto"

    if len(words) < 2 or words[1].lower() != "run":
        raise ValueError(
            "expected `run` after the interval. Usage: contextlake schedule "
            "interval <duration|auto> run <contextlake command...>")

    argv = words[2:]
    # argparse consumes a `--` separator before this runs, so `argv` never
    # begins with one through the CLI. The strip stays for a caller that builds
    # the word list by hand, and is a no-op when there is nothing to strip.
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("missing command after `run`. Give a contextlake "
                         "command, for example: kb wiki --force")

    for word in argv:
        bad = (set(word) & _SHELL_CHARS) - set(_SAFE_GLOB)
        if bad:
            raise ValueError(
                f"{word!r} contains shell characters ({''.join(sorted(bad))}). "
                f"A scheduled job is a contextlake command and its flags, not a "
                f"shell line. Write the words separately.")
    return setting, argv


def validate_job_argv(argv):
    """Prove the job can parse AND can run. Returns a list of warnings.

    Parsing is not enough. A `kb index` job on a core-only install parses
    and then fails every night with an ImportError nobody reads, which
    is the silent-3am failure this validation exists to prevent.
    """
    from ..cli import _KB_COMMANDS, _resolve_command, build_parser

    if argv and argv[0] == "schedule":
        raise ValueError(
            "a scheduled job cannot be `schedule` itself; that would recurse.")

    parser = build_parser()
    try:
        parsed = parser.parse_args(list(argv))
    except SystemExit:
        raise ValueError(
            f"`contextlake {' '.join(argv)}` is not a valid command. Run it by "
            f"hand first, or see `contextlake --help`.") from None
    try:
        _resolve_command(parsed, parser)
    except SystemExit:
        raise ValueError(
            f"`contextlake {' '.join(argv)}` names a namespace with no command "
            f"after it.") from None

    warnings = []
    if parsed.command in _KB_COMMANDS:
        try:
            __import__("contextlake.kb")
        except ImportError as e:
            warnings.append(
                f"`{' '.join(argv)}` needs the knowledge-layer extra, which is not "
                f"installed here ({e}). Install it with "
                f"pip install 'contextlake[kb]', or this job fails on every run.")
    return warnings


def _name_from_argv(argv) -> str:
    """A readable default job name, from the command it runs."""
    from .platform.base import NAME_RE

    words = [w for w in argv if not w.startswith("-")][:2]
    candidate = "-".join(words) or "job"
    return candidate if NAME_RE.match(candidate) else "job"


def cmd_interval(args, config) -> int:
    """Create or replace an ad-hoc job."""
    from .. import style
    from . import jobs as jobstore
    from .platform import base

    try:
        setting, argv = parse_interval_spec(getattr(args, "rest", []))
        warnings = validate_job_argv(argv)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2

    name = getattr(args, "job", None) or _name_from_argv(argv)
    try:
        base.check_name(name)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2

    jobs_file = jobstore.jobs_path(config)
    existing = jobstore.read_jobs(jobs_file).get(name)
    try:
        adapter = _adapter_for(args, existing)
        interval_s, why = resolve_interval(config, setting)
    except (base.NoAdapter, ValueError) as e:
        log(style.fail(str(e)))
        return 2

    job = jobstore.new_job(name, argv, setting, adapter.id,
                           created=existing.created if existing else None)
    on_battery = config.get("schedule_on_battery", "skip")
    try:
        written = adapter.install(job, interval_s, exec_argv_for(name),
                                  on_battery=on_battery)
    except OSError as e:
        log(style.fail(f"Could not install the {adapter.id} unit: {e}"))
        return 1
    # Written only after the unit is in place, so a failed install never leaves
    # a record claiming a schedule that does not exist.
    jobstore.write_job(jobs_file, job)

    _report_installed(
        adapter, job, interval_s, exec_argv_for(name), on_battery, why, written,
        lambda interval_str: (f"{style.ok()} Job {name!r}: contextlake "
                              f"{' '.join(argv)}, every {interval_str} on "
                              f"{adapter.id}."))
    for warning in warnings:
        log(f"  {style.warn()} {warning}")
    return 0


def dispatch(args, config) -> int:
    """Route one `schedule` invocation."""
    action = args.action
    if action == "recommend":
        return cmd_recommend(args, config)
    if action == "list":
        return cmd_list(args, config)
    if action == "install":
        return cmd_install(args, config)
    if action == "uninstall":
        return cmd_uninstall(args, config)
    if action == "status":
        return cmd_status(args, config)
    if action == "run":
        return cmd_run(args, config)
    if action == "reset":
        return cmd_reset(args, config)
    if action == "interval":
        return cmd_interval(args, config)
    log(f"`schedule {action}` is not implemented yet.")
    return 1
