"""The cron adapter: expressible intervals, and never damaging a crontab."""
from __future__ import annotations

import os
import subprocess
import sys

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


# ---- exec path parsing (pure, no real crontab) ---------------------------

def test_exec_path_from_block_reads_the_first_command_token():
    text = (
        "# unrelated line\n"
        + cron.BEGIN.format(name="default") + "\n"
        + 'MAILTO=""\n'
        + "*/30 * * * * /home/x/.venv/bin/python -m contextlake schedule run --job default\n"
        + cron.END.format(name="default") + "\n"
        + "# unrelated after\n")
    assert cron._exec_path_from_block(text, "default") == "/home/x/.venv/bin/python"


def test_exec_path_from_block_handles_a_quoted_path_with_a_space():
    text = (
        cron.BEGIN.format(name="default") + "\n"
        + 'MAILTO=""\n'
        + "0 * * * * '/home/x y/.venv/bin/python' -m contextlake schedule run --job default\n"
        + cron.END.format(name="default") + "\n")
    assert cron._exec_path_from_block(text, "default") == "/home/x y/.venv/bin/python"


def test_exec_path_from_block_returns_none_when_the_block_is_absent():
    assert cron._exec_path_from_block(EXISTING, "default") is None
    assert cron._exec_path_from_block("", "default") is None


def test_state_reports_the_blocks_exec_path(monkeypatch):
    """The parser above is proven against a fixture string; this proves the
    wiring inside `state()`, with `_read_crontab` stubbed rather than calling
    the real `crontab` binary."""
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "0 * * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    monkeypatch.setattr(cron, "_read_crontab", lambda: text)
    state = cron.CronAdapter().state(_job())
    assert state["exec_path"] == "/x/python"


def test_state_reports_no_exec_path_when_not_installed(monkeypatch):
    monkeypatch.setattr(cron, "_read_crontab", lambda: EXISTING)
    state = cron.CronAdapter().state(_job())
    assert state["installed"] is False
    assert state["exec_path"] is None


# ---- interval read-back (finding 1: interval_s was hardcoded None) -------

def test_interval_s_from_block_reads_an_hourly_spec():
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "0 * * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    assert cron._interval_s_from_block(text, "default") == 3600


def test_interval_s_from_block_reads_a_minute_step_spec():
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "*/15 * * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    assert cron._interval_s_from_block(text, "default") == 900


def test_interval_s_from_block_returns_none_for_an_unrecognised_spec():
    """A spec this adapter never writes (hand-edited, or from another
    version) is "cannot tell", not a guess."""
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "17 3 * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    assert cron._interval_s_from_block(text, "default") is None


def test_interval_s_from_block_returns_none_when_the_block_is_absent():
    assert cron._interval_s_from_block(EXISTING, "default") is None
    assert cron._interval_s_from_block("", "default") is None


def test_state_reports_the_installed_interval(monkeypatch):
    text = (cron.BEGIN.format(name="default") + "\n"
           + 'MAILTO=""\n'
           + "0 */2 * * * /x/python -m contextlake schedule run --job default\n"
           + cron.END.format(name="default") + "\n")
    monkeypatch.setattr(cron, "_read_crontab", lambda: text)
    state = cron.CronAdapter().state(_job())
    assert state["interval_s"] == 7200


def test_state_reports_no_interval_when_not_installed(monkeypatch):
    monkeypatch.setattr(cron, "_read_crontab", lambda: EXISTING)
    state = cron.CronAdapter().state(_job())
    assert state["interval_s"] is None


def test_a_failed_write_raises_oserror_not_calledprocesserror(monkeypatch):
    """`cmd_install` degrades on OSError. subprocess.CalledProcessError is
    not an OSError, so a `crontab -` failure must be re-raised as one or the
    degrade path never runs and the command crashes instead."""
    import subprocess as sp

    # Bytes, as the real call returns them: the crontab is read and written
    # without text mode so no byte of it changes on the way through.
    monkeypatch.setattr(
        cron.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 1, b"", b"permission denied"))
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


# ---- the working directory a job keeps -------------------------------------

_LINE = "0 */6 * * * /opt/venv/bin/python -m contextlake schedule run --job default >/dev/null 2>&1"


def _block(command_field, name="default"):
    return (cron.BEGIN.format(name=name) + "\n"
            + f"0 */6 * * * {command_field}\n"
            + cron.END.format(name=name) + "\n")


def _cwd_job(cwd, name="default"):
    return jobs.new_job(name, ["bootstrap"], "auto", "cron", origin={"cwd": cwd})


def test_a_job_with_a_directory_changes_to_it_first_and_only_runs_if_that_worked():
    line = cron.CronAdapter().render(_cwd_job("/home/me/my ws"), 21600.0,
                                     ["/opt/py", "-m", "contextlake", "schedule", "run",
                                      "--job", "default"])["crontab"]
    assert line == ("0 */6 * * * cd '/home/me/my ws' && /opt/py -m contextlake schedule run "
                    "--job default >/dev/null 2>&1\n")


def test_a_job_without_a_directory_renders_the_line_earlier_versions_wrote():
    line = cron.CronAdapter().render(_job(), 21600.0,
                                     ["/opt/venv/bin/python", "-m", "contextlake", "schedule",
                                      "run", "--job", "default"])["crontab"]
    assert line == _LINE + "\n"


def test_a_percent_sign_is_escaped_because_cron_turns_it_into_a_newline():
    line = cron.CronAdapter().render(_cwd_job("/home/me/100%"), 3600.0,
                                     ["/opt/py", "--config", "/x/50%.ini"])["crontab"]
    assert line.count("%") == line.count("\\%") == 2
    assert "\\%" in line.split("&&")[0] and "\\%" in line.split("&&")[1]


@pytest.mark.skipif(sys.platform == "win32", reason="runs the line with sh")
def test_the_rendered_line_starts_the_command_in_the_directory_with_the_arguments(tmp_path):
    """Run the way cron runs it: `%` unescaped first (crontab(5)), then `sh -c`, from a
    different directory. The interpreter is a script that writes down where it was started
    and what it was given."""
    out = tmp_path / "out.txt"
    script = tmp_path / "fake python"
    script.write_text(f'#!/bin/sh\npwd > "{out}"\nprintf "%s\\n" "$@" >> "{out}"\n',
                      encoding="utf-8")
    script.chmod(0o755)
    where = tmp_path / "100% done"
    where.mkdir()
    argv = [str(script), "-m", "contextlake", "--config", "/etc/a b/x%y.ini", "schedule", "run",
            "--job", "default"]
    line = cron.CronAdapter().render(_cwd_job(str(where)), 3600.0, argv)["crontab"]
    command = line.split(None, 5)[5].replace("\\%", "%")

    subprocess.run(["sh", "-c", command], cwd=tmp_path.parent, check=True)

    written = out.read_text(encoding="utf-8").splitlines()
    assert written[0] == os.path.realpath(where)
    assert written[1:] == argv[1:]


@pytest.mark.skipif(sys.platform == "win32", reason="runs the line with sh")
def test_a_directory_that_has_gone_leaves_the_job_unrun(tmp_path):
    """The job does not run in the wrong place. `cd` fails and `&&` stops the line."""
    ran = tmp_path / "ran.txt"
    script = tmp_path / "py"
    script.write_text(f'#!/bin/sh\necho ran > "{ran}"\n', encoding="utf-8")
    script.chmod(0o755)
    line = cron.CronAdapter().render(_cwd_job(str(tmp_path / "gone")), 3600.0,
                                     [str(script), "-m", "contextlake"])["crontab"]
    command = line.split(None, 5)[5].replace("\\%", "%")
    result = subprocess.run(["sh", "-c", command], cwd=tmp_path, check=False,
                            capture_output=True)
    assert result.returncode != 0
    assert not ran.exists()


def test_a_control_character_in_the_directory_is_refused_not_written():
    with pytest.raises(ValueError, match="control character"):
        cron.CronAdapter().render(_cwd_job("/ws\n* * * * * rm -rf x"), 3600.0, ["/opt/py"])


def test_the_read_back_still_parses_a_line_earlier_versions_wrote():
    """An rc4 unit has no `cd` and no `--config`. It reads as an interpreter with no
    directory, so `status` can tell it apart from one that has them."""
    text = _block(_LINE.split(None, 5)[5])
    assert cron._exec_path_from_block(text, "default") == "/opt/venv/bin/python"
    launch = cron._launch_from_command(cron._block_command(text, "default"))
    assert launch == {"argv": ["/opt/venv/bin/python", "-m", "contextlake", "schedule", "run",
                               "--job", "default"], "cwd": None}


def test_the_read_back_skips_the_cd_and_returns_the_directory():
    command = ("cd '/home/me/my ws' && /opt/py -m contextlake --config /a/x.ini schedule run "
               "--job default >/dev/null 2>&1")
    text = _block(command)
    assert cron._exec_path_from_block(text, "default") == "/opt/py"
    assert cron._launch_from_command(command) == {
        "argv": ["/opt/py", "-m", "contextlake", "--config", "/a/x.ini", "schedule", "run",
                 "--job", "default"], "cwd": "/home/me/my ws"}


def test_the_read_back_round_trips_what_render_wrote(tmp_path):
    argv = ["/opt/py", "-m", "contextlake", "--config", "/etc/a b/x%y.ini", "schedule", "run",
            "--job", "default"]
    line = cron.CronAdapter().render(_cwd_job("/home/me/100% ws"), 3600.0, argv)["crontab"]
    launch = cron._launch_from_command(line.split(None, 5)[5])
    assert launch == {"argv": argv, "cwd": "/home/me/100% ws"}


def test_a_cd_that_is_not_the_shape_render_writes_reads_as_cannot_tell():
    assert cron._launch_from_command("cd /x; /opt/py -m contextlake") is None
    assert cron._launch_from_command("cd /x") is None
    assert cron._exec_path_from_block(_block("cd /x"), "default") is None


def test_state_reports_what_the_installed_line_runs_and_where(monkeypatch):
    command = ("cd /ws && /x/python -m contextlake --config /a/x.ini schedule run --job default "
               ">/dev/null 2>&1")
    monkeypatch.setattr(cron, "_read_crontab", lambda: _block(command))
    state = cron.CronAdapter().state(_job())
    assert state["exec_path"] == "/x/python"
    assert state["launch"]["cwd"] == "/ws"
    assert "--config" in state["launch"]["argv"]


def test_state_reports_no_launch_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(cron, "_read_crontab", lambda: EXISTING)
    assert cron.CronAdapter().state(_job())["launch"] is None
