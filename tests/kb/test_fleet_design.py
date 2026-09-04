"""The fleet page: what many repositories have in common, and the counting trap under it.

This is the one generated document no per-repo page can produce, because disagreement is
invisible from inside any single repository: a service pinning `>=2.5,<4` and another leaving
the same package unpinned each look reasonable on their own page.

The centre of these tests is a single substitution that would make the page lie. Measured on
a real four-repository fleet before the renderer existed, one package had **11 dependency
edges across 2 repositories** -- one repository declares it in eleven manifests, its own plus
ten bundled examples. Counting edges would have rendered "11 repositories" onto a
four-repository fleet. That is absurd at four and entirely plausible at forty, which is why
it is pinned here rather than left to review.
"""

from __future__ import annotations

from contextlake.kb.docs.fleet import FleetDep, render_fleet_design
from contextlake.kb.docs.stamp import FLEET_REPO, UNKNOWN, fingerprint, read_stamp

R1, R2, R3 = "acme/stations", "acme/readings", "acme/console"


def _dep(pkg, repo, manifest="pyproject.toml", constraint="", group="runtime"):
    return FleetDep(pkg, repo, manifest, constraint, group)


def test_a_population_counts_repositories_not_edges():
    """The measured trap, reduced: one repo declaring one package in five manifests.

    A count of edges says five repositories require it. There are two. The page must say
    two, and report the manifest count as its own separate number rather than letting one
    stand in for the other.
    """
    deps = [_dep("shared", R1, f"examples/{i}/pyproject.toml", ">=1") for i in range(5)]
    deps.append(_dep("shared", R2, "pyproject.toml", ">=1"))
    page = render_fleet_design(deps, repos=[R1, R2])

    row = next(ln for ln in page.splitlines() if ln.startswith("| `shared`"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[1] == "2", f"population is not a repository count: {row}"
    assert cells[2] == "6", f"manifest count is wrong or substituted: {row}"
    assert ">=1` (2)" in cells[3], "the constraint count is not a repository count"
    assert "(6)" not in cells[3], "an edge count leaked into the constraint column"


def test_a_disagreement_is_reported_and_not_judged():
    """The finding the page exists for, and the sentence that keeps it a finding.

    A repository may pin tightly because it met a real incompatibility. Nothing in a graph
    can tell that from drift, so the page names the split and explicitly declines to
    recommend anything.
    """
    deps = [_dep("web", R1, constraint=">=2.0"),
            _dep("web", R2, constraint="==1.8"),
            _dep("web", R3)]
    page = render_fleet_design(deps, repos=[R1, R2, R3])
    assert "1 of 1 packages required at runtime are required by more than one repository" in page
    assert "1 of them are pinned differently across repositories:" in page
    assert "observation, not a recommendation" in page
    # All three constraints appear with their own repository count.
    assert ">=2.0` (1)" in page and "==1.8` (1)" in page and "*unpinned* (1)" in page


def test_agreement_is_stated_rather_than_left_to_inference():
    """Silence would read as "not checked". The page says it checked and found none."""
    deps = [_dep("web", R1, constraint=">=2.0"), _dep("web", R2, constraint=">=2.0")]
    page = render_fleet_design(deps, repos=[R1, R2])
    assert "Every shared package is pinned identically everywhere it appears." in page
    assert "pinned differently" not in page


def test_only_runtime_and_peer_reach_the_fleet_view():
    """A dev dependency disagreeing across the fleet is a different, lesser finding.

    Mixing it in would bury the one that matters, and would also inflate the shared count
    with tooling every repository happens to install.
    """
    deps = [_dep("linter", R1, group="dev", constraint=">=1"),
            _dep("linter", R2, group="dev", constraint=">=2"),
            _dep("extra", R1, group="optional:test"),
            _dep("extra", R2, group="optional:test")]
    page = render_fleet_design(deps, repos=[R1, R2])
    assert "linter" not in page and "extra" not in page
    assert "No repository in this store records a runtime dependency" in page


def test_absence_is_split_into_its_THREE_different_causes():
    """A repository can be missing from the tables for three unrelated reasons.

    It declares only dev dependencies (a manifest WAS read, nothing in it runs). It declares
    nothing this reads (either nothing, or in a file not understood). Or its shard could not
    be loaded, in which case this page knows nothing about it either way. The first draft
    printed all three under one heading reading "no recorded commitments", which made a repo
    with a dev-only manifest indistinguishable from a broken store entry.

    Named rather than counted throughout, because a count invites the reader to guess which.
    """
    deps = [_dep("web", R1, constraint=">=2.0"),
            _dep("linter", R2, group="dev", constraint=">=1")]
    page = render_fleet_design(deps, repos=[R1, R2, R3], unreadable=["acme/broken"])

    assert "4 of 4 are absent" not in page          # R1 committed; it is not absent
    assert "3 of 4 are absent" in page
    assert "Declare only development or opt-in dependencies** (1)" in page
    assert "Declare no dependency this reads** (1)" in page
    assert "Could not be read** (1)" in page
    # each named under its own cause, and never under another
    dev, _, rest = page.partition("**Declare no dependency this reads**")
    unread_section = page.partition("**Could not be read**")[2]
    assert f"- `{R2}`" in dev and f"- `{R2}`" not in rest
    assert f"- `{R3}`" in rest and f"- `{R3}`" not in unread_section
    assert "- `acme/broken`" in unread_section
    assert f"- `{R1}`" not in page


def test_an_unreadable_repo_is_not_reported_as_declaring_nothing():
    """The distinction the section exists for, isolated.

    `kb docs` already counts unreadable shards; without passing them here the fleet page
    would file one under "declares no dependency" and state something it cannot know.
    """
    page = render_fleet_design([_dep("web", R1, constraint=">=1")],
                               repos=[R1], unreadable=["acme/broken"])
    assert "Could not be read** (1)" in page
    assert "a store to repair, not a finding about the code" in page
    assert "Declare no dependency this reads" not in page


def test_the_denominator_names_the_filter_it_was_drawn_from():
    """"3 of 15 packages" would silently redefine "packages" as "runtime packages".

    A fleet with 15 runtime and 200 development packages reads as a 15-package fleet, and
    nothing else on the page contradicts it. The excluded population is stated instead.
    """
    deps = [_dep("web", R1, constraint=">=1"), _dep("web", R2, constraint=">=1")]
    deps += [_dep(f"tool{i}", R1, group="dev", constraint=">=1") for i in range(7)]
    page = render_fleet_design(deps, repos=[R1, R2])
    assert "1 of 1 packages required at runtime" in page
    assert "A further 7 appear only as development or opt-in dependencies" in page
    assert "the runtime population rather than every package the fleet mentions" in page


def test_a_package_in_one_repository_is_not_shared():
    """The whole point of the shared table is more than one. One is not a share."""
    deps = [_dep("solo", R1, constraint=">=1"), _dep("other", R2, constraint=">=1")]
    page = render_fleet_design(deps, repos=[R1, R2])
    assert "required by exactly one repository" in page
    assert "Nothing is shared, so nothing can disagree." in page
    assert "| `solo`" not in page


def test_an_empty_store_names_what_it_reads():
    """As on the per-repo page: an empty list is ambiguous between two different facts."""
    page = render_fleet_design([], repos=[R1, R2])
    assert "No repository in this store records a runtime dependency" in page
    assert "pyproject.toml" in page and "package.json" in page


def test_the_page_carries_a_fingerprint_of_every_member():
    """It cannot carry one commit, so it carries a hash of all of them.

    It used to carry NOTHING and explain in prose that it spanned many commits. That is
    honest to a human and useless to a program, which is the whole gap `docs/stamp.py`
    exists to close -- and it mattered the moment an MCP tool could return this page.
    """
    page = render_fleet_design(
        [_dep("web", R1)], repos=[R1],
        members=[(R1, "abc123", "10"), ("other", "def456", "10")])
    marker = read_stamp(page)
    assert marker is not None, "the page carries no machine-readable stamp"
    kind, repo, value = marker
    assert kind == "fleet", kind
    assert repo == FLEET_REPO, repo
    assert value == fingerprint([(R1, "abc123", "10"), ("other", "def456", "10")])
    # The human sentence must not call a fingerprint a commit. The marker and the prose
    # disagreeing is exactly what `stamp` says to avoid.
    assert "at fingerprint" in page
    assert "at commit" not in page


def test_a_member_moving_changes_the_fingerprint():
    """The point of stamping it. If this did not move, the stamp would be decoration."""
    before = render_fleet_design([_dep("web", R1)], repos=[R1],
                                 members=[(R1, "abc123", "10")])
    after = render_fleet_design([_dep("web", R1)], repos=[R1],
                                members=[(R1, "ZZZ999", "10")])
    assert read_stamp(before)[2] != read_stamp(after)[2]


def test_a_parser_bump_alone_changes_the_fingerprint():
    """A page can go stale without a single commit moving: the parser changes what is
    extracted from the same code. That happened twice in one day on this project."""
    before = render_fleet_design([_dep("web", R1)], repos=[R1],
                                 members=[(R1, "abc123", "9")])
    after = render_fleet_design([_dep("web", R1)], repos=[R1],
                                members=[(R1, "abc123", "10")])
    assert read_stamp(before)[2] != read_stamp(after)[2]


def test_the_fingerprint_does_not_depend_on_member_order():
    """The store does not promise an order, and a page that re-fingerprints on every run
    because the rows came back differently would report a fresh store as stale."""
    a = render_fleet_design([_dep("web", R1)], repos=[R1],
                            members=[(R1, "a", "10"), ("z", "b", "10")])
    b = render_fleet_design([_dep("web", R1)], repos=[R1],
                            members=[("z", "b", "10"), (R1, "a", "10")])
    assert read_stamp(a)[2] == read_stamp(b)[2]


def test_a_member_with_no_head_is_stamped_unknown_not_dropped():
    """Dropping it would make a store with an unindexed member fingerprint identically to
    one that does not have that member at all -- two different stores, one hash."""
    with_unindexed = render_fleet_design(
        [_dep("web", R1)], repos=[R1], members=[(R1, "a", "10"), ("z", None, None)])
    without = render_fleet_design([_dep("web", R1)], repos=[R1], members=[(R1, "a", "10")])
    assert read_stamp(with_unindexed)[2] != read_stamp(without)[2]


def test_no_members_stamps_unknown_rather_than_leaving_the_page_unstamped():
    """An absent marker reads as "nothing to report"; a present `unknown` reads as
    "checked, could not tell". A consumer defaults to fresh on the first and stale on the
    second, which is the difference this whole module is about."""
    page = render_fleet_design([_dep("web", R1)], repos=[R1])
    marker = read_stamp(page)
    assert marker is not None
    assert marker[2] == UNKNOWN
    # And the page still says in prose that nobody wrote it. The stamp is for a
    # program; this sentence is what stops a human reading it as a design authority.
    assert "Nobody wrote this page" in page


def test_the_shared_table_is_bounded_and_says_so():
    deps = []
    for i in range(30):
        deps += [_dep(f"pkg{i:02d}", R1, constraint=">=1"),
                 _dep(f"pkg{i:02d}", R2, constraint=">=1")]
    page = render_fleet_design(deps, repos=[R1, R2], max_shared=10)
    assert "30 of 30 packages required at runtime are required by more than one repository" in page
    assert "20 more shared packages are not listed." in page
