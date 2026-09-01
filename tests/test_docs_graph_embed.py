"""The live graph embedded in the docs pages that explain the graph.

Four pages described the visualizer in prose while the running thing sat one directory
away, reachable only from the landing page. They now carry it.

`graph-embed.html` is 788 KB, so it is NOT embedded eagerly: the markup ships an
`<img>` and an IntersectionObserver swaps in the iframe when the reader scrolls near
it. A reader who never scrolls there, and a reader with no JS, keeps the screenshot.

**Asserted against the SOURCES, not the built pages.** `.gitignore` ignores
`site/*.html` apart from `index.html` and `graph-embed.html`, because the docs pages
are generated per build. The first version of this file read those built pages and
passed on a machine that had just run the builder, then failed all three tests on CI,
where only `index.html` exists. A local green proved nothing about the thing CI runs.

**The swap itself is not asserted here, and that is deliberate.** It was verified in a
real browser (Playwright: img replaced, `src="graph-embed.html#theme=light"`, 600 px
tall, and inside the frame four cytoscape canvases, the inspector and the legend).
`--dump-dom`, which every other browser test in this suite uses, does NOT run
IntersectionObserver callbacks -- the SHIPPED landing page, which has used this exact
pattern since long before this change, also reports "still-img" under it. So a
dump-dom assertion here would pin the environment's limitation, not the behaviour.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
SITE = REPO / "site"
BUILDER = SITE / "build_docs.py"

EMBED_PAGES = ["asking-the-graph", "code-graph-model",
               "indexing-the-code-graph", "visualizing-the-graph"]
IMG = re.compile(r'<img[^>]*\bdata-embed="([^"]+)"[^>]*>')


# The builder is READ, not imported. Importing it needs `markdown`, which is a site
# dependency and not installed in CI's jobs -- the first version of these two tests used
# `importorskip` and SKIPPED on CI while passing here, which is the same
# only-runs-on-my-machine hole the rest of this file was rewritten to close. Both
# questions below are answerable from the source text.
def _builder_source() -> str:
    return BUILDER.read_text(encoding="utf-8")


def test_each_graph_page_source_carries_the_embed():
    """The four `.md` sources, which are what git tracks and what the builder reads."""
    for name in EMBED_PAGES:
        md = (DOCS / f"{name}.md").read_text(encoding="utf-8")
        m = IMG.search(md)
        assert m, f"docs/{name}.md has no live-graph embed"
        assert m.group(1) == "graph-embed.html"


def test_no_other_docs_page_carries_one():
    """A page gaining the markup without being intended to is drift; so is a page in
    the list quietly losing it. Both directions are pinned by comparing the whole set."""
    have = sorted(p.stem for p in DOCS.glob("*.md") if IMG.search(p.read_text(encoding="utf-8")))
    assert have == sorted(EMBED_PAGES)


def test_the_placeholder_reserves_its_box_and_describes_itself():
    """`width`/`height` give the image an aspect ratio before it loads, and the iframe
    that replaces it is sized in CSS. Without both, the swap moves everything under it.
    `alt` is what a screen-reader user gets, since the swap never happens for them."""
    for name in EMBED_PAGES:
        tag = IMG.search((DOCS / f"{name}.md").read_text(encoding="utf-8")).group(0)
        for attr in ("width=", "height=", "alt="):
            assert attr in tag, f"docs/{name}.md: embed image has no {attr}"
        alt = re.search(r'\balt="([^"]*)"', tag).group(1)
        assert len(alt) > 30, f"docs/{name}.md: alt text is too thin to describe the graph"
    css = (SITE / "docs.css").read_text(encoding="utf-8")
    assert "iframe.graph-embed" in css and "height:" in css


def test_both_referenced_files_reach_the_built_site():
    """A wrong `src` renders a broken image and a wrong `data-embed` renders nothing at
    all, and neither raises.

    Neither file can be checked in `site/`: `graph.jpg` is synced there at build time
    from `docs/img/`, and it is the build output that is untracked. So each is resolved
    where git actually keeps it, plus the builder's own sync list for the image.
    """
    assert (SITE / "graph-embed.html").exists()          # tracked, .gitignore exempts it
    assert (DOCS / "img" / "graph.jpg").exists()         # tracked source of site/graph.jpg
    shared = re.search(r"SHARED_IMG\s*=\s*\[(.*?)\]", _builder_source(), re.S)
    assert shared, "build_docs no longer has a SHARED_IMG list"
    assert '"graph.jpg"' in shared.group(1), \
        "graph.jpg is not synced into site/, so every embed placeholder would 404"


def test_the_builder_ships_the_swap_script_only_with_the_markup():
    """The gate is a marker in the rendered body. Exercised through the builder's real
    condition rather than described, and in both directions: the attribute name alone
    (which is prose in the changelog, inside a `<code>` tag) must not be enough."""
    cond = re.search(r"EMBED = EMBED_JS if (.+?) else \"\"", _builder_source())
    assert cond, "build_docs no longer gates EMBED_JS on a body marker"
    expr = cond.group(1)

    def emits(body):
        return eval(expr, {}, {"body": body})       # noqa: S307 - the builder's own line

    assert emits('<img class="shot" src="graph.jpg" data-embed="graph-embed.html">')
    # The case the second term exists for: a page QUOTING the markup, which is what a
    # docs page documenting this feature would contain. The changelog's own mention is
    # `<code>data-embed</code>` with no `=`, so it does not exercise the gate at all --
    # a first version of this test used that string, and the loosened condition passed
    # every assertion. The fixture has to contain the case the guard protects.
    assert not emits('<pre><code>&lt;img data-embed="graph-embed.html"&gt;</code></pre>')
    assert not emits("<p>the <code>data-embed</code> attribute</p>")
    assert not emits('<img class="shot" src="graph.jpg">')
    assert not emits("<p>nothing here</p>")
