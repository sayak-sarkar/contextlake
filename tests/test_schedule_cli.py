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


# ---- REMAINDER: prove it before Task 11 builds on it -------------------

@pytest.mark.parametrize("argv,expected", [
    (["schedule", "interval", "30m", "run", "kb", "index", "--workspace", "/x"],
     ["30m", "run", "kb", "index", "--workspace", "/x"]),
    (["schedule", "interval", "auto", "run", "mirror", "sync", "--repos", "acme/*"],
     ["auto", "run", "mirror", "sync", "--repos", "acme/*"]),
    (["schedule", "interval", "2h", "run", "kb", "wiki", "--force"],
     ["2h", "run", "kb", "wiki", "--force"]),
    (["schedule", "interval", "1h", "run", "--", "kb", "index", "--force"],
     ["1h", "run", "--", "kb", "index", "--force"]),
])
def test_the_captured_command_keeps_its_own_flags(argv, expected):
    """A flag inside the captured command must NOT be parsed as a flag of
    `schedule`. This is the entire reason for REMAINDER."""
    assert _parse(argv).rest == expected


def test_a_global_flag_before_the_action_still_works():
    args = _parse(["schedule", "--quiet", "recommend"])
    assert args.action == "recommend"
    assert args.quiet is True


def test_schedule_flags_parse_when_they_come_before_the_remainder():
    args = _parse(["schedule", "--job", "nightly", "interval", "2h", "run", "kb", "wiki"])
    assert args.job == "nightly"
    assert args.rest == ["2h", "run", "kb", "wiki"]


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


@pytest.mark.parametrize("flag", ["job", "foreground", "platform", "purge", "all", "yes"])
def test_every_new_flag_has_a_defaults_entry(flag):
    """A flag with default=SUPPRESS and no _DEFAULTS entry leaves
    getattr(args, flag, None) returning the SUPPRESS sentinel, which is TRUTHY.
    That is `absent-field-reads-as-a-pass`."""
    from contextlake.cli import _DEFAULTS

    assert flag in _DEFAULTS


def test_parsed_flags_are_never_the_suppress_sentinel():
    import argparse

    args = _parse(["schedule", "recommend"])
    for name in ("job", "foreground", "platform", "purge", "all", "yes", "interval"):
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


def test_an_unparseable_config_value_falls_back_and_warns(caplog):
    """Refusing to run because one INI key is a typo is worse than running on
    the default and saying so."""
    s = cmds.settings_from_config({"schedule_duty_cycle": "banana",
                                   "schedule_interval": "later"})
    assert s["duty_cycle"] == 0.10
    assert s["fixed_s"] is None


def test_a_duty_cycle_outside_zero_to_one_is_refused():
    assert cmds.settings_from_config({"schedule_duty_cycle": "2.0"})["duty_cycle"] == 0.10
    assert cmds.settings_from_config({"schedule_duty_cycle": "0"})["duty_cycle"] == 0.10


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
    assert payload["interval"] == payload["interval"]  # present and stable


def test_recommend_changes_nothing_on_disk(tmp_path):
    import argparse

    config = {"cache_dir": str(tmp_path), "cache_file": "p.txt"}
    before = sorted(p.name for p in tmp_path.iterdir())
    cmds.cmd_recommend(argparse.Namespace(json=False), config)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
