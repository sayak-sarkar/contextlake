"""The interval maths, exhaustively.

Pure functions, so every branch is a plain call: no tmp_path, no monkeypatch,
no clock. If a branch here is untested it is because nobody wrote the test, not
because it was hard to reach.
"""
from __future__ import annotations

import pytest

from contextlake.schedule import recommend as R

HOUR = 3600.0


def _settings(**kw):
    s = dict(R.DEFAULT_SETTINGS)
    s.update(kw)
    return s


def _runs(specs):
    """specs: list of (hours_ago, duration_s, repos_changed). Newest last."""
    out = []
    for hours_ago, duration, changed in specs:
        total_h = 100 - hours_ago
        out.append({"ts": f"2026-08-{1 + total_h // 24:02d}T{total_h % 24:02d}:00:00Z",
                    "kind": "incremental", "duration_s": float(duration), "exit": 0,
                    "repos_total": 480, "repos_changed": changed})
    return out


# ---- duration parsing --------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("45s", 45.0), ("30m", 1800.0), ("2h", 7200.0), ("7d", 604800.0),
    ("90", 90.0), ("1H", 3600.0), (" 2h ", 7200.0), ("1.5h", 5400.0),
])
def test_parse_duration_accepts_the_documented_forms(text, seconds):
    assert R.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "auto", "h", "2y", "-5m", "0", "0s", "two hours", None])
def test_parse_duration_refuses_junk_and_non_positive(text):
    with pytest.raises(ValueError):
        R.parse_duration(text)


@pytest.mark.parametrize("seconds,text", [
    (45, "45s"), (1800, "30m"), (7200, "2h"), (604800, "7d"),
    (4200, "70m"), (0.5, "1s"),
])
def test_format_duration_is_the_shortest_exact_unit(seconds, text):
    assert R.format_duration(seconds) == text


def test_format_duration_round_trips_through_parse():
    for seconds in (60, 600, 3600, 4200, 86400, 259200):
        assert R.parse_duration(R.format_duration(seconds)) == float(seconds)


# ---- the two bounds ----------------------------------------------------

def test_duty_cycle_wins_on_a_busy_fleet():
    """Spec's worked row 1: 7 min incremental, ~3 repos/h. Duty 70m beats
    activity 20m."""
    runs = _runs([(3, 420, 3), (2, 420, 3), (1, 420, 3)])
    rec = R.recommend(runs, _settings(duty_cycle=0.10, k=1.0))
    assert rec.basis == "duty"
    assert rec.interval_s == pytest.approx(4200.0)
    assert rec.measured is True


def test_activity_wins_on_a_quiet_fleet():
    """Spec's worked row 2: same 7 min run, ~0.1 repos/h. Activity 10h beats
    duty 70m."""
    runs = _runs([(20, 420, 1), (10, 420, 1), (0, 420, 0)])
    rec = R.recommend(runs, _settings(duty_cycle=0.10, k=1.0, max_s=24 * HOUR))
    assert rec.basis == "activity"
    assert rec.interval_s > 4200.0


def test_duty_cycle_wins_on_a_big_slow_fleet():
    """Spec's worked row 3: 40 min incremental, 3 repos/h. Duty 6.7h wins."""
    runs = _runs([(3, 2400, 3), (2, 2400, 3), (1, 2400, 3)])
    rec = R.recommend(runs, _settings(duty_cycle=0.10, k=1.0, max_s=24 * HOUR))
    assert rec.basis == "duty"
    assert rec.interval_s == pytest.approx(24000.0)


# ---- clamping ----------------------------------------------------------

def test_clamped_to_min_when_both_floors_are_tiny():
    runs = _runs([(3, 1, 500), (2, 1, 500), (1, 1, 500)])
    rec = R.recommend(runs, _settings(min_s=HOUR))
    assert rec.interval_s == HOUR
    assert rec.clamped == "min"


def test_clamped_to_max_when_a_floor_is_enormous():
    runs = _runs([(3, 90000, 3), (2, 90000, 3), (1, 90000, 3)])
    rec = R.recommend(runs, _settings(max_s=24 * HOUR))
    assert rec.interval_s == 24 * HOUR
    assert rec.clamped == "max"


# ---- the awkward cases -------------------------------------------------

def test_no_history_is_a_declared_cold_start_not_a_measurement():
    rec = R.recommend([], _settings())
    assert rec.basis == "cold-start"
    assert rec.interval_s == R.COLD_START_S
    assert rec.measured is False
    assert rec.samples == 0
    assert "default" in rec.reason.lower()


def test_a_single_run_still_gives_a_duty_floor():
    """One duration is enough for floor_duty. It is not enough for a change
    rate, which needs a span between two timestamps."""
    rec = R.recommend(_runs([(0, 420, 5)]), _settings())
    assert rec.measured is True
    assert rec.floor_duty_s == pytest.approx(4200.0)
    assert rec.floor_activity_s is None


def test_a_zero_change_rate_clamps_to_max_and_never_divides_by_zero():
    runs = _runs([(48, 420, 0), (24, 420, 0), (0, 420, 0)])
    rec = R.recommend(runs, _settings(max_s=24 * HOUR))
    assert rec.interval_s == 24 * HOUR
    assert rec.floor_activity_s == float("inf")


def test_the_median_absorbs_one_pathological_run():
    """A PARSER_VERSION bump forces a full re-parse. If that leaked into the
    incremental estimate as a mean, it would widen the interval for days."""
    normal = [(5, 420, 3), (4, 420, 3), (3, 420, 3), (2, 420, 3)]
    rec_clean = R.recommend(_runs(normal), _settings())
    rec_spiked = R.recommend(_runs(normal + [(1, 36000, 3)]), _settings())
    assert rec_spiked.interval_s == pytest.approx(rec_clean.interval_s, rel=0.15)


def test_full_runs_are_excluded_from_the_incremental_estimate():
    runs = _runs([(3, 420, 3), (2, 420, 3), (1, 420, 3)])
    runs.append({"ts": "2026-08-26T00:00:00Z", "kind": "full", "duration_s": 36000.0,
                 "exit": 0, "repos_total": 480, "repos_changed": 480})
    rec = R.recommend(runs, _settings())
    assert rec.floor_duty_s == pytest.approx(4200.0)


def test_failed_runs_are_excluded_from_the_duration_estimate():
    """A run that died after 3 seconds measures nothing about how long the work
    takes. Averaging it in would shrink the interval on the strength of a crash."""
    runs = _runs([(2, 420, 3)])
    runs.append({"ts": "2026-08-26T00:00:00Z", "kind": "incremental", "duration_s": 3.0,
                 "exit": 1, "repos_total": 480, "repos_changed": 0})
    rec = R.recommend(runs, _settings())
    assert rec.floor_duty_s == pytest.approx(4200.0)


def test_records_without_repos_changed_do_not_fake_a_change_rate():
    """Absence is not zero. A run from before the activity plumbing existed
    must not read as 'nothing changed'."""
    runs = [{"ts": "2026-08-25T00:00:00Z", "kind": "incremental", "duration_s": 420.0, "exit": 0},
            {"ts": "2026-08-26T00:00:00Z", "kind": "incremental", "duration_s": 420.0, "exit": 0}]
    assert R.change_rate_per_hour(runs) is None
    rec = R.recommend(runs, _settings())
    assert rec.floor_activity_s is None
    assert rec.basis == "duty"


# ---- the fixed-interval override ---------------------------------------

def test_a_fixed_interval_overrides_every_measurement():
    runs = _runs([(3, 420, 3), (2, 420, 3), (1, 420, 3)])
    rec = R.recommend(runs, _settings(fixed_s=2 * HOUR))
    assert rec.basis == "fixed"
    assert rec.interval_s == 2 * HOUR


def test_a_fixed_interval_is_not_clamped_by_min_or_max():
    """The bounds exist to keep AUTO-adjust sane. An explicit pin is the user
    saying they know better, and silently moving it would be a lie."""
    rec = R.recommend([], _settings(fixed_s=30.0, min_s=HOUR, max_s=24 * HOUR))
    assert rec.interval_s == 30.0
    assert rec.clamped is None


# ---- backoff -----------------------------------------------------------

@pytest.mark.parametrize("failures,expected", [
    (0, 3600.0), (1, 7200.0), (2, 14400.0), (3, 28800.0),
])
def test_backoff_doubles_per_consecutive_failure(failures, expected):
    assert R.backoff_interval(3600.0, failures, 24 * HOUR) == expected


def test_backoff_is_capped_and_cannot_overflow():
    assert R.backoff_interval(3600.0, 999, 24 * HOUR) == 24 * HOUR


def test_change_rate_needs_a_real_span():
    """Two records with the same timestamp span zero hours. Dividing by that
    span is the same divide-by-zero the change rate itself guards."""
    same = [{"ts": "2026-08-26T00:00:00Z", "kind": "incremental", "duration_s": 1.0,
             "exit": 0, "repos_changed": 5, "repos_total": 10} for _ in range(2)]
    assert R.change_rate_per_hour(same) is None


def test_the_reason_names_the_winning_bound():
    """`schedule recommend` prints this string. If it does not say which bound
    won, the user cannot tell a duty-capped interval from a quiet fleet."""
    runs = _runs([(3, 420, 3), (2, 420, 3), (1, 420, 3)])
    reason = R.recommend(runs, _settings()).reason
    assert "duty" in reason.lower()
    assert "70m" in reason
