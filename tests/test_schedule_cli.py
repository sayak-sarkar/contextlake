"""The `schedule` command's parse shape, and `recommend`'s output.

`schedule` is a TOP-LEVEL command with a positional action, following
`kb source` (cli.py:1057), NOT a third namespace. _resolve_command collapses a
namespace onto args.command, so `schedule status` would arrive at dispatch as
plain "status" and collide with `mirror status`.
"""
from __future__ import annotations

import pytest

from contextlake.cli import build_parser
from contextlake.schedule import cmds
from contextlake.schedule.platform import base as platform_base


def _parse(argv):
    return build_parser().parse_args(argv)


# ---- the parse shape ---------------------------------------------------

@pytest.mark.parametrize("action", [
    "recommend", "install", "uninstall", "status", "run", "list", "reset", "interval"])
def test_every_action_parses(action):
    args = _parse(["schedule", action])
    assert args.command == "schedule"
    assert args.action == action


def test_schedule_is_top_level_not_a_namespace():
    """If it were a namespace, `schedule status` would come out as "status"."""
    args = _parse(["schedule", "status"])
    assert args.command == "schedule"
    assert getattr(args, "subcommand", None) is None


def test_mirror_status_is_untouched():
    from contextlake.cli import _resolve_command

    args = _parse(["mirror", "status"])
    _resolve_command(args, build_parser())
    assert args.command == "status"


def test_an_unknown_action_is_rejected_at_parse_time():
    with pytest.raises(SystemExit):
        _parse(["schedule", "recomend"])


def test_the_action_is_required():
    """No nargs="?" anywhere near a `choices` list: on Python 3.9-3.11 argparse
    validates the SUPPRESS sentinel itself against choices when the option is
    omitted. The `doctor` registration comment documents that trap."""
    with pytest.raises(SystemExit):
        _parse(["schedule"])


# ---- the trailing command: `--` separator, and flags reach their target ---

@pytest.mark.parametrize("argv,expected", [
    (["schedule", "interval", "30m", "run", "--", "kb", "index", "--workspace", "/x"],
     ["30m", "run", "kb", "index", "--workspace", "/x"]),
    (["schedule", "interval", "auto", "run", "--", "mirror", "sync", "--repos", "acme/*"],
     ["auto", "run", "mirror", "sync", "--repos", "acme/*"]),
    (["schedule", "interval", "2h", "run", "--", "kb", "wiki", "--force"],
     ["2h", "run", "kb", "wiki", "--force"]),
    (["schedule", "interval", "1h", "run", "--", "kb", "index", "--force"],
     ["1h", "run", "kb", "index", "--force"]),
])
def test_the_captured_command_keeps_its_own_flags(argv, expected):
    """A flag inside the captured command must NOT be parsed as a flag of
    `schedule`. This is why `interval`'s trailing command needs a `--`
    separator. argparse consumes the `--` itself, so it never shows up in
    `rest` -- unlike the old REMAINDER shape, which preserved it."""
    assert _parse(argv).rest == expected


@pytest.mark.parametrize("argv,attr,expected", [
    (["schedule", "recommend", "--json"], "json", True),
    (["schedule", "install", "--interval", "2h"], "interval", "2h"),
    (["schedule", "run", "--foreground"], "foreground", True),
    (["schedule", "uninstall", "--job", "nightly"], "job", "nightly"),
    (["schedule", "reset", "--history"], "history", True),
    (["schedule", "uninstall", "--all"], "all", True),
])
def test_a_flag_after_the_action_reaches_its_destination(argv, attr, expected):
    """`argparse.REMAINDER` swallowed every one of these into `rest`, so
    `schedule uninstall --job nightly` silently removed the default job
    instead of `nightly`. Each case asserts the flag's VALUE, not that the
    line parses: every one of them parsed cleanly under REMAINDER while
    doing the wrong thing."""
    args = _parse(argv)
    assert getattr(args, attr) == expected
    assert args.rest == []


def test_a_global_flag_before_the_action_still_works():
    args = _parse(["schedule", "--quiet", "recommend"])
    assert args.action == "recommend"
    assert args.quiet is True


def test_schedule_flags_parse_when_they_come_before_the_remainder():
    args = _parse(["schedule", "--job", "nightly", "interval", "2h", "run", "kb", "wiki"])
    assert args.job == "nightly"
    assert args.rest == ["2h", "run", "kb", "wiki"]


def test_the_separator_form_still_captures_flags_verbatim():
    """Task 12's ad-hoc jobs depend on `interval ... run -- <command>`
    capturing the trailing command's own flags untouched."""
    args = _parse(["schedule", "interval", "1h", "run", "--",
                   "kb", "index", "--workspace", "/x", "--force"])
    assert args.rest == ["1h", "run", "kb", "index", "--workspace", "/x", "--force"]


# ---- registry obligations ----------------------------------------------

def test_schedule_is_categorized_exactly_once():
    """test_every_registered_command_is_categorized_exactly_once goes red
    without this. Asserted here too so the failure names the cause."""
    from contextlake.cli import _COMMAND_CATEGORIES

    hits = [ns for ns, _, names in _COMMAND_CATEGORIES if "schedule" in names]
    assert hits == [None], "schedule must be top-level (namespace None), listed once"


def test_schedule_is_not_a_knowledge_layer_command():
    """`schedule recommend` and `schedule status` must work on a core-only
    install. Only `run` may reach the kb tier, and only inside the function."""
    from contextlake.cli import _KB_COMMANDS

    assert "schedule" not in _KB_COMMANDS


@pytest.mark.parametrize("flag", ["job", "foreground", "platform", "purge", "all", "yes",
                                  "history"])
def test_every_new_flag_has_a_defaults_entry(flag):
    """A flag with default=SUPPRESS and no _DEFAULTS entry leaves
    getattr(args, flag, None) returning the SUPPRESS sentinel, which is TRUTHY.
    That is `absent-field-reads-as-a-pass`."""
    from contextlake.cli import _DEFAULTS

    assert flag in _DEFAULTS


def test_parsed_flags_are_never_the_suppress_sentinel():
    import argparse

    args = _parse(["schedule", "recommend"])
    for name in ("job", "foreground", "platform", "purge", "all", "yes", "interval",
                "history"):
        assert getattr(args, name, None) is not argparse.SUPPRESS


# ---- settings from config ----------------------------------------------

def test_settings_come_from_the_config_with_defaults_underneath():
    s = cmds.settings_from_config({"schedule_duty_cycle": "0.25", "schedule_min": "15m"})
    assert s["duty_cycle"] == 0.25
    assert s["min_s"] == 900.0
    assert s["max_s"] == 86400.0
    assert s["fixed_s"] is None


def test_schedule_interval_auto_means_no_fixed_pin():
    assert cmds.settings_from_config({"schedule_interval": "auto"})["fixed_s"] is None
    assert cmds.settings_from_config({"schedule_interval": "  AUTO "})["fixed_s"] is None


def test_a_fixed_schedule_interval_becomes_a_pin():
    assert cmds.settings_from_config({"schedule_interval": "90m"})["fixed_s"] == 5400.0


def test_an_unparseable_config_value_falls_back_and_warns(gls_logs):
    """Refusing to run because one INI key is a typo is worse than running on
    the default and saying so. Uses gls_logs, not caplog: the package logger
    sets propagate=False, so caplog's root handler never sees the record and
    the assertion would pass whether or not anything was logged.
    """
    s = cmds.settings_from_config({"schedule_duty_cycle": "banana",
                                   "schedule_interval": "later"})
    assert s["duty_cycle"] == 0.10
    assert s["fixed_s"] is None
    text = gls_logs.text
    assert "schedule_duty_cycle" in text
    assert "schedule_interval" in text


def test_a_duty_cycle_outside_zero_to_one_is_refused():
    assert cmds.settings_from_config({"schedule_duty_cycle": "2.0"})["duty_cycle"] == 0.10
    assert cmds.settings_from_config({"schedule_duty_cycle": "0"})["duty_cycle"] == 0.10


def test_a_duty_cycle_of_exactly_one_is_refused():
    """The bound is exclusive: 1.0 means "run continuously" (floor_duty
    equals the run duration itself, no gap at all), not merely a high but
    legitimate duty cycle. 0.9999 is legitimate and must still pass."""
    assert cmds.settings_from_config({"schedule_duty_cycle": "1.0"})["duty_cycle"] == 0.10
    assert cmds.settings_from_config({"schedule_duty_cycle": "0.9999"})["duty_cycle"] == 0.9999


def test_min_above_max_is_refused_rather_than_producing_an_empty_range():
    s = cmds.settings_from_config({"schedule_min": "20h", "schedule_max": "2h"})
    assert s["min_s"] <= s["max_s"]


# ---- recommend ---------------------------------------------------------

def test_recommend_on_a_cold_machine_says_it_is_a_default(tmp_path, capsys):
    import argparse

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    rc = cmds.cmd_recommend(argparse.Namespace(json=False), config)
    out = capsys.readouterr().out
    assert rc == 0
    assert "6h" in out
    assert "default" in out.lower()
    assert "no measured runs" in out.lower()


def test_recommend_json_is_machine_readable(tmp_path):
    import argparse
    import json as jsonlib

    from contextlake.schedule import history
    from contextlake.schedule import recommend as recommend_mod

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    path = history.history_path(config)
    for i in range(3):
        history.append_run(path, {"ts": f"2026-08-2{i+1}T00:00:00Z", "kind": "incremental",
                                  "duration_s": 420.0, "exit": 0,
                                  "repos_total": 480, "repos_changed": 3})
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cmds.cmd_recommend(argparse.Namespace(json=True), config) == 0
    payload = jsonlib.loads(buf.getvalue())
    assert payload["basis"] in ("duty", "activity")
    assert payload["measured"] is True
    assert payload["samples"] == 3
    # Pins the human-readable string against the seconds figure, so a
    # mismatch between the two is caught rather than only checking the key
    # is present.
    assert payload["interval"] == recommend_mod.format_duration(payload["interval_seconds"])


def test_recommend_says_when_the_activity_bound_was_never_measured(tmp_path, capsys):
    """An unmeasured activity floor printed NOTHING, so the reader could not
    tell it apart from the bound being switched off.

    This is the normal state on an install without the `kb` extra: the index
    stage is what records how many repositories changed, and nothing else does.
    """
    import argparse

    from contextlake.schedule import history

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    path = history.history_path(config)
    # Scoreable (incremental, exit 0) so the recommendation is `measured`, but
    # carrying no `repos_changed`, which is what a mirror-only or core-only
    # install writes. Both halves are needed: without the first the cold-start
    # branch runs instead, and the activity line is never reached.
    for day in (21, 22, 23):
        history.append_run(path, {"ts": f"2026-08-{day}T00:00:00Z",
                                  "kind": "incremental", "duration_s": 420.0, "exit": 0})

    assert cmds.cmd_recommend(argparse.Namespace(json=False), config) == 0
    out = capsys.readouterr().out

    assert "activity floor:   not measured" in out
    assert "kb extra" in out
    # The duty floor must still be reported. The claim is that the interval
    # rests on it, so an output that dropped both would satisfy the assertion
    # above while saying less than before.
    assert "duty-cycle floor" in out


def test_recommend_json_separates_unmeasured_activity_from_no_change(tmp_path):
    """`floor_activity_seconds` is null for two unrelated reasons: nothing ever
    recorded activity, and activity was recorded with nothing changing (an
    infinite floor). The text output has always distinguished them. JSON
    collapsed both to null, so no consumer could.
    """
    import argparse
    import contextlib
    import io
    import json as jsonlib

    from contextlake.schedule import history

    cases = iter(range(100))

    def _activity(records):
        # A fresh cache_dir per case: history is append-only, so reusing one
        # would let the first case's records score the second.
        cfg = {"cache_dir": str(tmp_path / f"case{next(cases)}"), "cache_file": "p.txt"}
        path = history.history_path(cfg)
        for r in records:
            history.append_run(path, r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cmds.cmd_recommend(argparse.Namespace(json=True), cfg) == 0
        return jsonlib.loads(buf.getvalue())

    def _runs(extra=None):
        extra = extra or {}
        return [dict({"ts": f"2026-08-{day}T00:00:00Z", "kind": "incremental",
                      "duration_s": 420.0, "exit": 0}, **(extra.get(day) or {}))
                for day in (21, 22, 23)]

    # No record carries repos_changed at all.
    assert _activity(_runs())["activity"] == "not-measured"

    # Recorded, and nothing moved. change_rate_per_hour excludes the FIRST
    # record (its changes predate the window), so the zeroes that matter are
    # the later two.
    no_change = _runs(extra={21: {"repos_changed": 4, "repos_total": 480},
                             22: {"repos_changed": 0, "repos_total": 480},
                             23: {"repos_changed": 0, "repos_total": 480}})
    assert _activity(no_change)["activity"] == "no-change"

    # Recorded, and repositories moved.
    moved = _runs(extra={21: {"repos_changed": 0, "repos_total": 480},
                         22: {"repos_changed": 3, "repos_total": 480},
                         23: {"repos_changed": 5, "repos_total": 480}})
    payload = _activity(moved)
    assert payload["activity"] == "measured"
    # A real number, not just a label: this is the state where the floor is
    # actually computable, and it is the one the other two must differ from.
    assert payload["floor_activity_seconds"] > 0


def test_recommend_changes_nothing_on_disk(tmp_path):
    import argparse

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    before = sorted(p.name for p in tmp_path.iterdir())
    cmds.cmd_recommend(argparse.Namespace(json=False), config)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ---- install -------------------------------------------------------------

def test_the_degrade_path_prints_the_configured_battery_behaviour(tmp_path, monkeypatch, capsys):
    """The fallback render must carry the same on_battery as the install
    attempt. A user with schedule_on_battery=run handed a unit containing
    ConditionACPower=true gets the opposite of their config, and on a
    read-only home that printed unit is the only artefact they receive.
    """
    import argparse

    from contextlake.schedule.platform import systemd

    def _refuse(self, job, interval_s, exec_argv, **options):
        raise OSError("read-only home")

    monkeypatch.setattr(systemd.SystemdAdapter, "install", _refuse)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt",
              "schedule_on_battery": "run"}
    args = argparse.Namespace(job=None, interval=None, platform="systemd")
    rc = cmds.cmd_install(args, config)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ConditionACPower" not in out


def test_cmd_install_reports_the_rounding_cron_had_to_do(tmp_path, monkeypatch):
    """`render` computes the rounding disclosure, but `install` returns only a
    list of paths written. Without this, the disclosure the plan requires
    ("says which") is computed and thrown away, and a user who asked for 70m
    is told "every 70m" while the crontab holds an hourly line.

    ``cmds.log`` is patched directly rather than read through capsys: see
    ``_log_lines`` in ``tests/test_schedule_run.py`` for why capsys reads back
    empty here once any earlier test in the session has called ``log()``.
    """
    import argparse

    from contextlake.schedule.platform import cron

    monkeypatch.setattr(cron, "_read_crontab", lambda: "")
    monkeypatch.setattr(cron, "_write_crontab", lambda text: None)
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt",
              "schedule_on_battery": "skip"}
    args = argparse.Namespace(job=None, interval="70m", platform="cron")
    rc = cmds.cmd_install(args, config)
    out = "\n".join(lines)
    assert rc == 0
    assert "every 1h" in out
    assert "cron cannot express 70m" in out


@pytest.mark.parametrize("platform_name", sorted(platform_base._registry()))
def test_the_degrade_path_prints_only_artefacts_never_metadata(
        tmp_path, monkeypatch, platform_name):
    """render() mixes artefact keys (files to write) with metadata keys
    (facts about the install, like cron's `interval_s`). The degrade path
    used to print every key as if it were a file, so a section headed
    `----- interval_s -----` showed up under "install these yourself"
    holding a plain int. Runs over every registered adapter, not just cron,
    so a Plan 2 backend that mixes metadata into `render()` fails this on
    day one rather than shipping the same defect again."""
    import argparse
    import re

    from contextlake.schedule import jobs as jobstore
    from contextlake.schedule.platform import base

    cls = base._registry()[platform_name]

    def _refuse(self, job, interval_s, exec_argv, **options):
        raise OSError("no permission to write the unit")

    monkeypatch.setattr(cls, "install", _refuse)
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    args = argparse.Namespace(job=None, interval=None, platform=platform_name)
    rc = cmds.cmd_install(args, config)
    assert rc == 0

    out = "\n".join(lines)
    headers = set(re.findall(r"----- (.+?) -----", out))

    job = jobstore.new_job(jobstore.DEFAULT_JOB, list(jobstore.DEFAULT_ARGV),
                           "auto", cls.id)
    interval_s, _ = cmds.resolve_interval(config, "auto")
    rendered = cls().render(job, interval_s,
                            cmds.exec_argv_for(jobstore.DEFAULT_JOB),
                            on_battery=config.get("schedule_on_battery", "skip"))
    expected = set(rendered) - cls.metadata_keys
    assert headers == expected
    # Not redundant: `expected` can be empty, and `headers == expected` would
    # then pass against an adapter that printed nothing at all.
    assert headers, "expected at least one artefact header"
    # A loop over `metadata_keys` asserting no metadata key is a header used to
    # sit here. It could not fail: `expected` is `set(rendered) - metadata_keys`,
    # so once `headers == expected` holds, no metadata key IS a header, by
    # construction. The check above already carries the whole claim.


def test_cmd_install_does_not_duplicate_the_cannot_catch_up_note(tmp_path, monkeypatch):
    """cron's own `state()` already reports it cannot replay a run missed
    while asleep; `install` printing it again unconditionally showed the
    identical sentence twice on every cron install.

    `_read_crontab` must reflect what `_write_crontab` wrote, or `state()`
    (called after `install()` inside `cmd_install`) sees an empty crontab,
    reports "not installed", and never contributes its own note, which would
    make this test pass whether or not the duplicate had been fixed.
    """
    import argparse

    from contextlake.schedule.platform import cron

    written = {"text": ""}
    monkeypatch.setattr(cron, "_read_crontab", lambda: written["text"])
    monkeypatch.setattr(cron, "_write_crontab", lambda text: written.__setitem__("text", text))
    lines = []
    monkeypatch.setattr(cmds, "log", lines.append)

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt",
              "schedule_on_battery": "skip"}
    args = argparse.Namespace(job=None, interval=None, platform="cron")
    rc = cmds.cmd_install(args, config)
    out = "\n".join(lines).lower()
    assert rc == 0
    assert out.count("does not replay a run missed") == 1
