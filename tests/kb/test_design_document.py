"""The design document: what it prints, and every way it could lie while looking right.

The page makes claims about a repository that nobody in that repository wrote, so the
failure mode is not a crash. It is a plausible sentence. These tests pin the properties that
keep it honest:

- the *proposed, never ratified* status survives being PARSED, not only read;
- every filtered list states its denominator, because a short list with no denominator reads
  as "there is little here" rather than "most of it did not clear the bar";
- an AMBIGUOUS reading never reaches a printed count, since the graph attributes one site to
  every same-named definition and summing those reports three times the real number;
- an empty dependency list says which manifests were read, because "this project depends on
  nothing" is a claim and "I did not read that file" is a different one;
- nothing on the page characterises a symbol, only counts it.

Each of these was checked by breaking it: the filter reverted, the fixture re-run, and the
printed numbers confirmed to MOVE. A guard whose numbers do not move is measuring nothing.
"""

from __future__ import annotations

from datetime import date

from contextlake.kb.docs.design import (
    MAX_DECLARATION_SHOWN,
    STATUS_MARKER,
    render_design_document,
)
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.shards import GraphShard

REPO = "acme/orders"
_DATE = date(2026, 1, 1)


def _prov(file, line):
    return Provenance(source_file=file, source_line=line, verified_at=_DATE)


def _const(nid, name, declaration, file="config.py", line=1):
    return Node(id=nid, repo=REPO, kind="global_variable", name=name, file=file,
                line_start=line, attrs={"declaration": declaration})


def _uses(dst, file, line, confidence=Confidence.INFERRED):
    return Edge(src=f"file_{file}", dst=dst, relation="uses", confidence=confidence,
                provenance=_prov(file, line))


def _pkg(name):
    return Node(id=f"pkg_pypi_{name}", repo="(packages)", kind="package", name=name)


def _depends(pkg, manifest, line, group="runtime", constraint="",
             confidence=Confidence.EXTRACTED):
    attrs = {"group": group}
    if constraint:
        attrs["constraint"] = constraint
    return Edge(src=f"file_{manifest}", dst=f"pkg_pypi_{pkg}", relation="depends_on",
                confidence=confidence, attrs=attrs, provenance=_prov(manifest, line))


def _shard(nodes, edges):
    return GraphShard(repo=REPO, head_commit="abc123", parser_version="8",
                      nodes=nodes, edges=edges)


def _full_shard():
    """One constant that clears every threshold, one of each way to fail them, and two
    manifests so the nested-project separation is exercised."""
    nodes = [
        _const("c_retry", "MAX_RETRY", "MAX_RETRY = 3"),
        _const("c_one_file", "LOCAL_ONLY", "LOCAL_ONLY = 1"),
        _const("c_rare", "RARELY_READ", "RARELY_READ = 2"),
        _const("c_nodecl", "NO_DECLARATION", ""),
        _pkg("blinker"), _pkg("pytest"), _pkg("orders"),
    ]
    edges = [
        # clears everything: 4 sites across 3 files
        _uses("c_retry", "a.py", 10), _uses("c_retry", "a.py", 11),
        _uses("c_retry", "b.py", 4), _uses("c_retry", "c.py", 7),
        # enough sites, only one file
        _uses("c_one_file", "a.py", 20), _uses("c_one_file", "a.py", 21),
        _uses("c_one_file", "a.py", 22),
        # enough files, too few sites
        _uses("c_rare", "a.py", 30), _uses("c_rare", "b.py", 31),
        # would clear everything but carries no declaration
        _uses("c_nodecl", "a.py", 40), _uses("c_nodecl", "b.py", 41),
        _uses("c_nodecl", "c.py", 42),
        _depends("blinker", "pyproject.toml", 24, constraint=">=1.9.0"),
        _depends("pytest", "pyproject.toml", 30, group="optional:test"),
        _depends("orders", "examples/demo/pyproject.toml", 7),
    ]
    return _shard(nodes, edges)


def test_only_recorded_evidence_becomes_a_numbered_decision():
    """The line between the two evidence classes, enforced where it matters most.

    A numbered ADR asserts that a decision was made. A manifest dependency supports that:
    somebody wrote it on purpose. A constant read in many places does not, and on a measured
    public tree three of seven such candidates were typing constructs, so numbering them
    would put "ADR-005: `T` is a repository-wide type variable" in a document whose whole
    claim is that it invents nothing. Constants stay in their table.
    """
    page = render_design_document(_full_shard(), repo_id=REPO)
    adrs, _, values = page.partition("## Load-bearing values")
    assert "ADR-001" in adrs
    assert "`blinker`" in adrs
    # MAX_RETRY qualifies as a load-bearing value and must never be numbered.
    assert "MAX_RETRY" not in adrs
    assert "`MAX_RETRY`" in values
    assert "ADR-" not in values


def test_only_the_repository_own_runtime_commitments_are_numbered():
    """Scope: the shallowest manifest, runtime and peer only.

    A nested project's dependencies are that project's decisions, and a dev dependency is a
    contributor's convenience rather than something the software cannot run without. All of
    them stay recorded in the tables, so nothing is lost by not numbering them; what would
    be lost by numbering them is the distinction the graph took work to record.
    """
    nodes = [_pkg("blinker"), _pkg("linter"), _pkg("plugin"), _pkg("nested")]
    edges = [
        _depends("blinker", "pyproject.toml", 10, constraint=">=1.9.0"),
        _depends("linter", "pyproject.toml", 20, group="dev", constraint=">=2"),
        _depends("plugin", "pyproject.toml", 30, group="optional:extras"),
        _depends("nested", "examples/demo/pyproject.toml", 5),
    ]
    page = render_design_document(_shard(nodes, edges), repo_id=REPO)
    adrs, _, tables = page.partition("## Recorded choices: dependencies")

    assert "ADR-001: Depend on `blinker`" in adrs
    # The count names BOTH numbers and what separates them. Stating only the filtered count
    # beside a table listing all three would read as a discrepancy, which on a page selling
    # the trustworthiness of its numbers is worse than the narrower scope it describes.
    assert "1 of the 3 dependencies `pyproject.toml` declares" in adrs
    for excluded in ("linter", "plugin", "nested"):
        assert excluded not in adrs, f"{excluded} was promoted to a decision record"
        # ... but every one is still recorded, so the exclusion costs no coverage.
        assert excluded in tables


def test_every_entry_states_the_status_and_the_absent_reason():
    page = render_design_document(_full_shard(), repo_id=REPO)
    assert page.count("**Status:** proposed, never ratified.") >= 1
    assert "Nobody wrote this down" in page
    # The numbers are positional in a generated file, so the page has to say so or a reader
    # will cite ADR-007 and mean something different after the next regeneration.
    assert "not stable identifiers" in page


def test_a_capped_decision_list_says_how_many_it_wrote_out():
    """Same rule as every other bounded list here: the total is stated, never implied."""
    nodes, edges = [], []
    for i in range(5):
        nodes.append(_pkg(f"lib{i}"))
        edges.append(_depends(f"lib{i}", "pyproject.toml", 10 + i, constraint=">=1"))
    page = render_design_document(_shard(nodes, edges), repo_id=REPO, max_adrs=2)
    assert "5 of the 5 dependencies `pyproject.toml` declares" in page
    assert "The first 2 are written out; the remaining 3 are in the table below." in page
    assert "ADR-002" in page and "ADR-003" not in page


def test_no_qualifying_commitment_writes_no_empty_section():
    """A heading with nothing under it reads as a finding. There is no finding here."""
    nodes = [_pkg("linter")]
    page = render_design_document(
        _shard(nodes, [_depends("linter", "pyproject.toml", 3, group="dev")]), repo_id=REPO)
    assert "## Proposed decision records" not in page
    assert "linter" in page  # still recorded in the table


def test_the_status_survives_being_parsed_not_only_read():
    """`docs/` is served to agents over MCP, so prose is not enough.

    An agent reading this file gets bytes, not a rendered page. "Proposed, never ratified"
    stated only in a paragraph is a sentence a summariser can drop; the marker is a token it
    can match. Both are present on purpose, one for each kind of reader.
    """
    page = render_design_document(_full_shard(), repo_id=REPO)
    assert STATUS_MARKER in page
    assert "proposed-never-ratified" in page
    assert "Nobody wrote this page" in page


def test_every_filtered_list_states_its_denominator():
    """Four constants exist and one clears the bar. Both numbers appear.

    Without the denominator the section reads as though this repository has one notable
    value, when in fact it has four and three of them were dropped by rules the reader
    cannot see.
    """
    page = render_design_document(_full_shard(), repo_id=REPO)
    assert "1 of 4 constants" in page
    assert "`MAX_RETRY`" in page
    for dropped in ("LOCAL_ONLY", "RARELY_READ", "NO_DECLARATION"):
        assert dropped not in page


def test_the_coverage_line_counts_what_qualified_not_what_fitted():
    """The cap must never be folded into the coverage number.

    Six constants clear the bar and the cap shows two. "2 of 6 carry evidence strong enough
    to print" would be false, not merely imprecise: six did, and a reader deciding whether
    this repository has notable values would be told a quarter of the truth. That is the
    same defect as a surface reporting a partial run as a complete one, which is what this
    whole page exists to avoid, so the cap gets its own sentence.

    No fixture exercised the cap until this one. The real repository this was built against
    had seven qualifiers against a limit of twenty-five, so the bound never bound and the
    bug was invisible in every end-to-end check.
    """
    nodes, edges = [], []
    for i in range(6):
        nodes.append(_const(f"c{i}", f"VALUE_{i}", f"VALUE_{i} = {i}"))
        # 8 down to 3 sites, each in its own file: every one clears MIN_USE_SITES and
        # MIN_USE_FILES, and the descending count makes the ordering checkable. The first
        # draft used `6 - i`, which left the last two below the site threshold, so only four
        # qualified and the test was measuring a different situation than it described.
        for n in range(8 - i):
            edges.append(_uses(f"c{i}", f"f{n}.py", 1))
    page = render_design_document(_shard(nodes, edges), repo_id=REPO, max_values=2)

    assert "6 of 6 constants" in page
    assert "2 of 6" not in page
    assert "4 more qualified and are not shown" in page
    # And it shows the ones read in the most places, not an arbitrary two.
    assert "`VALUE_0`" in page and "`VALUE_1`" in page
    assert "`VALUE_5`" not in page


def test_an_ambiguous_reading_never_reaches_a_printed_count():
    """The count has to MOVE when the confidence changes, or the filter is decoration.

    A name defined in several places has the same site attributed to each definition, so an
    unfiltered sum reports several times the real number. Here the same four readings are
    rendered twice, once INFERRED and once AMBIGUOUS, and the constant must qualify in the
    first case and vanish in the second.
    """
    nodes = [_const("c_retry", "MAX_RETRY", "MAX_RETRY = 3")]
    sites = [("a.py", 10), ("a.py", 11), ("b.py", 4), ("c.py", 7)]

    honest = render_design_document(
        _shard(nodes, [_uses("c_retry", f, ln) for f, ln in sites]), repo_id=REPO)
    assert "1 of 1 constants" in honest and "`MAX_RETRY`" in honest

    ambiguous = render_design_document(
        _shard(nodes, [_uses("c_retry", f, ln, Confidence.AMBIGUOUS) for f, ln in sites]),
        repo_id=REPO)
    assert "0 of 1 constants" in ambiguous
    assert "`MAX_RETRY`" not in ambiguous


def test_a_nested_project_gets_its_own_table():
    """A bundled example that depends on THIS repository must not read as a dependency of it.

    Merging every manifest into one list made a public library appear to depend on itself
    three times, once per bundled example, listed beside its real dependencies as an equal.
    The repository's own manifest sorts first because it is the shallowest path.
    """
    page = render_design_document(_full_shard(), repo_id=REPO)
    assert "### `pyproject.toml`" in page
    assert "### `examples/demo/pyproject.toml`" in page
    assert page.index("### `pyproject.toml`") < page.index("### `examples/demo/")
    # The self-dependency is present, under the example that declares it, not the root.
    root, _, nested = page.partition("### `examples/demo/pyproject.toml`")
    assert "`orders`" in nested and "`orders`" not in root


def test_a_dependency_carries_its_constraint_group_and_line():
    page = render_design_document(_full_shard(), repo_id=REPO)
    assert "`>=1.9.0`" in page
    assert "Optional extra `test`" in page
    assert "*unpinned*" in page  # absence shown as absence, not as an empty cell
    assert "| 24 |" in page


def test_only_extracted_dependency_edges_are_recorded_choices():
    """The section calls itself a RECORD. An inferred edge is not one.

    Compared against the `Confidence` enum rather than the string it serialises as: writing
    this same filter against a lowercase literal once made it match nothing, so everything
    passed and the resulting table looked entirely plausible.
    """
    nodes = [_pkg("guessed")]
    page = render_design_document(
        _shard(nodes, [_depends("guessed", "pyproject.toml", 3,
                                confidence=Confidence.INFERRED)]), repo_id=REPO)
    assert "guessed" not in page
    assert "No dependency is recorded" in page


def test_no_dependencies_says_which_manifests_were_read():
    """An empty list is ambiguous between two very different facts.

    "This project declares no dependencies" and "its dependencies are in a file I do not
    read" render identically as an empty section, and the second was real: an application
    that declares everything in a lock file or an unread table reported zero. The page has
    to name what it looked at.
    """
    page = render_design_document(_shard([], []), repo_id=REPO)
    assert "No dependency is recorded" in page
    assert "pyproject.toml" in page and "package.json" in page
    assert "lock file" in page


def test_a_long_declaration_is_cut_visibly():
    """Cut for display only, and never silently: a cell that just stops implies the value
    ended there. One real declaration opened a multi-line help string and filled its row."""
    long = "CONFIG = dict(" + ", ".join(f"key{i}='value{i}'" for i in range(20)) + ")"
    nodes = [_const("c_long", "CONFIG", long)]
    page = render_design_document(
        _shard(nodes, [_uses("c_long", "a.py", 1), _uses("c_long", "b.py", 2),
                       _uses("c_long", "c.py", 3)]), repo_id=REPO)
    assert "..." in page
    assert long not in page
    row = next(ln for ln in page.splitlines() if ln.startswith("| `CONFIG`"))
    assert len(row) < len(long)
    assert MAX_DECLARATION_SHOWN < len(long)  # the fixture actually exceeds the cap


def test_the_page_counts_symbols_and_does_not_characterise_them():
    """The rule the wiki's gotchas prompt already carries, applied to generated prose.

    A use count is evidence a value is load-bearing and no evidence about why. Words that
    explain the count are exactly what this page must never generate, because it has no way
    to tell a genuine architectural choice from a type variable that happens to be popular.
    """
    page = render_design_document(_full_shard(), repo_id=REPO).lower()
    for characterisation in ("foundational", "critical infrastructure", "core abstraction",
                             "most important", "key decision", "well-designed"):
        assert characterisation not in page


def test_a_repository_with_nothing_to_say_still_says_it():
    """An empty shard produces a page with both sections and no invented content."""
    page = render_design_document(_shard([], []), repo_id=REPO)
    assert page.startswith(f"# {REPO} design notes")
    assert "## Recorded choices: dependencies" in page
    assert "## Load-bearing values" in page
    assert "no indexed constant" in page
    assert page.endswith("\n")
