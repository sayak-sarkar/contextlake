"""What a `depends_on` edge records beyond the package's name.

A manifest dependency is the strongest evidence the graph holds, because somebody wrote it
in a file on purpose rather than it being inferred from a parse tree. It was also the
thinnest: the edge recorded a name, cited line 1 of the manifest whatever the manifest said,
and folded every group into one relation. Measured on a real public tree, that made an
opt-in extra indistinguishable from a dependency the package cannot start without.

Three facts are now carried, and each has a way of being wrong that looks right:

- **the constraint as written**, so `>=1.9.0` survives rather than being parsed off. It is
  deliberately not interpreted: nothing here decides whether a version satisfies a range,
  so the author's own text is both the honest record and the useful one.
- **the group**, separating a hard runtime requirement from a dev, peer or optional one.
- **the declaring line**, which is the fact most easily faked. A citation pointing at the
  wrong line is worse than no line, because it reads as precise. The package listed in two
  groups is the case that catches it: searching from the start of the file makes the second
  one cite the first one's line, and the number still looks plausible.
"""

from __future__ import annotations

import pytest

from contextlake.kb.manifest import parse_manifest
from contextlake.kb.model import Confidence

PYPROJECT = (
    b'[project]\n'                      # 1
    b'name = "demo"\n'                  # 2
    b'dependencies = [\n'               # 3
    b'    "blinker>=1.9.0",\n'          # 4
    b'    "celery[redis]>=5.0",\n'      # 5
    b']\n'                              # 6
    b'\n'                               # 7
    b'[project.optional-dependencies]\n'  # 8
    b'async = ["asgiref>=3.2"]\n'       # 9
    b'dotenv = ["python-dotenv"]\n'     # 10
)

PACKAGE_JSON = (
    b'{\n'                                                          # 1
    b'  "name": "demo",\n'                                          # 2
    b'  "dependencies": { "express": "^4.17.1" },\n'                # 3
    b'  "devDependencies": { "jest": "^29", "express": "^4.17.1" },\n'  # 4
    b'  "peerDependencies": { "react": ">=18" }\n'                  # 5
    b'}\n'
)


def _deps(rel_path: str, content: bytes) -> dict[str, tuple[str, str, int]]:
    """`{package name: (group, constraint, line)}` for every depends_on edge."""
    nodes, edges = parse_manifest("r", rel_path, content)
    name_of = {n.id: n.name for n in nodes}
    return {name_of[e.dst]: (e.attrs.get("group", ""), e.attrs.get("constraint", ""),
                             e.provenance.source_line)
            for e in edges if e.relation == "depends_on"}


def test_a_pyproject_dependency_carries_its_constraint_group_and_line():
    got = _deps("pyproject.toml", PYPROJECT)
    assert got["blinker"] == ("runtime", ">=1.9.0", 4)
    assert got["asgiref"] == ("optional:async", ">=3.2", 9)


def test_an_extra_is_not_a_runtime_dependency():
    """The distinction this whole record exists for.

    `asgiref` sits behind an extra a user opts into; `blinker` is required to import the
    package at all. Flattened into one relation they read identically, and a reader of the
    generated design document cannot tell which choices are load-bearing.
    """
    got = _deps("pyproject.toml", PYPROJECT)
    assert got["blinker"][0] == "runtime"
    assert got["asgiref"][0] == "optional:async"
    assert got["python-dotenv"][0] == "optional:dotenv"
    # Two extras are two different groups, not one bucket called "optional".
    assert got["asgiref"][0] != got["python-dotenv"][0]


def test_a_dependency_with_no_constraint_records_no_constraint():
    """Absence is recorded as absence, not as an invented range.

    `python-dotenv` is written with no version at all. The key must be missing rather than
    present-and-empty, so a consumer asking "is this pinned" gets a truthful no.
    """
    _, edges = parse_manifest("r", "pyproject.toml", PYPROJECT)
    by_line = {e.provenance.source_line: e for e in edges if e.relation == "depends_on"}
    assert "constraint" not in by_line[10].attrs
    assert by_line[9].attrs["constraint"] == ">=3.2"


def test_extras_and_markers_are_kept_verbatim_rather_than_parsed():
    """`celery[redis]>=5.0` records `[redis]>=5.0`.

    The remainder is stored as the author wrote it. Splitting extras out of the constraint
    would mean deciding what an extra means, and nothing downstream needs that decision.
    """
    assert _deps("pyproject.toml", PYPROJECT)["celery"][1] == "[redis]>=5.0"


def test_the_same_package_in_two_groups_cites_each_group_own_line():
    """The citation test that a "line is not 1" assertion would pass while broken.

    `express` appears in both `dependencies` (line 3) and `devDependencies` (line 4). A
    search that starts at the beginning of the file finds the line 3 occurrence both times,
    so the dev entry cites the runtime declaration: a precise-looking number pointing at a
    different fact. Both entries survive as separate edges, each with its own line.
    """
    _, edges = parse_manifest("r", "package.json", PACKAGE_JSON)
    name_of = {n.id: n.name for n in parse_manifest("r", "package.json", PACKAGE_JSON)[0]}
    express = sorted((e.attrs["group"], e.provenance.source_line)
                     for e in edges
                     if e.relation == "depends_on" and name_of[e.dst] == "express")
    assert express == [("dev", 4), ("runtime", 3)]


def test_pep_735_dependency_groups_are_read():
    """`[dependency-groups]` is a sibling of `[project]`, not a key inside it.

    Reading only `[project]` meant a project that declares everything this way reported
    ZERO dependencies. Measured on a public Django application: 0 before, 137 after. A
    zero is the worst possible failure for a section whose whole job is to list recorded
    choices, because an empty list reads as "this project made none".

    Kept under its own `group:` prefix rather than folded into `optional:`, because the
    two are different mechanisms: an extra is published in the package's metadata and a
    user can install it, a dependency group is local to the checkout and never published.
    """
    content = (b'[project]\n'                        # 1
               b'name = "demo"\n'                    # 2
               b'\n'                                 # 3
               b'[dependency-groups]\n'              # 4
               b'prod = [\n'                         # 5
               b'  # a comment between the specs\n'  # 6
               b'  "web[extra]==5.2.*",\n'           # 7
               b'  "queue",\n'                       # 8
               b']\n'                                # 9
               b'dev = ["linter>=1"]\n')             # 10
    got = _deps("pyproject.toml", content)
    assert got["web"] == ("group:prod", "[extra]==5.2.*", 7)
    assert got["queue"] == ("group:prod", "", 8)
    assert got["linter"] == ("group:dev", ">=1", 10)


def test_an_include_group_entry_is_not_a_package():
    """PEP 735 lets a group pull in another group as a table, not a string.

    `{include-group = "prod"}` names a group, so treating it as a spec would invent a
    package node for something that is not a package.
    """
    content = (b'[project]\nname = "demo"\n'
               b'[dependency-groups]\n'
               b'prod = ["web"]\n'
               b'dev = [{include-group = "prod"}, "linter"]\n')
    got = _deps("pyproject.toml", content)
    assert set(got) == {"web", "linter"}


def test_a_bare_name_does_not_match_inside_a_longer_name():
    """The bug this shape produced on a real tree, reduced to its smallest form.

    A project called `demo-example-worker` that depends on `demo` has the string `demo`
    on its own `name =` line, several lines above the dependency. Searching for the bare
    name found that line first and cited it, so the citation named the project's own
    name declaration instead of the dependency: a precise-looking line number pointing at
    a different fact. Found by reading generated output against the file it came from, not
    by any assertion -- a "line is not 1" check passes happily on the wrong line.
    """
    content = (b'[project]\n'                            # 1
               b'name = "demo-example-worker"\n'         # 2
               b'version = "1.0.0"\n'                    # 3
               b'dependencies = ["demo", "queue[redis]"]\n')  # 4
    got = _deps("pyproject.toml", content)
    assert got["demo"] == ("runtime", "", 4), "cited its own name line, not the dependency"
    assert got["queue"] == ("runtime", "[redis]", 4)


def test_npm_groups_map_to_the_shared_vocabulary():
    got = _deps("package.json", PACKAGE_JSON)
    assert got["jest"] == ("dev", "^29", 4)
    assert got["react"] == ("peer", ">=18", 5)


def test_a_package_reference_is_read_whatever_order_its_attributes_are_in():
    """MSBuild writes `Include` and `Version` in either order.

    The previous pattern anchored on `Include` and could only reach what followed it, so a
    `Version`-first reference lost its version silently while still producing an edge.
    """
    content = (b'<Project>\n'
               b'  <ItemGroup>\n'
               b'    <PackageReference Include="Serilog" Version="3.1.1" />\n'
               b'    <PackageReference Version="8.0.0" Include="Polly" />\n'
               b'  </ItemGroup>\n'
               b'</Project>\n')
    got = _deps("App.csproj", content)
    assert got["Serilog"] == ("runtime", "3.1.1", 3)
    assert got["Polly"] == ("runtime", "8.0.0", 4)


def test_a_nuget_version_written_as_a_child_element_is_still_a_version():
    """MSBuild accepts both spellings, and reading one made a pin look like no pin.

    The contract here is that an absent `constraint` key means the manifest pinned
    nothing. Reading only the attribute broke that: a package pinned via a `<Version>`
    child arrived with no key, so a consumer asking "is this pinned" got a confident and
    wrong no. The same shape as a missing field reading as a passed check, in the data
    model rather than in a test.
    """
    content = (b'<Project>\n'                                    # 1
               b'  <ItemGroup>\n'                                # 2
               b'    <PackageReference Include="Serilog">\n'     # 3
               b'      <Version>3.1.1</Version>\n'               # 4
               b'    </PackageReference>\n'                      # 5
               b'    <PackageReference Include="Polly" />\n'     # 6
               b'  </ItemGroup>\n'
               b'</Project>\n')
    got = _deps("App.csproj", content)
    assert got["Serilog"] == ("runtime", "3.1.1", 3)
    # And the version does not migrate: the self-closing reference after it is unpinned,
    # which is what the lookahead bound exists to guarantee.
    assert got["Polly"] == ("runtime", "", 6)


@pytest.mark.parametrize("scope,expected", [
    (b"<scope>test</scope>", "dev"),
    (b"<scope>provided</scope>", "dev"),
    (b"<scope>compile</scope>", "runtime"),
    (b"", "runtime"),
    (b"<optional>true</optional>", "optional"),
])
def test_maven_states_its_group_inside_the_block(scope, expected):
    """Maven puts the group in the dependency, not in which list it sits in.

    So the mapping happens here rather than at the call site, and lands on the same
    vocabulary the other three ecosystems produce -- otherwise a consumer would need to
    know which ecosystem it is reading before it could ask "is this a real dependency".
    """
    content = (b'<project>\n'
               b'  <artifactId>demo</artifactId>\n'
               b'  <dependencies>\n'
               b'    <dependency><groupId>org.x</groupId><artifactId>lib</artifactId>'
               b'<version>1.2</version>' + scope + b'</dependency>\n'
               b'  </dependencies>\n'
               b'</project>\n')
    assert _deps("pom.xml", content)["org.x:lib"] == (expected, "1.2", 4)


def test_a_manifest_that_states_nothing_extra_still_parses():
    """No constraint, no version attribute, nothing unusual: the edge still exists.

    Guards against the enrichment becoming a requirement -- a dependency written bare is
    still a dependency, and dropping it would be a silent regression in coverage.
    """
    got = _deps("package.json", b'{"name":"d","dependencies":{"lodash":""}}')
    assert got["lodash"] == ("runtime", "", 1)


def test_dependency_edges_stay_extracted_confidence():
    """Recorded, not inferred. Everything downstream filters on this."""
    _, edges = parse_manifest("r", "pyproject.toml", PYPROJECT)
    assert edges and all(e.confidence is Confidence.EXTRACTED for e in edges)
