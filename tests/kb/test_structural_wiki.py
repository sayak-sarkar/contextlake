"""The structural wiki page: six sections, rendered from facts, no model involved.

Two properties get most of the attention here.

**Empty sections are omitted AND named.** Either half alone is a worse page than both: a
small library whose page is two-thirds "none found" is noise, and a section that vanishes
without a word reads identically whether the repository has none of that thing or the
extractor missed it.

**The Markdown has to survive its own inputs.** Every cell carries text from source, and a
signature with a `|` in it or a doc comment with a newline rewrites the table around it.
Two tables with no blank line between them merge into one broken table, which is what the
first draft did.
"""

from __future__ import annotations

import re

from contextlake.kb.wiki.structural import SECTION_TITLES, render_structural_page

FULL_BRIEF = {
    "top_symbols": [
        {"kind": "entry_point", "name": "main", "file": "cmd/m.go", "signature": None},
        {"kind": "route", "name": "GET /health", "file": "api.py", "signature": None},
        {"kind": "function", "name": "helper", "file": "h.py", "signature": "int f()"},
    ],
    "hubs": [{"kind": "function", "name": "load", "file": "x.py", "count": 12}],
    "langs": {"go": 40, "python": 12},
    "kinds": {"function": 30, "entry_point": 1},
    "setup_signals": (["Makefile", "pyproject.toml"], {"go": 3}, ["Dockerfile"]),
}
OWNERS = [{"name": "Contributor a1b2", "share": 0.62, "last_active": "2026-08-01"}]
MODULES = [{"prefix": "api", "nodes": 30}]
DEPS = {"depends_on": ["team/core"], "depended_on_by": ["team/ui"]}


def _full(**kw) -> str:
    base = dict(repo_id="team/app", modules=MODULES, owners=OWNERS, dependencies=DEPS)
    base.update(kw)
    return render_structural_page(FULL_BRIEF, **base)


# --- the six sections ---------------------------------------------------------------


def test_a_complete_brief_renders_all_six_sections():
    page = _full()
    for title in SECTION_TITLES.values():
        assert f"## {title}" in page, f"missing section: {title}"
    assert "## Sections omitted" not in page


def test_each_section_carries_its_own_facts():
    page = _full()
    assert "`main`" in page and "`GET /health`" in page   # entry points
    assert "`api`" in page                                # architecture
    assert "Contributor a1b2" in page and "62%" in page   # ownership
    assert "`load`" in page and "| 12 |" in page          # public surface
    assert "`Makefile`" in page                           # installation
    assert "| go | 40 |" in page                          # contents


def test_the_public_surface_reports_callers_not_a_sample():
    """The count is the point. It comes from the brief's hub ranking, computed over the
    whole shard, so "12 callers" is a fact about the repository rather than about the
    fifteen symbols somebody happened to sample."""
    page = _full()
    row = next(ln for ln in page.splitlines() if ln.startswith("| `load`"))
    assert row.endswith("| 12 |")


# --- empty sections: omitted, and named ---------------------------------------------


def test_an_empty_section_is_omitted():
    page = render_structural_page({"langs": {"go": 1}}, repo_id="t/a")
    assert "## Entry points and how to run it" not in page
    assert "## What this repository contains" in page


def test_every_omitted_section_is_named():
    """The half that keeps the omission honest. Without it, a missing section reads the
    same whether the repository has no entry points or the extractor missed them."""
    page = render_structural_page({"langs": {"go": 1}}, repo_id="t/a")
    assert "## Sections omitted" in page
    for key, title in SECTION_TITLES.items():
        if key == "contents":
            continue
        assert title in page.split("## Sections omitted", 1)[1], (
            f"{title} was omitted from the page without being named")


def test_a_section_with_no_rows_does_not_render_a_bare_header_row():
    """A Markdown table header with no body still renders as a table, so it would make
    every section permanently non-empty and defeat the rule above."""
    page = render_structural_page({"top_symbols": [], "langs": {"go": 1}}, repo_id="t/a")
    assert "| Name | Kind | File | Signature |" not in page


def test_a_page_with_nothing_at_all_still_says_so():
    """The degenerate case. An empty brief must not produce a title and silence, which
    would read as a rendering failure rather than as an empty repository."""
    page = render_structural_page({}, repo_id="t/a")
    assert "## Sections omitted" in page
    assert all(t in page for t in SECTION_TITLES.values())


# --- the Markdown has to survive its inputs -----------------------------------------


def test_a_pipe_in_a_signature_cannot_break_the_table():
    """A C++ signature carries `|` for bitwise-or, and an unescaped one splits the row,
    silently shifting every later column by one."""
    brief = {"top_symbols": [{"kind": "entry_point", "name": "main", "file": "m.cpp",
                              "signature": "int main(int a|b)"}]}
    page = render_structural_page(brief, repo_id="t/a")
    row = next(ln for ln in page.splitlines() if "main" in ln and ln.startswith("|"))
    # UNESCAPED pipes only. The escaped one is still a `|` character, so counting every
    # pipe fails against a correctly escaped row -- which is what the first draft of this
    # assertion did, reporting the fix as the bug.
    delimiters = len(re.findall(r"(?<!\\)\|", row))
    assert delimiters == 5, f"the signature's pipe added a column: {row}"
    assert r"a\|b" in row


def test_a_newline_in_a_doc_cannot_end_the_row_early():
    brief = {"top_symbols": [{"kind": "route", "name": "GET /x", "file": "a.py",
                              "signature": "line one\nline two"}]}
    page = render_structural_page(brief, repo_id="t/a")
    rows = [ln for ln in page.splitlines() if ln.startswith("| `GET /x`")]
    assert len(rows) == 1 and "line one line two" in rows[0]


def test_no_two_tables_are_adjacent_without_a_blank_line():
    """Two tables with no blank line between them merge into one, and the second one's
    header renders as a data row of the first.

    This is not hypothetical: the first draft filtered blank strings out of the contents
    section while building it, and the language table and the kind table ran together.
    """
    page = _full()
    lines = page.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].startswith("|") and lines[i + 1].startswith("| ---"):
            # a header/separator pair is fine; what must never happen is a separator row
            # appearing directly after a DATA row of a previous table.
            before = lines[i - 1] if i else ""
            assert not (before.startswith("|") and not before.startswith("| ---")
                        and i >= 2 and lines[i - 2].startswith("|")), (
                f"a second table starts at line {i} with no blank line before it:\n"
                f"{before}\n{lines[i]}\n{lines[i + 1]}")


def test_the_page_ends_with_exactly_one_newline():
    assert _full().endswith("\n") and not _full().endswith("\n\n")


# --- scope labelling ----------------------------------------------------------------


def test_a_module_page_says_it_covers_only_that_module():
    page = _full(path_prefix="api")
    assert page.startswith("# team/app — api")
    assert "covers only the `api` module" in page


def test_cross_repo_dependencies_are_labelled_as_repo_level_on_every_page():
    """The one section a module page cannot scope.

    `repo_dependency_edges` comes from the package two-hop and is repo-level by
    construction. Under a neutral heading, a module page would read as though THIS module
    depended on those repositories, which is the "reads as if it describes the same scope"
    failure the module-page title already guards against one level up.
    """
    for kwargs in ({}, {"path_prefix": "api"}):
        page = _full(**kwargs)
        for line in page.splitlines():
            if "team/core" in line or "team/ui" in line:
                assert "whole repository, not scoped to any module" in line, (
                    f"a cross-repo dependency line carried no scope label: {line}")


def test_both_dependency_directions_are_rendered():
    """The differentiator is the pair. "What depends on me" is the question no single-repo
    tool can answer at all, and rendering only the outbound half would quietly drop it."""
    page = _full()
    assert "This repository depends on" in page
    assert "Repositories that depend on this one" in page


def test_ownership_shows_share_not_a_commit_scoreboard():
    """Ownership answers "who should I ask", and raw per-person commit and line counts
    are what would turn the same table into a productivity ranking."""
    page = _full()
    section = page.split("## Ownership and activity", 1)[1].split("##", 1)[0]
    assert "62%" in section
    assert not re.search(r"\bcommits?\b", section, re.I), (
        f"the ownership section grew a commit count: {section}")
