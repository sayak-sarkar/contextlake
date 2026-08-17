"""May generated prose take the structural page's place?

A repository has one wiki page per scope. The structural page is written first and IS that
page; prose replaces it only when the prose is accurate about the same names and covers the
same ground. Passing the council is not sufficient, and cannot be: a council judges a page
on its own terms and has never seen the page it would displace.

Both conditions are about the DISPLACED page, which is what makes this a separate gate
rather than another council lens.
"""

from __future__ import annotations

from contextlake.kb.wiki.structural import render_structural_page
from contextlake.kb.wiki.validate import model_body, replacement_gate

BRIEF = {
    "repo": "team/app", "head": "abc1234", "parser_version": "6",
    "top_symbols": [{"kind": "entry_point", "name": "serve", "file": "cmd/m.go"}],
    "hubs": [{"kind": "function", "name": "load", "file": "x.py", "count": 9}],
    "langs": {"go": 5}, "kinds": {"function": 4},
    # A flat list, which is the shape `repo_brief` carries. Written as a three-tuple here
    # first, matching a shard-only helper's internal return; the renderer mis-unpacked it
    # the same way, so both were wrong in the same direction and neither showed it.
    "setup_signals": ["Makefile"],
}
PAGE = render_structural_page(BRIEF, repo_id="team/app")


def _complete_draft() -> str:
    """Prose that names something from every section the page rendered."""
    return ("## How to run it\n\n`serve` is the entry point.\n\n"
            "## Surface\n\n`load` has nine callers, and `x.py` holds it.\n\n"
            "## Building\n\n`Makefile` drives the build.\n\n"
            "## Contents\n\nMostly `function` symbols, in `go`.\n")


def test_a_complete_accurate_draft_may_replace_the_page():
    assert replacement_gate(_complete_draft(), PAGE) is None


def test_a_draft_citing_a_name_the_page_does_not_hold_is_refused():
    """Sound precisely because the structural page IS the prompt: the model saw nothing
    else, so a name that is not there was invented. The check would be wrong the other way
    round, which is why the grounding change had to land first."""
    verdict = replacement_gate(
        _complete_draft() + "\nIt also calls `PaymentGateway`.\n", PAGE)
    assert verdict is not None
    assert verdict["reason"] == "cites names the graph does not hold"
    assert "PaymentGateway" in verdict["issues"][0]


def test_a_draft_that_drops_a_section_is_refused():
    """The strict half. A page covering three of four sections would otherwise replace one
    that covered four, and "at least as complete" is the whole basis on which a verified
    document steps aside."""
    thin = "## Overview\n\n`serve` is the entry point and `load` has callers.\n"
    verdict = replacement_gate(thin, PAGE)
    assert verdict is not None
    assert verdict["reason"] == "less complete than the page it would replace"
    assert "Installation and usage" in verdict["issues"][0]


def test_the_verdict_is_shaped_like_the_other_gates():
    """So a caller composes the three in one expression instead of growing a second
    idiom for gating."""
    verdict = replacement_gate("## X\n\nnothing.\n", PAGE)
    assert set(verdict) >= {"accepted", "score", "reason", "issues"}
    assert verdict["accepted"] is False


def test_sections_the_page_itself_omitted_are_not_required():
    """A page with no entry points cannot demand prose about entry points. Without this the
    gate would reject every draft for a repository that simply has no `main`."""
    brief = dict(BRIEF, top_symbols=[])
    page = render_structural_page(brief, repo_id="team/app")
    assert "## Entry points and how to run it" not in page
    draft = ("## Surface\n\n`load` has callers in `x.py`.\n\n"
             "## Building\n\n`Makefile` drives it.\n\n"
             "## Contents\n\n`function` symbols in `go`.\n")
    assert replacement_gate(draft, page) is None


def test_the_provenance_footer_is_not_read_as_a_model_citation():
    """contextlake appends that footer itself and backticks the repo id, the commit and the
    source files. Counting them rejected every page for "citing" its own repository's name,
    which is the first thing this gate did when it was wired up.
    """
    from contextlake.kb.wiki.generate import provenance_footer

    footered = _complete_draft() + provenance_footer(
        dict(BRIEF, files=["cmd/m.go"], grounded_count=1, coverage_total=1))
    assert "`team/app`" in footered, "the fixture lost the footer, so this proves nothing"
    assert replacement_gate(footered, PAGE) is None


def test_model_body_stops_at_the_footer():
    from contextlake.kb.wiki.generate import provenance_footer

    body = model_body(_complete_draft() + provenance_footer(
        dict(BRIEF, files=["cmd/m.go"], grounded_count=1, coverage_total=1)))
    assert "Generated from the knowledge graph" not in body
    assert "`serve`" in body


def test_a_multi_word_backticked_span_is_not_treated_as_a_symbol_name():
    """`kb wiki --force` is a command a page may legitimately quote, not a claim about a
    symbol. Treating it as a citation would reject honest prose for naming the tool."""
    draft = _complete_draft() + "\nRun `contextlake kb wiki --force` to regenerate.\n"
    assert replacement_gate(draft, PAGE) is None
