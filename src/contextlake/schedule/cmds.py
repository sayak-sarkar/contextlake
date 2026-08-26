"""The `contextlake schedule` actions.

Core tier. ``run`` is the only action that can reach the knowledge layer, and it
does so by spawning a subprocess, so nothing here imports ``contextlake.kb`` at
all. That is what lets `schedule recommend` and `schedule status` work on an
install without the ``[kb]`` extra.
"""
from __future__ import annotations

import json as jsonlib

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


def dispatch(args, config) -> int:
    """Route one `schedule` invocation. Actions land here in Tasks 5 to 12."""
    action = args.action
    if action == "recommend":
        return cmd_recommend(args, config)
    if action == "list":
        return cmd_list(args, config)
    log(f"`schedule {action}` is not implemented yet.")
    return 1
