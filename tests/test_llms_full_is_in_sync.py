"""``site/llms-full.txt`` is generated and committed, so it can silently go stale.

It is the whole docs corpus concatenated into one file for single-fetch ingestion by
an LLM, built by ``site/build_docs.py``'s ``gen_llms_full`` from the same markdown
sources the rendered site uses. Because it is committed rather than built on demand,
editing a page under ``docs/`` without re-running the builder leaves the two
disagreeing, and nothing notices: the whole suite stays green while a published
artifact contradicts the documentation it was generated from.

That is not hypothetical. A ``--repos`` scope paragraph was corrected in
``docs/mirroring-repositories.md`` and the committed corpus kept the superseded wording, found only
because the agent that made the edit thought to mention it.

The check recomputes what ``gen_llms_full`` would write and compares. It does not
invoke the builder, which also copies assets and shells out to git; it reuses the
builder's own ``PAGES``, ``LLMS_INTRO``, ``de_emdash`` and ``linkify``, so the two
cannot drift apart the way a hand-copied expectation would.

``site/build_docs.py`` imports ``markdown`` only inside ``main()``, so this test does not
skip where ``markdown`` is absent. Keep that import out of module scope: a module-scope
import makes ``importorskip`` skip the check there.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SITE = REPO / "site"


def _build_docs():
    if str(SITE) not in sys.path:
        sys.path.insert(0, str(SITE))
    return pytest.importorskip("build_docs", reason="site/build_docs.py not importable")


def test_llms_full_matches_the_docs_it_was_generated_from():
    bd = _build_docs()
    target = SITE / "llms-full.txt"
    if not target.is_file():
        pytest.skip("site/llms-full.txt is not present in this checkout")

    # Mirrors gen_llms_full exactly. Kept in step by reusing the builder's own
    # helpers rather than restating the transform.
    parts = [bd.LLMS_INTRO.strip(), ""]
    for out, src, nav_title, *_ in bd.PAGES:
        parts += ["\n---\n", f"# {nav_title}", f"Source: {bd.BASE}{bd.linkify(out)}", "",
                  bd.de_emdash((REPO / src).read_text(encoding="utf-8")).strip()]
    expected = "\n".join(parts) + "\n"

    actual = target.read_text(encoding="utf-8")
    if actual == expected:
        return

    # Name the pages that drifted; a whole-file diff of a corpus this size is unreadable.
    stale = [
        nav_title
        for _out, src, nav_title, *_ in bd.PAGES
        if bd.de_emdash((REPO / src).read_text(encoding="utf-8")).strip() not in actual
    ]
    detail = ("pages whose current text is absent: " + ", ".join(stale)) if stale else (
        "no single page is missing, so the drift is in the intro, the page order, "
        "or a Source link"
    )
    pytest.fail(
        "site/llms-full.txt is out of date with the docs it is generated from.\n"
        f"{detail}\n"
        "Regenerate it with: .venv/bin/python site/build_docs.py"
    )


def test_every_docs_page_is_in_the_site_page_list_or_marked_github_only():
    """CONTRIBUTING.md said the build fails for a `docs/*.md` that is in neither `PAGES` nor
    `TO_GH`. The build never looked: it exited 0 and the page was missing from the nav, the
    sitemap, `llms.txt`, `llms-full.txt` and search. This is the check, so the sentence is true."""
    bd = _build_docs()
    registered = {src for _out, src, *_ in bd.PAGES} | set(bd.TO_GH)
    on_disk = {p.relative_to(REPO).as_posix() for p in (REPO / "docs").glob("*.md")}
    assert len(on_disk) > 20, "the docs directory scan found almost nothing"
    unregistered = sorted(on_disk - registered)
    assert not unregistered, (
        f"add these to PAGES (or to TO_GH if they stay on GitHub) in site/build_docs.py: "
        f"{unregistered}")


def test_build_docs_imports_markdown_only_inside_a_function():
    """`markdown` is not a runtime dependency. A module-scope import in `site/build_docs.py`
    makes `importorskip` skip the two sync tests that call `_build_docs()` wherever `markdown`
    is absent, so the committed `llms-full.txt` could drift unchecked. This test never calls
    `_build_docs()`, so it still runs there and fails. The import belongs inside `main()`."""
    tree = ast.parse((SITE / "build_docs.py").read_text(encoding="utf-8"))
    found = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                names = [a.name for a in child.names]
            elif isinstance(child, ast.ImportFrom):
                names = [child.module or ""]
            else:
                names = []
            found.extend(n for n in names if n.split(".")[0] == "markdown")
            visit(child)

    visit(tree)
    assert not found, f"move this import into main() in site/build_docs.py: {found}"
