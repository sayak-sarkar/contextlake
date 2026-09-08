"""The repo scope: segment-wise matching, partition ownership, sentinel rulings.

These guard an AUTHORIZATION decision, so the assertions are written to fail when the
rule is loosened, not merely when it errors. Several of them fail against the code that
stood before `kb/scope.py` existed, and those say so.
"""

from __future__ import annotations

import pytest

from contextlake.kb import scope
from contextlake.kb.model import (
    EXTERNAL_REPO,
    PACKAGES_REPO,
    SHARED_REPO,
    SYSTEM_REPO,
)

# --------------------------------------------------------------------------
# The glob
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("pattern", "repo", "expected"), [
    # The named case. `"a/bc".startswith("a/b")` is True, so a prefix test grants a
    # scope for `a/b` read access to `a/bc`. This is the assertion a `startswith`
    # implementation fails.
    ("a/b", "a/bc", False),
    ("a/b", "a/b", True),
    ("a/b", "a/b/c", False),
    # `*` stays inside one segment.
    ("a/*", "a/b", True),
    ("a/*", "a/b/c", False),
    ("a/*", "a", False),
    # `**` crosses segments, including zero of them.
    ("a/**", "a", True),
    ("a/**", "a/b", True),
    ("a/**", "a/b/c", True),
    ("a/**", "ab/c", False),
    ("**", "anything/at/all", True),
    # Within-segment wildcards, which is how `--repos "*api*"` is meant to work.
    ("*api*", "payments-api", True),
    ("api", "payments-api", False),
    ("a*b*c", "axxbyyc", True),
    ("a*b*c", "axxcyyb", False),
    # A wildcard in the middle of a path.
    ("a/*/c", "a/b/c", True),
    ("a/*/c", "a/b/d", False),
])
def test_glob_is_segment_wise(pattern, repo, expected):
    assert scope.match_repo(pattern, repo) is expected


def test_glob_never_decides_by_prefix():
    """Property: no pair in the fixture is decided the way `startswith` would decide it.

    Written as a property over pairs rather than one example, because the bug class is
    "somebody swapped the matcher", and one example is one line to special-case.
    """
    ids = ["a", "a/b", "a/bc", "a/b/c", "ab", "ab/c", "team/api", "team/api-v2"]
    disagreements = [
        (p, r) for p in ids for r in ids
        if scope.match_repo(p, r) is not (p == r)
    ]
    # With no wildcard in any pattern, an exact match is the ONLY true answer. A
    # prefix matcher would additionally return True for ('a', 'a/b'), ('a/b','a/bc')
    # and ('team/api', 'team/api-v2').
    assert disagreements == []


# --------------------------------------------------------------------------
# Partition ownership
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("partition", "owner"), [
    ("a/b", "a/b"),
    ("@connect:a/b", "a/b"),
    ("@enrich:a/b", "a/b"),
    ("@wiki:a/b", "a/b"),
    # The open-ended family. One partition per qualifying module, so it can never be
    # written out as a fixed list -- it has to be recognised.
    ("@wiki:a/b::src_core", "a/b"),
    # A module prefix may itself contain the separator; only the FIRST one divides
    # the repo from the module.
    ("@wiki:a/b::mod::sub", "a/b"),
    # Belongs to no repo, so no repo grant can reach it.
    ("@ingest:notes", None),
    # Sentinels are ruled on separately, never by ownership.
    (SHARED_REPO, None),
    (SYSTEM_REPO, None),
    # A family this module has no rule for is owned by nobody, not guessed into one.
    ("@future:a/b", None),
    ("@wiki:", None),
])
def test_partition_repo(partition, owner):
    assert scope.partition_repo(partition) == owner


def test_wiki_partition_is_in_scope_for_its_own_repo():
    """FAILS AGAINST THE PRE-9.3.0 CODE, both copies of it.

    `embeddings.store._repo_scope` and `cmds.forget._partitions` each returned three
    entries -- the literal id, `@connect:` and `@enrich:` -- and neither included
    `@wiki:`. So a repo-scoped search never saw the repo's own generated prose, and
    `kb forget` never deleted it.
    """
    assert "@wiki:a/b" in scope.repo_partitions("a/b")
    assert scope.owns_partition("@wiki:a/b", ["a/b"])


def test_module_partition_follows_its_repo_in_both_directions():
    assert scope.owns_partition("@wiki:a/b::src_core", ["a/b"]) is True
    assert scope.owns_partition("@wiki:a/b::src_core", ["a/c"]) is False


def test_ingest_is_unreachable_by_a_repo_grant():
    """`@ingest:<name>` belongs to no repo, so even `**` does not reach it here."""
    assert scope.owns_partition("@ingest:notes", ["a/**"]) is False
    assert scope.owns_partition("@ingest:notes", ["**"]) is False


def test_an_unrecognised_partition_is_denied_not_allowed():
    """The direction that matters. `visualize/payload.py` has the recorded incident
    where a check covering `(` and missing `@` doubled a fleet count."""
    for weird in ["@future:a/b", "@", "@wiki:", "@enrich:"]:
        assert scope.owns_partition(weird, ["**"]) is False


# --------------------------------------------------------------------------
# Sentinels
# --------------------------------------------------------------------------

def test_every_sentinel_in_the_model_has_a_ruling():
    """Enumerated from `model.py`, so a FIFTH sentinel added there and not ruled on
    here fails this test rather than defaulting to something nobody chose."""
    from contextlake.kb import model

    declared = {
        value for name, value in vars(model).items()
        if name.endswith("_REPO") and isinstance(value, str) and value.startswith("(")
    }
    assert declared == set(scope.KNOWN_SENTINELS), (
        "a sentinel in model.py has no ruling in scope.py, or vice versa")


@pytest.mark.parametrize(("sentinel", "patterns", "external", "visible"), [
    # Visible to everyone, or imports break: these nodes are deduped to one row per
    # store because their id encodes no repo.
    (SHARED_REPO, ["a/b"], False, True),
    (PACKAGES_REPO, ["a/b"], False, True),
    # Connector-fetched third-party content, on its own axis.
    (EXTERNAL_REPO, ["a/b"], False, False),
    (EXTERNAL_REPO, ["a/b"], True, True),
    (EXTERNAL_REPO, ["**"], False, False),
    # Its presence discloses that SOME indexed repo calls an unindexed target, which
    # is information about repositories a narrow scope may not cover.
    (SYSTEM_REPO, ["a/b"], False, False),
    (SYSTEM_REPO, ["a/**"], False, False),
    (SYSTEM_REPO, ["**"], False, True),
])
def test_sentinel_rulings(sentinel, patterns, external, visible):
    assert scope.sentinel_visible(
        sentinel, patterns, external=external) is visible
    assert scope.owns_partition(
        sentinel, patterns, external=external) is visible


def test_an_unknown_sentinel_is_denied():
    assert scope.sentinel_visible("(invented)", ["**"]) is False


# --------------------------------------------------------------------------
# Turning the predicate into an IN-clause list
# --------------------------------------------------------------------------

def test_partitions_in_scope_reaches_the_open_ended_family():
    """The whole point of the design: an `IN (?,...)` list that still covers a family
    whose members cannot be enumerated from the grant alone."""
    known = [
        "a/b", "@wiki:a/b", "@wiki:a/b::src_core", "@wiki:a/b::api",
        "@connect:a/b", "@enrich:a/b",
        "a/c", "@wiki:a/c", "@wiki:a/c::core",
        "@ingest:notes", SHARED_REPO, SYSTEM_REPO,
    ]
    got = scope.partitions_in_scope(known, ["a/b"])
    assert got == ["a/b", "@wiki:a/b", "@wiki:a/b::src_core", "@wiki:a/b::api",
                   "@connect:a/b", "@enrich:a/b", SHARED_REPO]
    # Nothing belonging to the repo that was not granted.
    assert not [p for p in got if "a/c" in p]


def test_partitions_in_scope_preserves_order():
    """SQL parameters stay stable run to run, so a plan and an assertion reproduce."""
    known = ["z/1", "a/1", "m/1"]
    assert scope.partitions_in_scope(known, ["**"]) == known


def test_an_empty_scope_is_unscoped_not_denied():
    """"Nobody wrote a scope" and "scope to nothing" are opposite instructions.

    Same reading `grants._check_tools` gives an absent axis. Collapsing them would
    deny every key issued before the axis existed.
    """
    assert scope.owns_partition("a/b", []) is True
    assert scope.partitions_in_scope(["a/b", "c/d"], []) == ["a/b", "c/d"]


def test_module_partitions_of_finds_only_that_repos_modules():
    known = ["@wiki:a/b", "@wiki:a/b::x", "@wiki:a/b::y", "@wiki:a/bc::z", "@wiki:a/c::x"]
    assert scope.module_partitions_of(known, "a/b") == ["@wiki:a/b::x", "@wiki:a/b::y"]
    # `a/bc` is a different repo and its pages must not come back: the same
    # sibling-prefix trap the glob closes, in the partition-id domain.
    assert "@wiki:a/bc::z" not in scope.module_partitions_of(known, "a/b")


# --------------------------------------------------------------------------
# The builders and the recogniser share one spelling
# --------------------------------------------------------------------------

def test_the_partition_builders_use_scopes_constants():
    """Guards the drift that caused this whole story.

    Three functions BUILD partition ids and this module RECOGNISES them. When those
    were separate string literals, `@wiki:` was written by the wiki command and
    absent from both expanders, so the family was invisible to every reader.
    """
    from contextlake.kb.cmds.wiki import _module_partition_head, _wiki_partition
    from contextlake.kb.connectors.enrich import enrich_partition
    from contextlake.kb.connectors.orchestrate import connect_partition

    assert connect_partition("a/b") == f"{scope.CONNECT_PREFIX}a/b"
    assert enrich_partition("a/b") == f"{scope.ENRICH_PREFIX}a/b"
    assert _wiki_partition("a/b") == f"{scope.WIKI_PREFIX}a/b"
    assert _module_partition_head("a/b") == f"{scope.WIKI_PREFIX}a/b{scope.MODULE_SEP}"
    # And every one of them is recognised by the module that decides scope.
    for built in (connect_partition("a/b"), enrich_partition("a/b"),
                  _wiki_partition("a/b"), _module_partition_head("a/b") + "mod"):
        assert scope.partition_repo(built) == "a/b", built
