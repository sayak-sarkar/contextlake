"""Reading the schedule settings, and turning them into an interval.

Split out of ``cmds.py``: five commands consult these and none of them mutate
anything, so they belong together and away from the code that installs units.

Core tier. Nothing here may import ``contextlake.kb`` at module level, which
``tests/test_schedule_source_tier.py`` enforces.
"""
from __future__ import annotations

from ..logging_setup import log
from . import history, recommend


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


def current_recommendation(config, job=None):
    """``(Recommendation, runs)`` for this workspace. The one place that pairs
    the stored history with the configured settings, so `recommend`, `status`,
    `install` and `run` can never disagree about the number.

    ``job`` scopes the history to one job's records. Pass it wherever a job is
    in hand: durations differ per job, so an unscoped median mixes a two-minute
    `kb index` with a forty-minute `bootstrap` and recommends an interval that
    fits neither. Left as ``None`` the reading covers every job, which is what
    `recommend` and `status` report, because they describe the schedule rather
    than one job in it.
    """
    runs = history.read_runs(history.history_path(config))
    if job is not None:
        runs = history.for_job(runs, job)
    return recommend.recommend(runs, settings_from_config(config)), runs


def resolve_interval(config, interval_setting):
    """``(seconds, one-line explanation)`` for a job's interval setting."""
    if str(interval_setting).strip().lower() != "auto":
        seconds = recommend.parse_duration(interval_setting)
        return seconds, (f"Fixed at {recommend.format_duration(seconds)}; "
                         f"auto-adjust is off for this job.")
    rec, _ = current_recommendation(config)
    return rec.interval_s, rec.reason
