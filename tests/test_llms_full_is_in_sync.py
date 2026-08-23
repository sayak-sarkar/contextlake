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

``markdown`` is imported at ``build_docs`` module scope and is not a runtime
dependency, so this skips wherever it is absent rather than failing there.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SITE = REPO / "site"


def _build_docs():
    pytest.importorskip("markdown", reason="site builder dependency, not a runtime one")
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
