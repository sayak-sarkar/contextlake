"""How often contextlake should run, from what it measured last time.

PURE. No clock, no filesystem, no config lookup, no logging. Everything this
module needs arrives as an argument, so every branch is reachable from a plain
function call and the interval maths can be tested exhaustively.

Two independent lower bounds, and the larger one wins:

    floor_duty     = median(incremental duration) / duty_cycle
    floor_activity = k / change_rate
    interval       = clamp(max(floor_duty, floor_activity), min, max)

``floor_duty`` is a cost bound: never occupy more than ``duty_cycle`` of
wall-clock time. ``floor_activity`` is a freshness bound: there is no point
running more often than the fleet produces roughly ``k`` changed repositories.
"""
from __future__ import annotations

import re
import statistics
from collections import namedtuple
from datetime import datetime, timezone

# No history means no measurement. Six hours is deliberately conservative: it
# is cheap on a big fleet and merely unambitious on a small one, and the first
# real run replaces it. Every surface that prints it must say it is a default.
COLD_START_S = 6 * 3600.0

DEFAULT_SETTINGS = {
    "duty_cycle": 0.10,     # max share of wall-clock time contextlake may occupy
    "min_s": 3600.0,        # 1h
    "max_s": 86400.0,       # 24h
    "k": 1.0,               # repositories of change worth waking up for
    "fixed_s": None,        # a pin, which disables auto-adjust entirely
}

_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)

Recommendation = namedtuple(
    "Recommendation",
    "interval_s basis reason floor_duty_s floor_activity_s clamped measured samples")


def parse_duration(text) -> float:
    """``"30m"`` to ``1800.0``. A bare number is seconds.

    Raises ``ValueError`` on anything else, including zero and negatives: an
    interval of zero is a busy loop, and there is no reading of "-5m" that is
    what somebody meant. ``"auto"`` is NOT handled here; it is a mode, and the
    caller decides that before reaching for a duration.
    """
    match = _DURATION_RE.match(text or "") if isinstance(text, str) else None
    if not match:
        raise ValueError(f"not a duration: {text!r} (try 45s, 30m, 2h, 7d)")
    value = float(match.group(1)) * _UNITS[(match.group(2) or "s").lower()]
    if value <= 0:
        raise ValueError(f"interval must be positive: {text!r}")
    return value


def format_duration(seconds) -> str:
    """The shortest exact unit, so a rendered unit file and a status line agree.

    Exact only: 4200s is "70m", never "1.2h". A rounded string in a unit file
    would install a different interval from the one that was computed.
    """
    value = max(1, int(round(float(seconds))))
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if value % size == 0:
            return f"{value // size}{suffix}"
    return f"{value}s"


def _parse_ts(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _scoreable(runs):
    """Incremental runs that succeeded.

    Full rebuilds are excluded because they measure different work; the spec
    keeps them apart with ``kind`` for exactly this. Failed runs are excluded
    because a crash after three seconds measures nothing about how long the
    work takes, and averaging it in would SHRINK the interval on the strength
    of a failure.
    """
    return [r for r in runs
            if r.get("kind") == "incremental" and r.get("exit") == 0
            and isinstance(r.get("duration_s"), (int, float))]


def change_rate_per_hour(runs):
    """Repositories changed per hour, or ``None`` when it cannot be measured.

    ``None`` and ``0.0`` are different answers and must not be conflated.
    ``None`` means "no data" (fewer than two timestamped records, no span
    between them, or no record carrying ``repos_changed`` at all, which is
    every record written before the activity plumbing existed). ``0.0`` means
    "measured, and nothing changed", which legitimately widens the interval to
    the maximum.
    """
    scored = [r for r in runs if isinstance(r.get("repos_changed"), (int, float))]
    stamps = [(_parse_ts(r.get("ts")), r) for r in scored]
    stamps = [(t, r) for t, r in stamps if t is not None]
    if len(stamps) < 2:
        return None
    stamps.sort(key=lambda pair: pair[0])
    span_h = (stamps[-1][0] - stamps[0][0]).total_seconds() / 3600.0
    if span_h <= 0:
        return None
    # The first record's changes were counted before the window opened, so they
    # are not evidence about the rate INSIDE it.
    changed = sum(float(r.get("repos_changed") or 0) for _, r in stamps[1:])
    return changed / span_h


def recommend(runs, settings=None) -> Recommendation:
    """The interval, and a sentence saying how it was reached."""
    cfg = dict(DEFAULT_SETTINGS)
    cfg.update(settings or {})

    fixed = cfg.get("fixed_s")
    if fixed:
        # Deliberately NOT clamped. The bounds exist to keep auto-adjust sane;
        # an explicit pin is the user overriding the recommender, and quietly
        # moving their number would make `status` a lie.
        return Recommendation(
            interval_s=float(fixed), basis="fixed",
            reason=f"fixed at {format_duration(fixed)} by configuration; "
                   f"auto-adjust is off",
            floor_duty_s=None, floor_activity_s=None, clamped=None,
            measured=False, samples=len(runs))

    scoreable = _scoreable(runs)
    if not scoreable:
        return Recommendation(
            interval_s=COLD_START_S, basis="cold-start",
            reason=f"no measured runs yet, so this is the built-in default of "
                   f"{format_duration(COLD_START_S)}, not a measurement. The "
                   f"first completed run replaces it.",
            floor_duty_s=None, floor_activity_s=None, clamped=None,
            measured=False, samples=0)

    duty = max(0.001, min(1.0, float(cfg["duty_cycle"])))
    median_s = statistics.median(float(r["duration_s"]) for r in scoreable)
    floor_duty = median_s / duty

    rate = change_rate_per_hour(runs)
    if rate is None:
        floor_activity = None
    elif rate <= 0:
        # Measured, and nothing moved. Infinity is the honest floor; the clamp
        # below turns it into schedule_max without ever dividing by zero.
        floor_activity = float("inf")
    else:
        floor_activity = (float(cfg["k"]) / rate) * 3600.0

    if floor_activity is not None and floor_activity > floor_duty:
        raw, basis = floor_activity, "activity"
    else:
        raw, basis = floor_duty, "duty"

    low, high = float(cfg["min_s"]), float(cfg["max_s"])
    if raw < low:
        interval, clamped = low, "min"
    elif raw > high:
        interval, clamped = high, "max"
    else:
        interval, clamped = raw, None

    if basis == "duty":
        why = (f"duty cycle: the median incremental run takes "
               f"{format_duration(median_s)}, and at {duty:.0%} of wall-clock "
               f"time that needs {format_duration(floor_duty)} between runs")
    else:
        why = (f"activity: the fleet changes about {rate:.2f} repo(s) per hour, "
               f"so {format_duration(floor_activity)} passes before roughly "
               f"{cfg['k']:g} repo(s) move"
               if floor_activity != float("inf")
               else "activity: nothing has changed in the measured window")
    if clamped:
        why += f", clamped to the schedule_{clamped} bound of {format_duration(interval)}"

    return Recommendation(
        interval_s=interval, basis=basis, reason=why,
        floor_duty_s=floor_duty, floor_activity_s=floor_activity,
        clamped=clamped, measured=True, samples=len(scoreable))


def backoff_interval(base_s, failures, max_s) -> float:
    """Exponential backoff after consecutive failures, capped.

    ``failures`` is capped before it reaches the shift so a long outage cannot
    produce a number too large to represent. Reset to zero on the first success.
    """
    steps = max(0, min(int(failures or 0), 40))
    return min(float(base_s) * (2 ** steps), float(max_s))
