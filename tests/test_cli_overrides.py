"""Regression tests for bug #1: boolean config settings silently ignored.

Before the fix, the paired --x/--no-x flags defaulted their dest to False, so
``apply_cli_overrides`` overwrote the config file on every run -- which silently
disabled branch protection and the clean-workspace requirement by default.
"""

import pytest

from contextlake.cli import apply_cli_overrides, build_parser
from contextlake.config import ConfigError

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
    args = build_parser().parse_args(["mirror", "status"])
    assert getattr(args, flag) is None, f"{flag} should be None when not passed"


def test_no_flags_preserve_config_values():
    """The core of bug #1: with no CLI flags, config-file values must survive."""
    args = build_parser().parse_args(["mirror", "sync"])
    config = {
        "protect_working_branches": "true",
        "require_clean_workspace": "true",
        "adaptive_workers": "true",
        "auto_stash": "false",
    }
    result = apply_cli_overrides(args, dict(config))
    assert result == config  # untouched


def test_negated_flag_sets_false():
    args = build_parser().parse_args(["mirror", "sync", "--no-protect-working-branches"])
    config = {"protect_working_branches": "true"}
    result = apply_cli_overrides(args, config)
    assert result["protect_working_branches"] == "false"


def test_positive_flag_sets_true():
    args = build_parser().parse_args(["mirror", "update", "--auto-stash"])
    config = {"auto_stash": "false"}
    result = apply_cli_overrides(args, config)
    assert result["auto_stash"] == "true"


def test_dry_run_flag():
    args = build_parser().parse_args(["mirror", "sync", "--dry-run"])
    config = apply_cli_overrides(args, {})
    assert config["dry_run"] == "true"


def test_scalar_overrides_applied_and_stringified():
    args = build_parser().parse_args(
        ["mirror", "clone", "--max-retries", "5", "--safe-branches", "main,trunk"]
    )
    config = apply_cli_overrides(args, {})
    assert config["max_retries"] == "5"
    assert config["safe_branches"] == "main,trunk"


def test_scalar_not_passed_leaves_config_untouched():
    args = build_parser().parse_args(["mirror", "clone"])
    config = {"max_retries": "9"}
    result = apply_cli_overrides(args, config)
    assert result["max_retries"] == "9"


@pytest.mark.parametrize("provider", ["anthropic", "cli"])
@pytest.mark.parametrize("argv", [
    ["kb", "wiki", "--llm", "{provider}"],
    ["bootstrap", "--llm", "{provider}"],
    # root hidden flag, pre-subparser style: `contextlake --llm X kb wiki`
    ["--llm", "{provider}", "kb", "wiki"],
])
def test_llm_flag_accepts_anthropic_and_cli_providers(argv, provider):
    """--llm anthropic / --llm cli must not be rejected by argparse `choices`."""
    full_argv = [a.format(provider=provider) for a in argv]
    args = build_parser().parse_args(full_argv)
    assert args.llm == provider


# --- yes/no keys written in a config file -----------------------------------------
# Every reader compares against the literal "true", so `dry_run = yes` ran for real and
# `require_clean_workspace = yes` switched its guard off, with no message.

@pytest.mark.parametrize("flag", TRISTATE)
@pytest.mark.parametrize("spelling, expected", [
    ("true", "true"), ("True", "true"), ("yes", "true"), ("YES", "true"), ("on", "true"),
    ("1", "true"), (" yes ", "true"),
    ("false", "false"), ("False", "false"), ("no", "false"), ("off", "false"), ("0", "false"),
])
def test_a_file_spelling_is_normalised_to_true_or_false(flag, spelling, expected):
    args = build_parser().parse_args(["mirror", "status"])
    assert apply_cli_overrides(args, {flag: spelling})[flag] == expected


@pytest.mark.parametrize("flag", TRISTATE)
@pytest.mark.parametrize("spelling", ["maybe", "ture", ""])
def test_an_unknown_spelling_stops_the_run_and_names_the_key(flag, spelling):
    args = build_parser().parse_args(["mirror", "status"])
    with pytest.raises(ConfigError, match=f"^{flag}: {spelling!r} is not one of true, false"):
        apply_cli_overrides(args, {flag: spelling})


def test_a_flag_on_the_command_line_decides_over_a_bad_file_value():
    args = build_parser().parse_args(["mirror", "sync", "--dry-run"])
    assert apply_cli_overrides(args, {"dry_run": "maybe"})["dry_run"] == "true"
