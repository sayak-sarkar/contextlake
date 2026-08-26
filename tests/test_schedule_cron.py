"""The cron adapter: expressible intervals, and never damaging a crontab."""
from __future__ import annotations

import pytest

from contextlake.schedule import jobs
from contextlake.schedule.platform import cron


def _job(name="default"):
    return jobs.new_job(name, ["version"], "auto", "cron")


# ---- what cron can express -------------------------------------

@pytest.mark.parametrize("seconds,expected_s,spec", [
    (300, 300, "*/5 * * * *"),
    (900, 900, "*/15 * * * *"),
    (1800, 1800, "*/30 * * * *"),
    (3600, 3600, "0 * * * *"),
    (7200, 7200, "0 */2 * * *"),
    (86400, 86400, "0 0 * * *"),
])
def test_an_expressible_interval_is_kept_exactly(seconds, expected_s, spec):
    assert cron.nearest_expressible(seconds) == (expected_s, spec)


@pytest.mark.parametrize("seconds,expected_s", [
    (4200, 3600),    # 70m has no cron spelling; 60m is the nearest below
    (5400, 3600),    # 90m likewise
    (100, 60),
])
def test_an_inexpressible_interval_rounds_DOWN_and_reports_it(seconds, expected_s):
    """Down, never up. Rounding 70m up to 2h would silently halve the
    freshness the user asked for; rounding down runs more often, which
    costs duty cycle but never staleness. The caller PRINTS the difference."""
    got, _ = cron.nearest_expressible(seconds)
    assert got == expected_s
    assert got <= seconds


def test_the_installer_says_when_it_had_to_round():
    adapter = cron.CronAdapter()
    files = adapter.render(_job(), 4200.0, ["/opt/py", "-m", "contextlake"])
    assert "70m" in files["notes"]
    assert "1h" in files["notes"]


@pytest.mark.parametrize("seconds,expected_s", [
    (30, 60),    # below cron's floor: rounds UP, there is nothing smaller
    (45, 60),
    (90, 60),    # above the floor: rounds DOWN
    (4200, 3600),
])
def test_the_rounding_direction_is_documented_in_both_directions(seconds, expected_s):
    """Rounds down above a minute, up below it. The docstring claimed "rounds
    down" without qualification, which is false for a sub-minute request."""
    got, _ = cron.nearest_expressible(seconds)
    assert got == expected_s


def test_a_sub_minute_request_is_disclosed_like_any_other_rounding():
    """The user asked for something cron cannot do. Rounding up silently would
    be the same failure as rounding down silently."""
    adapter = cron.CronAdapter()
    rendered = adapter.render(_job(), 30.0, ["/opt/py", "-m", "contextlake"])
    assert rendered["interval_s"] == 60
    assert rendered["notes"]


# ---- crontab splicing ---------------------------------------------------

EXISTING = (
    "# m h  dom mon dow   command\n"
    "0 */6 * * * cd /home/u/planning && ./backup.sh\n"
    "@reboot /usr/local/bin/something\n"
)


def test_an_existing_crontab_survives_byte_identical_around_the_block():
    spliced = cron.splice(EXISTING, "default", "*/30 * * * * /opt/py -m contextlake\n")
    for line in EXISTING.splitlines():
        assert line in spliced.splitlines()


def test_the_block_is_delimited_by_its_markers():
    spliced = cron.splice(EXISTING, "default", "*/30 * * * * /x\n")
    assert cron.BEGIN.format(name="default") in spliced
    assert cron.END.format(name="default") in spliced


def test_installing_twice_replaces_the_block_rather_than_duplicating_it():
    once = cron.splice(EXISTING, "default", "*/30 * * * * /x\n")
    twice = cron.splice(once, "default", "0 * * * * /y\n")
    assert twice.count(cron.BEGIN.format(name="default")) == 1
    assert "/y" in twice
    assert "/x" not in twice


def test_two_named_jobs_get_two_independent_blocks():
    text = cron.splice(EXISTING, "default", "*/30 * * * * /x\n")
    text = cron.splice(text, "nightly", "0 3 * * * /y\n")
    assert cron.BEGIN.format(name="default") in text
    assert cron.BEGIN.format(name="nightly") in text
    assert "/x" in text and "/y" in text


def test_removing_a_block_restores_the_original_byte_for_byte():
    """The property test the spec asks for: install AND uninstall must leave a
    pre-existing crontab as it was."""
    spliced = cron.splice(EXISTING, "default", "*/30 * * * * /x\n")
    assert cron.splice(spliced, "default", None) == EXISTING


def test_removing_a_block_that_is_not_there_changes_nothing():
    assert cron.splice(EXISTING, "ghost", None) == EXISTING


def test_an_empty_crontab_gains_only_the_block():
    text = cron.splice("", "default", "*/30 * * * * /x\n")
    assert text.startswith(cron.BEGIN.format(name="default"))
    assert text.endswith("\n")


def test_a_crontab_with_no_trailing_newline_is_repaired_not_corrupted():
    """cron ignores a final line with no newline. Appending straight onto it
    would silently disable the user's last job."""
    text = cron.splice("0 5 * * * /backup", "default", "*/30 * * * * /x\n")
    assert "0 5 * * * /backup\n" in text


def test_the_command_is_never_a_shell_string_with_user_input():
    """argv is joined with shlex.quote, so a job name or path containing a
    semicolon cannot become a second command."""
    import shlex

    adapter = cron.CronAdapter()
    line = adapter.render(_job(), 3600.0,
                          ["/opt/py", "-m", "contextlake", "schedule", "run",
                           "--job", "a b;rm -rf /"])["crontab"]
    assert shlex.quote("a b;rm -rf /") in line
    assert "; rm -rf /" not in line.replace(shlex.quote("a b;rm -rf /"), "")


def test_cron_does_not_claim_to_catch_up_after_sleep():
    assert cron.CronAdapter().catches_up_after_sleep is False


def test_a_failed_write_raises_oserror_not_calledprocesserror(monkeypatch):
    """`cmd_install` degrades on OSError. subprocess.CalledProcessError is
    not an OSError, so a `crontab -` failure must be re-raised as one or the
    degrade path never runs and the command crashes instead."""
    import subprocess as sp

    monkeypatch.setattr(
        cron.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 1, "", "permission denied"))
    with pytest.raises(OSError, match="permission denied"):
        cron._write_crontab("some text\n")


# ---- registry ------------------------------------------------------------

def test_cron_is_registered_in_the_platform_registry():
    from contextlake.schedule.platform import base

    assert base._registry()["cron"] is cron.CronAdapter


def test_detect_prefers_systemd_over_cron_when_both_are_usable(monkeypatch):
    from contextlake.schedule.platform import base, systemd

    monkeypatch.setattr(systemd.SystemdAdapter, "usable", lambda self: True)
    monkeypatch.setattr(cron.CronAdapter, "usable", lambda self: True)
    assert base.detect() == "systemd"
