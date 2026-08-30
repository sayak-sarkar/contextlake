"""Units that outlived their job record.

`state()` answers "is job X installed?", which can only be asked about a job
that still has a record. The reverse case had no reader at all: delete a record
and its unit keeps firing on schedule, is absent from `schedule list`, and
`uninstall` cannot reach it, because it resolves a job name through the record
that is gone.

These tests never touch the real crontab or the real unit directory. Every one
of them either monkeypatches the adapter's reader or points it at tmp_path.
"""
from __future__ import annotations

import argparse

from contextlake.schedule import jobs as jobstore
from contextlake.schedule import report
from contextlake.schedule.platform import base, cron, systemd


def _config(tmp_path):
    return {"cache_dir": str(tmp_path), "cache_file": "p.txt"}


# ---- the adapters can enumerate -----------------------------------------

def test_systemd_lists_job_names_from_its_timer_files(tmp_path, monkeypatch):
    units = tmp_path / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "contextlake-default.timer").write_text("x")
    (units / "contextlake-nightly.timer").write_text("x")
    # A service file for the same job must not double-count it, and a unit
    # belonging to something else entirely must not be claimed.
    (units / "contextlake-default.service").write_text("x")
    (units / "logrotate.timer").write_text("x")
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(units))

    assert systemd.SystemdAdapter().installed_names() == ["default", "nightly"]


def test_systemd_reports_none_installed_when_the_directory_is_absent(tmp_path, monkeypatch):
    """A directory that does not exist IS a measurement, and its answer is
    none. That is different from being unable to read one."""
    monkeypatch.setattr(systemd, "unit_dir", lambda: str(tmp_path / "nope"))
    assert systemd.SystemdAdapter().installed_names() == []


def test_cron_lists_job_names_from_its_markers(monkeypatch):
    text = (
        "0 * * * * some-unrelated-user-job\n"
        + cron.BEGIN.format(name="default") + "\n"
        + "*/30 * * * * contextlake bootstrap\n"
        + cron.END.format(name="default") + "\n"
        + cron.BEGIN.format(name="wiki-refresh") + "\n"
        + "0 2 * * * contextlake kb wiki\n"
        + cron.END.format(name="wiki-refresh") + "\n"
    )
    monkeypatch.setattr(cron, "_read_crontab", lambda: text)
    assert cron.CronAdapter().installed_names() == ["default", "wiki-refresh"]
    # The user's own line is outside any marked block and is never claimed.
    assert "some-unrelated-user-job" not in cron.CronAdapter().installed_names()


def test_the_base_adapter_cannot_enumerate_and_says_so():
    """None, not []. An adapter with no enumeration must not be read as
    having measured that nothing is installed."""
    assert base.Adapter().installed_names() is None


# ---- the diff ------------------------------------------------------------

def _only(monkeypatch, name, installed):
    # Patches the REGISTRY, not available(): orphan enumeration deliberately
    # asks every registered adapter, including ones this machine cannot use.
    monkeypatch.setattr(base, "registered", lambda: [name])
    adapter = base.Adapter()
    adapter.installed_names = lambda: installed
    monkeypatch.setattr(base, "get", lambda _n: adapter)


def test_a_unit_with_no_job_record_is_reported(monkeypatch):
    _only(monkeypatch, "cron", ["default", "ghost"])
    assert report.orphaned_units({"default"}) == ([("cron", "ghost")], [])


def test_a_job_with_a_record_is_not_an_orphan(monkeypatch):
    _only(monkeypatch, "cron", ["default"])
    assert report.orphaned_units({"default"}) == ([], [])


def test_an_adapter_that_cannot_enumerate_is_named_not_read_as_clean(monkeypatch):
    """The load-bearing case, and the reason the return value is a PAIR.

    Skipping a platform and finding nothing on it both produce an empty orphan
    list, so a caller given only that list cannot tell "checked, clean" from
    "never looked". Asserting only on the empty list would pass whether the
    code skipped None or coerced it to [], which is an assertion that cannot
    fail. The unchecked half is what makes the two distinguishable.
    """
    _only(monkeypatch, "cron", None)
    assert report.orphaned_units(set()) == ([], ["cron"])
    # The same adapter, having actually measured, reports and is not listed
    # as unchecked.
    _only(monkeypatch, "cron", ["ghost"])
    assert report.orphaned_units(set()) == ([("cron", "ghost")], [])


def test_a_probe_that_raises_is_announced_rather_than_swallowed(monkeypatch):
    """A raising probe means this platform was NOT checked. Silence there
    reads as "none found", which is the same defect one level down."""
    def _boom():
        raise OSError("crontab unreadable")

    monkeypatch.setattr(base, "registered", lambda: ["cron"])
    adapter = base.Adapter()
    adapter.installed_names = _boom
    monkeypatch.setattr(base, "get", lambda _n: adapter)
    lines = []
    monkeypatch.setattr(report, "log", lines.append)

    assert report.orphaned_units(set()) == ([], ["cron"])
    assert any("orphaned units" in line for line in lines), lines
    assert any("crontab unreadable" in line for line in lines), lines


# ---- what the user sees --------------------------------------------------

def test_schedule_list_names_the_orphan_and_how_to_remove_it(tmp_path, monkeypatch, capsys):
    _only(monkeypatch, "cron", ["ghost"])
    config = _config(tmp_path)
    jobstore.write_job(jobstore.jobs_path(config),
                       jobstore.new_job("default", ["bootstrap"], "auto", "cron"))

    assert report.cmd_list(argparse.Namespace(json=False), config) == 0
    out = capsys.readouterr().out

    assert "ghost" in out
    # Naming it is the point. A count would say something is wrong without
    # saying which thing, and the name is what the removal command needs.
    assert "uninstall" in out
    assert "default" in out, "the real job must still be listed"


def test_schedule_list_reports_an_orphan_even_with_no_jobs_left(tmp_path, monkeypatch, capsys):
    """The exact shape of the defect: the record is gone, so the old code took
    the "No scheduled jobs" early return and never looked at the platform."""
    _only(monkeypatch, "cron", ["ghost"])

    assert report.cmd_list(argparse.Namespace(json=False), _config(tmp_path)) == 0
    out = capsys.readouterr().out

    assert "ghost" in out
    # Saying there are no job records is true and stays. What must NOT survive
    # is the old early return's invitation to create one, which told a reader
    # with a unit still firing that there was nothing here.
    assert "Create one with" not in out


def test_schedule_list_json_carries_jobs_and_orphans(tmp_path, monkeypatch, capsys):
    import json as jsonlib

    _only(monkeypatch, "cron", ["ghost"])
    config = _config(tmp_path)
    jobstore.write_job(jobstore.jobs_path(config),
                       jobstore.new_job("default", ["bootstrap"], "auto", "cron"))

    assert report.cmd_list(argparse.Namespace(json=True), config) == 0
    payload = jsonlib.loads(capsys.readouterr().out)

    # Jobs stay at the top level, where 8.8.0 put them: a script reading
    # payload["default"] keeps working, which is what makes this additive
    # rather than the breaking reshape a major bump would have required.
    assert payload["default"]["name"] == "default"
    assert payload["_orphaned_units"] == [{"platform": "cron", "name": "ghost"}]
    assert payload["_unchecked_platforms"] == []


def test_schedule_list_says_which_platform_it_could_not_check(tmp_path, monkeypatch, capsys):
    """An unenumerable platform must be named. Printing nothing would let a
    clean-looking report stand for a platform that was never looked at."""
    _only(monkeypatch, "cron", None)

    assert report.cmd_list(argparse.Namespace(json=False), _config(tmp_path)) == 0
    out = capsys.readouterr().out

    assert "Not checked for orphaned units" in out
    assert "cron" in out


def test_an_unusable_platform_is_still_checked_for_orphans(monkeypatch):
    """The case filtering on usable() would have missed, and the one most worth
    reporting: a unit installed under systemd stays a unit after the machine
    stops offering systemd, and nothing would ever mention it again.
    """
    adapter = base.Adapter()
    adapter.usable = lambda: False
    adapter.installed_names = lambda: ["ghost"]
    monkeypatch.setattr(base, "registered", lambda: ["systemd"])
    monkeypatch.setattr(base, "get", lambda _n: adapter)

    assert report.orphaned_units(set()) == ([("systemd", "ghost")], [])


def test_the_new_json_keys_cannot_collide_with_a_job_name():
    """`_orphaned_units` leads with an underscore for a reason: check_name
    requires a job name to start with an alphanumeric, so the key can never be
    a job. Without the underscore it could be, which is what made a flat
    namespace look unusable and nearly cost a major version bump.
    """
    import pytest

    from contextlake.schedule.platform.base import check_name

    for key in ("_orphaned_units", "_unchecked_platforms"):
        with pytest.raises(ValueError):
            check_name(key)

    # ...and the unprefixed forms ARE legal job names, which is the whole point.
    assert check_name("orphaned_units") == "orphaned_units"
