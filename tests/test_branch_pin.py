"""Pinning the fleet to a named branch, and reporting the repositories that lack it.

`branch_strategy` existed as a config key with no way to set it from the command line, and
there was no way at all to say "put everything on release/24.1". Both are flags now.

The behaviour that needed deciding was not the pin, it was the miss. A fleet where the
requested branch exists in four repositories out of four hundred must not report the other
three hundred and ninety-six as having done what was asked. So a repository without the
branch is its own outcome, named in the run summary, rather than an ordinary switch to
something else.
"""

from __future__ import annotations

import argparse

import pytest

from contextlake.cli import _BRANCH_STRATEGIES, apply_cli_overrides
from contextlake.config import ConfigError
from contextlake.core import _REPO_OK_STATES, resolve_target_branch

# name / commit count / last-commit timestamp, the shape `_collect_branch_info` returns.
BRANCHES = [
    {"name": "main", "count": 400, "ts": 1000},
    {"name": "develop", "count": 900, "ts": 2000},
    {"name": "release/24.1", "count": 10, "ts": 500},
]


# --- resolution ------------------------------------------------------------------------

def test_no_pin_keeps_the_most_active_selection():
    assert resolve_target_branch(BRANCHES, "hybrid", "") == ("develop", False)
    assert resolve_target_branch(BRANCHES, "commits", "") == ("develop", False)
    assert resolve_target_branch(BRANCHES, "recency", "") == ("develop", False)


def test_a_pin_that_exists_wins_over_every_strategy():
    """The point of the flag: `release/24.1` is the least active branch here by both
    measures, and asking for it by name must still get it."""
    for strategy in sorted(_BRANCH_STRATEGIES):
        assert resolve_target_branch(BRANCHES, strategy, "release/24.1") == (
            "release/24.1", False)


def test_a_pin_that_is_absent_is_reported_rather_than_silently_replaced():
    target, missing = resolve_target_branch(BRANCHES, "hybrid", "release/99.9")
    assert missing is True
    assert target == "develop", "it still lands somewhere usable"


def test_surrounding_whitespace_on_a_pin_is_ignored():
    assert resolve_target_branch(BRANCHES, "hybrid", "  main  ") == ("main", False)


def test_a_pin_is_matched_exactly_and_never_by_prefix():
    """`release/24` is not `release/24.1`. A prefix match on an identity question is the
    bug class this project has hit repeatedly, so it is pinned as a test."""
    _target, missing = resolve_target_branch(BRANCHES, "hybrid", "release/24")
    assert missing is True


def test_a_repository_with_no_branches_reports_the_pin_as_missing():
    """Not `(None, False)`: "there was nothing to pin to" is a miss, and reporting it as a
    clean run is the same defect as a silent replacement."""
    assert resolve_target_branch([], "hybrid", "main") == (None, True)
    assert resolve_target_branch([], "hybrid", "") == (None, False)


# --- the outcome it produces -----------------------------------------------------------

def test_the_unpinned_outcome_is_not_counted_as_a_failure():
    """A repository that simply does not have the branch is an answer, not an error. If
    this state were outside the ok set, `--branch` on any real fleet would exit 1."""
    assert "unpinned" in _REPO_OK_STATES


# --- the flags -------------------------------------------------------------------------

def _args(**kw):
    return argparse.Namespace(**kw)


def test_the_branch_flag_reaches_the_config():
    config = {"branch": "", "branch_strategy": "hybrid"}
    apply_cli_overrides(_args(branch="release/24.1"), config)
    assert config["branch"] == "release/24.1"


def test_an_absent_flag_leaves_the_config_value_alone():
    config = {"branch": "from-config", "branch_strategy": "recency"}
    apply_cli_overrides(_args(), config)
    assert config["branch"] == "from-config"
    assert config["branch_strategy"] == "recency"


def test_a_misspelled_strategy_is_refused_instead_of_running_a_different_one():
    """`select_most_active_branch` falls through to hybrid for any name it does not know,
    so `--branch-strategy recentcy` used to run a selection nobody asked for and say
    nothing. Validated here rather than by argparse `choices`, so a bad value in the
    config file is caught too."""
    with pytest.raises(ConfigError, match="recentcy"):
        apply_cli_overrides(_args(branch_strategy="recentcy"), {})

    with pytest.raises(ConfigError, match="branch_strategy"):
        apply_cli_overrides(_args(), {"branch_strategy": "most-recent"})


def test_every_valid_strategy_is_accepted():
    for strategy in sorted(_BRANCH_STRATEGIES):
        config = {}
        apply_cli_overrides(_args(branch_strategy=strategy), config)
        assert config["branch_strategy"] == strategy


def test_the_default_config_ships_the_key_unset():
    """Empty means "pick the most active branch", which is what every existing workspace
    already does. A default of `main` here would silently re-point a whole fleet on upgrade."""
    from contextlake.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["branch"] == ""
    assert DEFAULT_CONFIG["branch_strategy"] == "hybrid"
