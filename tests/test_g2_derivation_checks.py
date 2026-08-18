"""The G2 bars decide whether an output is GENERATED from source or merely emitted.

Every one of the six output types could be satisfied by a fixture, a cached sample, or by
reading a README, so "it appeared after indexing" proves nothing about where it came from.
Each bar therefore changes one thing in the tree, re-indexes, and asserts the specific
movement that change implies.

These tests cover the deciding half. The runner clones, indexes and measures; the functions
here turn a before/after pair into a verdict, and that is the part a wrong answer would come
from. Testing them needs no network and no minutes of indexing, which is why they were
separated in the first place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "g2_checks",
    Path(__file__).resolve().parent.parent / "benchmarks" / "g2-derivation" / "checks.py")
checks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checks)


# --- code graph -------------------------------------------------------------------

def test_a_graph_that_grew_and_stayed_wired_passes():
    status, detail = checks.code_graph(
        {"nodes": 2551, "edges": 8074, "dangling": 0},
        {"nodes": 2558, "edges": 8086, "dangling": 0})
    assert status == checks.VERIFIED
    assert "2551 -> 2558" in detail


def test_a_graph_that_did_not_move_is_broken():
    """The whole point of the gate: an output that ignores its source is not derived."""
    status, detail = checks.code_graph(
        {"nodes": 2551, "edges": 8074, "dangling": 0},
        {"nodes": 2551, "edges": 8086, "dangling": 0})
    assert status == checks.BROKEN
    assert "did not move" in detail


def test_nodes_appended_without_their_edges_is_broken():
    """Totals moving is not enough: a symbol nothing points at is not in the graph in any
    useful sense, and the counts alone would have called this a pass."""
    status, detail = checks.code_graph(
        {"nodes": 2551, "edges": 8074, "dangling": 0},
        {"nodes": 2558, "edges": 8086, "dangling": 3})
    assert status == checks.BROKEN
    assert "dangling" in detail


@pytest.mark.parametrize("missing", ["nodes", "edges", "dangling"])
def test_a_measurement_that_was_never_taken_is_unverifiable(missing):
    """Not a pass and not a failure: the bar was not tested, which is its own answer.

    "Never taken" is the key being ABSENT. A key present with a None value is a different
    state -- the runner tried and could not read it -- and the two are asserted separately
    below, because folding them together is how a bad reading comes back as "not tested".
    """
    after = {"nodes": 2558, "edges": 8086, "dangling": 0}
    del after[missing]
    status, detail = checks.code_graph({"nodes": 2551, "edges": 8074, "dangling": 0}, after)
    assert status == checks.UNVERIFIABLE
    assert missing in detail


@pytest.mark.parametrize("unreadable", ["nodes", "edges", "dangling"])
def test_a_measurement_that_could_not_be_read_is_also_unverifiable(unreadable):
    """A count that came back None must not be compared as though it were a number: an
    unreadable dangling count is not "zero dangling"."""
    after = {"nodes": 2558, "edges": 8086, "dangling": 0}
    after[unreadable] = None
    status, _ = checks.code_graph({"nodes": 2551, "edges": 8074, "dangling": 0}, after)
    assert status == checks.UNVERIFIABLE


# --- generated docs ---------------------------------------------------------------

def _api(before_has=False, after_has=True, sites=5, before_n=1916, after_n=1922):
    return ({"api_has_symbol": before_has, "api_call_sites": 0, "api_symbols": before_n},
            {"api_has_symbol": after_has, "api_call_sites": sites, "api_symbols": after_n})


def test_a_reference_that_gained_the_symbol_and_its_call_sites_passes():
    b, a = _api()
    status, _ = checks.api_reference(b, a, symbol="probe_fn", call_sites=5)
    assert status == checks.VERIFIED


def test_a_symbol_already_present_before_the_probe_proves_nothing():
    """The control that stops a run passing by accident: if the symbol was there first,
    its presence afterwards says nothing about this change."""
    b, a = _api(before_has=True)
    status, detail = checks.api_reference(b, a, symbol="probe_fn", call_sites=5)
    assert status == checks.BROKEN
    assert "already" in detail


def test_a_symbol_listed_without_its_real_call_sites_is_broken():
    b, a = _api(sites=2)
    status, detail = checks.api_reference(b, a, symbol="probe_fn", call_sites=5)
    assert status == checks.BROKEN
    assert "call site" in detail


def test_design_notes_must_record_where_in_the_manifest_the_dependency_sits():
    b = {"design_has_dep": False, "design_dep_line": None, "design_adrs": 0}
    a = {"design_has_dep": True, "design_dep_line": None, "design_adrs": 1}
    status, detail = checks.design_notes(b, a, dependency="urllib3")
    assert status == checks.BROKEN
    assert "line number" in detail

    a["design_dep_line"] = 42
    status, detail = checks.design_notes(b, a, dependency="urllib3")
    assert status == checks.VERIFIED
    assert "line 42" in detail


# --- fleet, diagrams, wiki, vectors ------------------------------------------------

def test_the_fleet_page_must_move_and_attribute_the_dependency():
    b = {"fleet_shared": 2, "fleet_dep_repos": 0}
    a = {"fleet_shared": 3, "fleet_dep_repos": 2}
    assert checks.fleet_view(b, a, dependency="urllib3")[0] == checks.VERIFIED

    a_zero = {"fleet_shared": 3, "fleet_dep_repos": 0}
    status, detail = checks.fleet_view(b, a_zero, dependency="urllib3")
    assert status == checks.BROKEN and "cannot be right" in detail


def test_a_diagram_whose_summary_disagrees_with_its_picture_is_broken():
    """The count and the drawing are computed separately, so they can describe different
    graphs -- and the number is the part a reader trusts without checking."""
    b = {"diagram_nodes": 38, "diagram_edges": 43, "diagram_rendered_nodes": 38,
         "diagram_rendered_edges": 43}
    a = {"diagram_nodes": 43, "diagram_edges": 48, "diagram_rendered_nodes": 43,
         "diagram_rendered_edges": 44}
    status, detail = checks.diagram(b, a, call_sites=5)
    assert status == checks.BROKEN
    assert "announced" in detail

    a["diagram_rendered_edges"] = 48
    assert checks.diagram(b, a, call_sites=5)[0] == checks.VERIFIED

    # The node half of the same comparison, which the edge fixture above never exercises:
    # breaking the node check alone failed nothing until this case existed.
    a_nodes = dict(a, diagram_rendered_nodes=41)
    status, detail = checks.diagram(b, a_nodes, call_sites=5)
    assert status == checks.BROKEN
    assert "43 nodes and rendered 41" in detail

    # And a diagram that drew fewer edges than the symbol has real call sites: the counts
    # agree with each other and are still wrong about the code.
    a_short = {"diagram_nodes": 43, "diagram_edges": 2, "diagram_rendered_nodes": 43,
               "diagram_rendered_edges": 2}
    status, detail = checks.diagram(b, a_short, call_sites=5)
    assert status == checks.BROKEN
    assert "real call site" in detail


def test_a_wiki_stamp_that_did_not_advance_is_broken():
    status, detail = checks.wiki({"wiki_commit": "be1bc4e7"}, {"wiki_commit": "be1bc4e7"})
    assert status == checks.BROKEN
    assert "stale stamp" in detail
    assert checks.wiki({"wiki_commit": "be1bc4e7"},
                       {"wiki_commit": "34b96b4"})[0] == checks.VERIFIED


def test_semantic_search_must_return_the_probe_at_all():
    """The query shares no word with the symbol or its docstring, so a substring matcher
    returns it NOWHERE. Being in the ranked results is therefore already proof of retrieval
    by meaning, and the rank is recorded rather than gated on.

    This bar was written demanding first place and loosened after a live run measured #3 of
    a 1,830-symbol corpus. The loosening is asserted here on purpose: a threshold that moved
    because it failed has to be visible in the tests, not only in a comment.
    """
    for rank in (0, 2, 9):
        status, detail = checks.vector_search({}, {"semantic_rank": rank}, symbol="p")
        assert status == checks.VERIFIED
        assert f"#{rank + 1}" in detail, "the measured rank has to survive into the evidence"
    status, detail = checks.vector_search({}, {"semantic_rank": None}, symbol="p")
    assert status == checks.BROKEN
    assert "not returned at all" in detail, (
        "the failure has to name what happened: the query returned the symbol nowhere, "
        "which is exactly what a substring matcher would do")


# --- the summary ------------------------------------------------------------------

def test_an_untested_bar_counts_against_the_run():
    """G2 asks whether the bar was PROVEN. A bar that could not be tested has not been, and
    a summary that treated it as a pass would report a gate as closed on evidence that was
    never gathered."""
    ok, line = checks.summarise([
        ("code graph", checks.VERIFIED, ""),
        ("wiki", checks.UNVERIFIABLE, "no page was produced"),
    ])
    assert ok is False
    assert "not tested" in line and "wiki" in line


def test_all_verified_is_the_only_pass():
    ok, line = checks.summarise([("a", checks.VERIFIED, ""), ("b", checks.VERIFIED, "")])
    assert ok is True
    assert "2/2 verified" in line


def test_a_broken_bar_is_named_in_the_summary():
    ok, line = checks.summarise([("diagrams", checks.BROKEN, "counts disagree")])
    assert ok is False
    assert "BROKEN" in line and "diagrams" in line
