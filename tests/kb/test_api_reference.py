"""The generated API reference: real call sites, and honest numbers about them.

Every guard here exists because the same claim was rendered wrongly on a real public tree
first. The reference was measured on two pinned public repositories before any of this was
written, and each defect it showed is one test below:

**A caller is a definition, not a container.** A `calls` edge's source can be the enclosing
FILE, which is what the store records when a call cannot be attributed to any symbol. Counting
those as callers overstates how many places a reader must open; discarding every non-documented
kind instead UNDERSTATES it, and did: on one C++ tree that dropped 270 test-function callers,
reporting a symbol with 12 real call sites as having 0 callers.

**The unattributed count is a subset.** Phrased as "and N more" it turned 12 sites with no
named caller into a page saying 24.

**Selection order and presentation order are different.** Symbols are chosen by call count and
then grouped by file, so a page claiming to be "ordered by how many places call them" put the
least-used symbols first.

**A cap inside a tie is not a ranking.** On one Python tree only 365 of 1607 symbols had any
call site, so a 500-entry cap fell entirely inside the tie at zero: which symbols were dropped
was decided by filename, not by importance, and the page has to say so.

The fixture therefore carries BOTH a symbol-source and a file-source call edge. With only
symbol sources the caller guard never runs and reads as unnecessary.
"""

from __future__ import annotations

from datetime import date

import pytest

from contextlake.kb.docs.api import (
    CONTAINER_KINDS,
    render_api_reference,
    scoped_name,
)
from contextlake.kb.model import Confidence, Edge, Node, Provenance
from contextlake.kb.store.shards import GraphShard

REPO = "team/svc"
_VERIFIED = date(2026, 8, 17)


def _node(nid, kind, name, file, line, qn=None):
    return Node(id=nid, repo=REPO, kind=kind, name=name, qualified_name=qn,
                file=file, line_start=line, line_end=line + 5)


def _calls(src, dst, file, line):
    return Edge(src=src, dst=dst, relation="calls", confidence=Confidence.EXTRACTED,
                provenance=Provenance(source_file=file, source_line=line,
                                      verified_at=_VERIFIED))


# `target` is called four times: twice from a method, once from a TEST function, once from
# file-level code. Those numbers -- 4 sites, 2 callers, 1 unattributed -- are all different, so
# a test can tell which of them a change moved. A fixture where they coincided could not.
#
# The test-kind caller is load-bearing rather than variety. `test` is a definition (registry
# group "Symbols") but is not a DOCUMENTED kind, so it is the only case that separates "a
# caller is anything that is not a container" from "a caller is a documentable kind". Without
# it, reverting that rule changed no number in this file and the guard read as unnecessary --
# while on a real C++ tree the same revert dropped 270 callers.
TARGET_SITES = 4
TARGET_CALLERS = 2
TARGET_UNATTRIBUTED = 1


@pytest.fixture
def shard():
    return GraphShard(
        repo=REPO, head_commit="h1",
        nodes=[
            _node("target", "function", "encode", "codec.py", 100,
                  qn="codec.py::encode"),
            _node("caller", "method", "run", "driver.py", 10,
                  qn="driver.py::Driver.run"),
            # A definition that is NOT a documentable kind, and still a caller.
            _node("tcase", "test", "test_encodes", "test_codec.py", 5,
                  qn="test_codec.py::test_encodes"),
            # The file node that a call with no enclosing definition is attributed to.
            _node("driverfile", "file", "driver.py", "driver.py", 1),
            # Nothing calls this one, which is a real finding rather than a gap.
            _node("orphan", "function", "unused", "codec.py", 200,
                  qn="codec.py::unused"),
        ],
        edges=[
            _calls("caller", "target", "driver.py", 20),
            _calls("caller", "target", "driver.py", 30),
            _calls("tcase", "target", "test_codec.py", 7),
            _calls("driverfile", "target", "driver.py", 4),
        ],
    )


def test_file_source_is_not_counted_as_a_caller(shard):
    """Four call sites, two callers: the file-attributed site is not a third caller."""
    page = render_api_reference(shard, repo_id=REPO)
    assert f"**{TARGET_SITES} call site(s)** across **{TARGET_CALLERS} caller(s)**" in page


def test_unattributed_sites_are_reported_as_a_subset(shard):
    """"of which", never "and N more": the sites are already inside the total."""
    page = render_api_reference(shard, repo_id=REPO)
    assert f"{TARGET_UNATTRIBUTED} of which name no caller" in page
    assert "more not attributed" not in page


def test_a_file_source_is_never_rendered_as_a_name(shard):
    """The Caller column says what a file-level site is, rather than naming the file."""
    page = render_api_reference(shard, repo_id=REPO)
    assert "file-level code, no enclosing definition recorded" in page
    # The defect this forbids: `driver.py` appearing as though it were the calling symbol.
    # It is still legitimate in the File column, so the assertion is on the backticked
    # name form the caller cell would have used.
    assert "| `driver.py` *(" not in page


def test_a_non_container_source_is_named_with_its_kind(shard):
    """A method, a test or an entry point IS a caller and is named as one."""
    page = render_api_reference(shard, repo_id=REPO)
    assert "`Driver.run` *(method)*" in page


def test_a_definition_that_is_not_documented_is_still_a_caller(shard):
    """The 270-caller bug: a test function calls things, and is not itself documented.

    Two separate claims, because the bug broke both: the count includes it, and the row names
    it rather than falling back to the file-level wording.
    """
    page = render_api_reference(shard, repo_id=REPO)
    assert "`test_encodes` *(test)*" in page
    assert f"across **{TARGET_CALLERS} caller(s)**" in page
    # And it is not DOCUMENTED: a test has no entry of its own.
    assert "### `test_encodes`" not in page


def test_container_kinds_come_from_the_registry():
    """The container set is read off `KIND_REGISTRY`, so it cannot drift from it."""
    assert "file" in CONTAINER_KINDS
    assert "module" in CONTAINER_KINDS
    # A definition kind must never be in it: that is the bug that dropped 270 callers.
    assert not ({"function", "method", "test", "class"} & CONTAINER_KINDS)


def test_a_symbol_with_no_call_site_says_so(shard):
    page = render_api_reference(shard, repo_id=REPO)
    assert "No call site is recorded in this repository." in page


def test_the_header_does_not_claim_the_order_it_does_not_use(shard):
    """Selected by call count, GROUPED by file: the page states both, not one as the other."""
    page = render_api_reference(shard, repo_id=REPO)
    header = page.split("## ", 1)[0]
    assert "grouped by the file" in header
    assert "not by call count" in header


def test_a_cap_where_every_omitted_symbol_ties_claims_no_ranking_at_all(shard):
    """When the whole omitted set ties with a kept entry, there is no ranking to describe.

    This is the case a real Python tree produced: 365 of 1607 symbols had any call site, so a
    500-entry cap fell entirely inside the tie at zero. Saying "the ones omitted are those with
    the fewest call sites" there is a ranking claim about symbols that are all equal.
    """
    page = render_api_reference(shard, repo_id=REPO, max_symbols=2)
    assert "every symbol left out has exactly as many recorded call sites" in page
    assert "rather than to any judgement about importance" in page
    # And it must NOT also make the ranking claim it just contradicted.
    assert "are those with the fewest" not in page


def test_a_cap_where_only_some_omitted_symbols_tie_says_which_part_was_arbitrary():
    """A partial tie is a third case: the cut was mostly by call count, partly by filename.

    Sites 3, 1, 1, 0 with a cap of 2 keeps the 3 and one of the 1s, so one omitted symbol ties
    with a kept entry and one genuinely had fewer. Both halves of that get stated.
    """
    nodes = [_node("t", "function", "target", "a.py", 1, qn="target"),
             _node("one", "function", "one", "b.py", 10, qn="one"),
             _node("two", "function", "two", "c.py", 10, qn="two"),
             _node("zero", "function", "zero", "d.py", 10, qn="zero")]
    # `target` gets 3 sites, `one` and `two` get 1 each, `zero` gets none.
    edges = [_calls("one", "t", "b.py", 11), _calls("two", "t", "c.py", 11),
             _calls("zero", "t", "d.py", 11),
             _calls("t", "one", "a.py", 2), _calls("t", "two", "a.py", 3)]
    page = render_api_reference(
        GraphShard(repo=REPO, head_commit="h1", nodes=nodes, edges=edges),
        repo_id=REPO, max_symbols=2)
    assert "are those with the fewest recorded call sites" in page
    assert "tie with entries that were kept" in page
    assert "arbitrary rather than a judgement about importance" in page


def test_a_cap_outside_a_tie_makes_no_arbitrariness_claim(shard):
    """The tie note is conditional. Claiming it always would be its own false statement.

    A cap of 1 keeps only `encode`, whose 3 call sites no omitted symbol matches, so the cut
    genuinely was by call count and the page must not undercut its own ranking.
    """
    page = render_api_reference(shard, repo_id=REPO, max_symbols=1)
    assert "are not listed" in page
    assert "arbitrary rather than a judgement" not in page


def test_scoped_name_disambiguates_overloads():
    """Six identical `Get()` headings in one file section is a page a reader cannot use."""
    a = _node("a", "method", "flush", "os.h", 1, qn="ostream.flush")
    b = _node("b", "method", "flush", "os.h", 9, qn="detail.glibc_file.flush")
    assert scoped_name(a) == "ostream.flush"
    assert scoped_name(b) == "detail.glibc_file.flush"
    assert scoped_name(a) != scoped_name(b)


def test_scoped_name_strips_a_file_prefix():
    """Every non-C/C++ language carries `file_scope::`; a path in a heading is noise."""
    n = _node("n", "method", "run", "driver.py", 10, qn="driver.py::Driver.run")
    assert scoped_name(n) == "Driver.run"


def test_scoped_name_falls_back_when_the_scope_disagrees():
    """A `qualified_name` that does not end in the node's own name is not used.

    Without this the heading could contradict the entry beneath it, which is worse than a
    bare name: the graph records unqualified member functions for some C++ shapes, and
    inventing a scope for them would be a confident wrong answer.
    """
    n = _node("n", "function", "data", "args.h", 108, qn="something.else")
    assert scoped_name(n) == "data"
    bare = _node("m", "function", "data", "args.h", 108, qn="data")
    assert scoped_name(bare) == "data"


def test_empty_reference_states_the_observation_not_a_cause():
    """A build-file-only repository is the ordinary case, not a broken index."""
    page = render_api_reference(
        GraphShard(repo=REPO, head_commit="h1",
                   nodes=[_node("f", "file", "Makefile", "Makefile", 1)], edges=[]),
        repo_id=REPO)
    assert "no indexed symbol of a documentable kind" in page
    assert "build or configuration files that is expected" in page


def test_a_pipe_in_a_caller_name_does_not_rewrite_the_table():
    """The Caller column carries a symbol name from source, and a `|` in one splits the row.

    Not hypothetical: a C++ bitwise-or overload is named `operator|`, and it calls things. The
    escaping lives in `mdwrite.cell`, so this asserts the reference actually routes through
    it -- a name interpolated directly would look identical in the source and break the table.

    The signature is deliberately NOT the subject here. It is rendered into the `###` heading,
    which is not a table row, so a pipe there is harmless and a test asserting otherwise would
    be guarding a path that does not exist.
    """
    target = _node("t", "function", "mask", "m.cpp", 1, qn="mask")
    op = _node("op", "method", "operator|", "m.cpp", 10, qn="Flags.operator|")
    page = render_api_reference(
        GraphShard(repo=REPO, head_commit="h1", nodes=[target, op],
                   edges=[_calls("op", "t", "m.cpp", 11)]),
        repo_id=REPO)
    assert r"Flags.operator\|" in page
    # Every row in the rendered table must have the same cell count as its header.
    rows = [ln for ln in page.splitlines() if ln.startswith("| ")]
    assert rows and len({ln.count(" | ") for ln in rows}) == 1
