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

R1, R2, R3 = "acme/orders", "acme/billing", "acme/web"


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
    assert "1 of 1 required packages are required by more than one repository" in page
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


def test_repositories_with_nothing_recorded_are_NAMED_not_counted():
    """"2 repositories declare nothing" invites the reader to guess which two.

    And a repository absent from every table cannot otherwise be told from one this failed
    to read at all, which is a different and more serious situation.
    """
    deps = [_dep("web", R1, constraint=">=2.0")]
    page = render_fleet_design(deps, repos=[R1, R2, R3])
    assert "2 of 3 declare no runtime dependency" in page
    assert f"- `{R2}`" in page and f"- `{R3}`" in page
    assert f"- `{R1}`" not in page


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


def test_the_page_says_it_spans_many_commits():
    """It cannot carry one commit stamp, so it says why rather than carrying none silently.

    Every per-repo document records the commit it describes. This one is a view over the
    store as last indexed, and a reader comparing it against a single repository's page
    needs to know the difference.
    """
    page = render_fleet_design([_dep("web", R1)], repos=[R1])
    assert "spans many commits" in page
    assert "as last indexed" in page


def test_the_shared_table_is_bounded_and_says_so():
    deps = []
    for i in range(30):
        deps += [_dep(f"pkg{i:02d}", R1, constraint=">=1"),
                 _dep(f"pkg{i:02d}", R2, constraint=">=1")]
    page = render_fleet_design(deps, repos=[R1, R2], max_shared=10)
    assert "30 of 30 required packages are required by more than one repository" in page
    assert "20 more shared packages are not listed." in page
