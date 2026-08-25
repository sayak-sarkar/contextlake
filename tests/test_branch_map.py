"""Per-repository branch pins: `branch_map`, the exception list `branch` had no room for.

`branch` pins the WHOLE fleet to one name, which is the common case and not the only one. A
fleet where one team tracks `develop`, the legacy tree sits on `maintenance` and everything
else follows the most-active selection had no way to say so in config.

`branch_map` is a comma-separated list of `pattern=branch` pairs using the same glob syntax
as `repo_filter`, so a user writes one kind of pattern for both. Precedence, narrowest first:

    branch_map (first matching pattern)  ->  branch  ->  most-active selection

A repository whose mapped branch does not exist keeps the existing `branch` behaviour: it is
reported as its own `unpinned` outcome, never silently switched to something else.
"""

from __future__ import annotations

import subprocess

import pytest

from contextlake.core import branch_map_pairs, resolve_branch_pin, switch_repository_branch


def cfg(**kw):
    return {"branch": "", **kw}


class TestParsing:
    def test_pairs_keep_their_written_order(self):
        c = cfg(branch_map="team/api=develop, legacy-*=maintenance, acme/x=spike")
        assert branch_map_pairs(c) == [
            ("team/api", "develop"), ("legacy-*", "maintenance"), ("acme/x", "spike")]

    def test_whitespace_around_pairs_and_halves_is_ignored(self):
        assert branch_map_pairs(cfg(branch_map="  a = b ,c=d  ")) == [("a", "b"), ("c", "d")]

    def test_malformed_entries_are_dropped_not_guessed_at(self):
        # Pinning the wrong branch is worse than not pinning one.
        c = cfg(branch_map="noequals, =nobranch, nopattern=, ok=main")
        assert branch_map_pairs(c) == [("ok", "main")]

    def test_unset_is_empty(self):
        assert branch_map_pairs(cfg()) == []
        assert branch_map_pairs({}) == []


class TestResolution:
    def test_a_glob_pins_the_repos_it_matches(self):
        c = cfg(branch_map="legacy-*=maintenance")
        assert resolve_branch_pin("acme/legacy-billing", "legacy-billing", c) == "maintenance"

    def test_an_unmatched_repo_gets_no_pin(self):
        c = cfg(branch_map="legacy-*=maintenance")
        assert resolve_branch_pin("acme/modern", "modern", c) == ""

    def test_it_falls_back_to_the_fleet_wide_branch(self):
        c = cfg(branch="release/24.1", branch_map="legacy-*=maintenance")
        assert resolve_branch_pin("acme/legacy-x", "legacy-x", c) == "maintenance"
        assert resolve_branch_pin("acme/normal", "normal", c) == "release/24.1"

    def test_first_match_wins_so_specific_can_precede_general(self):
        c = cfg(branch_map="team/api=pinned, team/*=general")
        assert resolve_branch_pin("acme/team/api", "team/api", c) == "pinned"
        assert resolve_branch_pin("acme/team/web", "team/web", c) == "general"

    def test_it_matches_the_group_qualified_path_too(self):
        # Same two-spelling rule `repo_filter` uses.
        c = cfg(branch_map="acme/team/api=develop")
        assert resolve_branch_pin("acme/team/api", "team/api", c) == "develop"

    def test_nothing_configured_changes_nothing(self):
        assert resolve_branch_pin("acme/x", "x", cfg()) == ""
        assert resolve_branch_pin("acme/x", "x", cfg(branch="develop")) == "develop"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def fleet(tmp_path):
    """Two repos with real `origin/*` branches.

    `_collect_branch_info` reads `refs/remotes/origin/`, not local branches, so a fixture
    without a remote reports "No branches found" for every case and the whole test passes
    while proving nothing. That happened while writing this.
    """
    origins, wd = tmp_path / "origins", tmp_path / "wd"
    origins.mkdir()
    wd.mkdir()
    for name in ("alpha-api", "beta-web"):
        bare = origins / f"{name}.git"
        _git(tmp_path, "init", "-q", "--bare", str(bare))
        _git(tmp_path, "clone", "-q", str(bare), str(wd / name))
        repo = wd / name
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        _git(repo, "push", "-q", "origin", "HEAD:master")
        _git(repo, "checkout", "-q", "-b", "develop")
        (repo / "b.txt").write_text("y\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "dev")
        _git(repo, "push", "-q", "origin", "develop")
        # A THIRD branch, newer than develop, so it wins the most-active selection. Without
        # it the unmapped repo also lands on develop and the assertion below cannot tell a
        # working branch_map from the default -- the fixture would pass either way.
        _git(repo, "checkout", "-q", "-b", "maintenance")
        (repo / "c.txt").write_text("z\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "mnt")
        _git(repo, "push", "-q", "origin", "maintenance")
        _git(repo, "checkout", "-q", "master")
        _git(repo, "fetch", "-q", "origin")
    return str(wd)


PROJECTS = {n: {"archived": False, "full_path": f"acme/{n}"} for n in ("alpha-api", "beta-web")}
BASE = {"dry_run": "true", "protect_working_branches": "false"}


@pytest.mark.slow
def test_branch_map_reaches_the_switch_not_just_the_resolver(fleet):
    """Guards the WIRING, which the unit tests above cannot see.

    Reverting `switch_repository_branch` to read `config["branch"]` directly leaves every
    test above green, because they call `resolve_branch_pin` themselves. Only this one fails.
    """
    cfg = dict(BASE, branch_map="alpha-api=develop")
    got = {r: switch_repository_branch(r, PROJECTS, fleet, cfg)[2] for r in PROJECTS}
    # The mapped repo takes its mapped branch; the unmapped one takes the most-active
    # (maintenance). These differ, which is the whole point of the third branch.
    assert "develop" in got["alpha-api"], got
    assert "maintenance" in got["beta-web"], got


@pytest.mark.slow
def test_a_mapped_branch_that_does_not_exist_is_reported_not_swapped(fleet):
    state, _, msg = switch_repository_branch(
        "alpha-api", PROJECTS, fleet, dict(BASE, branch_map="alpha-api=nosuchbranch"))
    assert state == "unpinned", (state, msg)
    assert "nosuchbranch" in msg
