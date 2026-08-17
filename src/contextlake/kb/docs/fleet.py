"""What the whole fleet commits to, which no single repository's page can answer.

Every other generated document describes one repository. This one exists because the graph
keeps **package nodes global**, keyed by ecosystem and name rather than by repo, so two
repositories depending on the same package point at the same node. That shared node is the
only place a question like "how many of our services require this, and do they agree on the
version" is answerable at all.

The answer is worth having because disagreement is invisible from inside any one repo. A
service pinning `>=2.5,<4` and another leaving the same package unpinned each look
reasonable on their own page; only the fleet view shows they differ.

**It reports the disagreement and does not judge it.** Nothing here knows whether a split
was deliberate -- a repository may pin tightly because it hit a real incompatibility -- so
the page states which constraints are in use and by how many repositories, and stops. That
is the same rule the per-repo page follows for constants.

## The counting trap this module was written around

Measured on a real four-repository fleet before any of this was written: one package showed
**11 dependency edges across 2 repositories**, because one of those repositories declares it
in eleven manifests (its own, plus ten bundled examples). A count of edges would have
rendered "11 repositories" onto a four-repository fleet.

That absurdity is only obvious at this scale. On a forty-repository fleet, "11" reads as
perfectly plausible and nothing on the page would contradict it. So every population here is
a count of **distinct repositories**, manifests are reported as their own separate number,
and the two are never substituted for each other.
"""

from __future__ import annotations

import collections
from typing import NamedTuple

from ..mdwrite import code, table

# Only what a repository cannot run without. A dev dependency disagreeing across the fleet is
# not the same finding, and mixing the two would bury the one that matters. Same vocabulary
# and same reasoning as the per-repo page's decision records.
FLEET_GROUPS = frozenset({"runtime", "peer"})

DEFAULT_MAX_SHARED = 100
DEFAULT_MAX_NAMED_REPOS = 12


class FleetDep(NamedTuple):
    """One dependency as one manifest in one repository declares it."""

    package: str
    repo: str
    manifest: str
    constraint: str    # "" when the manifest pinned nothing
    group: str


UNPINNED = "*unpinned*"


def _by_package(deps):
    """`{package: {constraint: {repos}}}` plus `{package: {(repo, manifest)}}`.

    Two separate structures on purpose. Collapsing them into one count is exactly the
    substitution the module docstring describes, and keeping them apart makes it impossible
    to print one while meaning the other.
    """
    constraints: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    manifests: dict = collections.defaultdict(set)
    for d in deps:
        if d.group not in FLEET_GROUPS:
            continue
        constraints[d.package][d.constraint or UNPINNED].add(d.repo)
        manifests[d.package].add((d.repo, d.manifest))
    return constraints, manifests


def _repos_of(by_constraint) -> set:
    return {r for repos in by_constraint.values() for r in repos}


def _constraint_cell(by_constraint) -> str:
    """`>=2.0 (9), unpinned (1)`, where each number is REPOSITORIES."""
    parts = sorted(by_constraint.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return ", ".join(f"{code(c) if c != UNPINNED else c} ({len(repos)})" for c, repos in parts)


def render_fleet_design(deps, *, repos, max_shared: int = DEFAULT_MAX_SHARED,
                        max_named_repos: int = DEFAULT_MAX_NAMED_REPOS) -> str:
    """The fleet page as Markdown. ``repos`` is every indexed repo id, including any with
    no dependencies at all, because a repository missing from a table is a different fact
    from a repository with nothing to declare."""
    all_repos = sorted(set(repos))
    constraints, manifests = _by_package(list(deps))
    shared = {p: c for p, c in constraints.items() if len(_repos_of(c)) > 1}
    disagreeing = sorted(p for p, c in shared.items() if len(c) > 1)

    lines = [
        f"# Fleet design notes: {len(all_repos)} repositories",
        "",
        "**Nobody wrote this page.** It reads every indexed repository's manifests and reports "
        "what they have in common. Each repository's own design notes carry the commit they "
        "were generated from; this page spans many commits, so treat it as a view over the "
        "store as last indexed rather than a snapshot of any one repository.",
        "",
        "Every population below counts **distinct repositories**. Manifests are counted "
        "separately and the two are never mixed: one repository can declare the same package "
        "in a dozen manifests, and counting those as a dozen repositories would report a "
        "number larger than the fleet.",
        "",
    ]
    if not constraints:
        lines += ["No repository in this store records a runtime dependency in a manifest this "
                  "reads. Either none declares one, or they declare them somewhere not yet "
                  "read: `pyproject.toml` (including PEP 735 `[dependency-groups]`), "
                  "`package.json`, `*.csproj` and `pom.xml` are what is understood.", ""]
        return "\n".join(lines).rstrip() + "\n"

    lines += ["## Shared commitments", ""]
    if not shared:
        # Stated rather than left as an empty section: on a fleet of unrelated projects this
        # is the ordinary answer and it is a finding, not a gap in the report.
        lines += [f"Every one of the {len(constraints)} packages required across this fleet is "
                  f"required by exactly one repository. Nothing is shared, so nothing can "
                  f"disagree.", ""]
    else:
        by_reach = sorted(shared, key=lambda p: (-len(_repos_of(shared[p])), p))
        shown = by_reach[:max_shared]
        lines += [
            f"{len(shared)} of {len(constraints)} required packages are required by more than "
            f"one repository.", "",
        ]
        lines += table(
            ["Package", "Repos", "Manifests", "Constraints in use (repos)"],
            [(code(p), str(len(_repos_of(shared[p]))), str(len(manifests[p])),
              _constraint_cell(shared[p])) for p in shown])
        if len(shown) < len(by_reach):
            lines += ["", f"{len(by_reach) - len(shown)} more shared packages are not listed."]
        lines.append("")
        if disagreeing:
            lines += [
                f"**{len(disagreeing)} of them are pinned differently across repositories:** "
                + ", ".join(code(p) for p in disagreeing[:max_shared]) + ".",
                "",
                "That is an observation, not a recommendation. A repository may pin tightly "
                "because it met a real incompatibility, and nothing here can tell a deliberate "
                "split from a drifted one.",
                "",
            ]
        else:
            lines += ["Every shared package is pinned identically everywhere it appears.", ""]

    # Named, not counted. "6 repositories declare nothing" invites the reader to assume which
    # six, and a repository absent from every table above is indistinguishable from one this
    # simply failed to read.
    silent = [r for r in all_repos
              if r not in {d for c in constraints.values() for s in c.values() for d in s}]
    lines += ["## Repositories with no recorded commitments", ""]
    if not silent:
        lines += ["Every indexed repository declares at least one runtime dependency.", ""]
    else:
        named = silent[:max_named_repos]
        lines += [
            f"{len(silent)} of {len(all_repos)} declare no runtime dependency in a manifest "
            f"this reads. Named rather than counted, because a repository absent from the "
            f"tables above cannot otherwise be told from one that was not read:",
            "",
        ]
        lines += [f"- {code(r)}" for r in named]
        if len(named) < len(silent):
            lines += [f"- ... and {len(silent) - len(named)} more"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
