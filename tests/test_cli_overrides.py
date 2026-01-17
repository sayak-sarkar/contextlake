"""Regression tests for bug #1: boolean config settings silently ignored.

Before the fix, the paired --x/--no-x flags defaulted their dest to False, so
``apply_cli_overrides`` overwrote the config file on every run -- which silently
disabled branch protection and the clean-workspace requirement by default.
"""

import pytest

from gitlab_sync.cli import apply_cli_overrides, build_parser

TRISTATE = [
    "clean_corrupted",
    "adaptive_workers",
    "protect_working_branches",
    "require_clean_workspace",
    "auto_stash",
    "dry_run",
]


@pytest.mark.parametrize("flag", TRISTATE)
def test_tristate_flags_default_to_none(flag):
    args = build_parser().parse_args(["status"])
    assert getattr(args, flag) is None, f"{flag} should be None when not passed"


def test_no_flags_preserve_config_values():
    """The core of bug #1: with no CLI flags, config-file values must survive."""
    args = build_parser().parse_args(["sync"])
    config = {
        "protect_working_branches": "true",
        "require_clean_workspace": "true",
        "adaptive_workers": "true",
        "auto_stash": "false",
    }
    result = apply_cli_overrides(args, dict(config))
    assert result == config  # untouched


def test_negated_flag_sets_false():
    args = build_parser().parse_args(["sync", "--no-protect-working-branches"])
    config = {"protect_working_branches": "true"}
    result = apply_cli_overrides(args, config)
    assert result["protect_working_branches"] == "false"


def test_positive_flag_sets_true():
    args = build_parser().parse_args(["update", "--auto-stash"])
    config = {"auto_stash": "false"}
    result = apply_cli_overrides(args, config)
    assert result["auto_stash"] == "true"


def test_dry_run_flag():
    args = build_parser().parse_args(["sync", "--dry-run"])
    config = apply_cli_overrides(args, {})
    assert config["dry_run"] == "true"


def test_scalar_overrides_applied_and_stringified():
    args = build_parser().parse_args(
        ["clone", "--max-retries", "5", "--safe-branches", "main,trunk"]
    )
    config = apply_cli_overrides(args, {})
    assert config["max_retries"] == "5"
    assert config["safe_branches"] == "main,trunk"


def test_scalar_not_passed_leaves_config_untouched():
    args = build_parser().parse_args(["clone"])
    config = {"max_retries": "9"}
    result = apply_cli_overrides(args, config)
    assert result["max_retries"] == "9"
