"""The `--repos` pattern syntax that `docs/mirroring-repositories.md` documents.

The doc states four wildcards, four matching rules, and one workaround for a repo
whose name contains a glob character. Each row here is one of those claims. A doc
making a behavioural claim with nothing asserting it is how this project's docs have
gone stale before, so the table below is deliberately a mirror of the published one.

`--repos-exact` is deliberately absent. It was removed in c10e6bc3 because it reached
only five of the seven places that filter repos, so one pattern scoped differently
depending on the command. Anchoring is the only behaviour now, and there is no flag to
thread.
"""

from __future__ import annotations

import pytest

from contextlake.core import match_repo_filter, repo_filter_patterns


@pytest.mark.parametrize(("pattern", "full", "local", "expected"), [
    # `*` matches any run of characters, including none.
    ("team/*", "acme/team/api", "team/api", True),
    # `?` matches exactly one character.
    ("api-v?", "acme/api-v1", "api-v1", True),
    ("api-v?", "acme/api-v12", "api-v12", False),
    # `[abc]` matches one character from the set.
    ("api-v[12]", "acme/api-v1", "api-v1", True),
    ("api-v[12]", "acme/api-v3", "api-v3", False),
    # `[!abc]` matches one character NOT in the set.
    ("api-v[!0]", "acme/api-v1", "api-v1", True),
    ("api-v[!0]", "acme/api-v0", "api-v0", False),
])
def test_the_documented_wildcards_behave_as_documented(pattern, full, local, expected):
    assert match_repo_filter(full, local, [pattern]) is expected


def test_patterns_are_anchored_not_substring():
    """The rule that makes `--repos api` safe: it must describe the whole path.

    A filter selecting more than it was asked for is discovered after a fleet-wide
    run, which is why this is the default rather than an opt-in.
    """
    assert match_repo_filter("acme/api", "api", ["api"]) is True
    assert match_repo_filter("acme/forecast-api", "forecast-api", ["api"]) is False
    assert match_repo_filter("acme/api-gateway", "api-gateway", ["api"]) is False
    # Substring is still available, spelled the way it is spelled everywhere else.
    assert match_repo_filter("acme/forecast-api", "forecast-api", ["*api*"]) is True


def test_matching_is_case_insensitive():
    assert match_repo_filter("acme/team/api", "team/api", ["TEAM/*"]) is True


def test_each_pattern_is_tried_against_both_the_qualified_and_the_local_path():
    """Either form a user might type works, which is why the matcher takes two paths."""
    assert match_repo_filter("acme/team/api", "team/api", ["acme/team/*"]) is True
    assert match_repo_filter("acme/team/api", "team/api", ["team/api"]) is True


def test_a_comma_separated_filter_selects_a_repo_matching_any_one_pattern():
    patterns = repo_filter_patterns({"repo_filter": "team/*,sensors/core"})
    assert patterns == ["team/*", "sensors/core"]
    assert match_repo_filter("acme/team/api", "team/api", patterns) is True
    assert match_repo_filter("acme/sensors/core", "sensors/core", patterns) is True
    assert match_repo_filter("acme/other/x", "other/x", patterns) is False


@pytest.mark.parametrize(("pattern", "literal", "other"), [
    ("odd[*]name", "odd*name", "oddXname"),
    ("a[?]b", "a?b", "axb"),
])
def test_a_glob_character_in_a_repo_name_is_matched_with_a_character_set(
        pattern, literal, other):
    """There is no escape character, so the documented workaround is a one-character
    set. Without it, `odd*name` matches `oddXname` too and cannot pick out the repo
    that is literally named `odd*name`."""
    assert match_repo_filter(f"acme/{literal}", literal, [pattern]) is True
    assert match_repo_filter(f"acme/{other}", other, [pattern]) is False
    # The bare pattern is what fails to discriminate, which is why the note exists.
    bare = pattern.replace("[", "").replace("]", "")
    assert match_repo_filter(f"acme/{other}", other, [bare]) is True
