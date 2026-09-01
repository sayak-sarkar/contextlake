"""The live graph embedded in the docs pages that explain the graph.

Four pages described the visualizer in prose while the running thing sat one directory
away, reachable only from the landing page. They now carry it.

`graph-embed.html` is 788 KB, so it is NOT embedded eagerly: the markup ships an
`<img>` and an IntersectionObserver swaps in the iframe when the reader scrolls near
it. A reader who never scrolls there, and a reader with no JS, keeps the screenshot.

**The swap itself is not asserted here, and that is deliberate.** It was verified in a
real browser (Playwright: img replaced, `src="graph-embed.html#theme=light"`, 600 px
tall, and inside the frame four cytoscape canvases, the inspector and the legend).
`--dump-dom`, which every other browser test in this suite uses, does NOT run
IntersectionObserver callbacks -- the SHIPPED landing page, which has used this exact
pattern since long before this change, also reports "still-img" under it. So a
dump-dom assertion here would pin the environment's limitation, not the behaviour.
What is asserted below is everything a static read can settle honestly.
"""

from __future__ import annotations

import pathlib
import re

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"

# The pages that explain the graph. The landing page has carried the embed for longer
# and is checked with them, since it is the same markup contract.
EMBED_PAGES = {
    "asking-the-graph.html",
    "code-graph-model.html",
    "indexing-the-code-graph.html",
    "visualizing-the-graph.html",
}
IMG = re.compile(r'<img[^>]*\bdata-embed="([^"]+)"[^>]*>')


def _pages_with(pattern: str) -> set[str]:
    return {p.name for p in SITE.glob("*.html")
            if pattern in p.read_text(encoding="utf-8")}


def test_the_embed_reaches_exactly_the_pages_that_carry_it():
    """The builder gates the script on a marker in the body, so both halves are pinned:
    the pages that carry the image, and the pages that carry the script. A page gaining
    one without the other is the drift this catches -- a script with nothing to swap, or
    an image that never becomes the live graph.

    The marker requires the opening `<img class="shot"` tag as well as `data-embed=`.
    That is a precaution, not a fix: changelog.html discusses `data-embed` in prose, and
    the looser condition was checked against it and does NOT match, because the prose
    lacks the `=`. A page quoting the full attribute would match, and the extra term
    settles it.
    """
    with_img = {p.name for p in SITE.glob("*.html")
                if IMG.search(p.read_text(encoding="utf-8"))}
    with_js = _pages_with("img[data-embed]")
    assert with_img == EMBED_PAGES | {"index.html"}, f"image on: {sorted(with_img)}"
    assert with_js == EMBED_PAGES, f"script on: {sorted(with_js)}"


def test_every_embed_points_at_files_that_exist():
    """A wrong `src` renders a broken image and a wrong `data-embed` renders nothing at
    all, and neither raises. Both paths are resolved on disk instead."""
    checked = 0
    for page in sorted(EMBED_PAGES | {"index.html"}):
        html = (SITE / page).read_text(encoding="utf-8")
        for m in IMG.finditer(html):
            checked += 1
            target = m.group(1)
            assert (SITE / target).exists(), f"{page}: data-embed={target} does not exist"
            src = re.search(r'\bsrc="([^"]+)"', m.group(0))
            assert src, f"{page}: embed image has no src"
            assert (SITE / src.group(1)).exists(), f"{page}: src={src.group(1)} does not exist"
    assert checked == len(EMBED_PAGES) + 1, f"found {checked} embeds, expected 5"


def test_the_placeholder_reserves_its_box_and_describes_itself():
    """`width`/`height` give the image an aspect ratio before it loads, and the iframe
    that replaces it is sized in CSS. Without both, the swap moves everything under it.
    `alt` is what a screen-reader user gets, since the swap never happens for them."""
    for page in sorted(EMBED_PAGES):
        tag = IMG.search((SITE / page).read_text(encoding="utf-8")).group(0)
        for attr in ("width=", "height=", "alt="):
            assert attr in tag, f"{page}: embed image has no {attr}"
        alt = re.search(r'\balt="([^"]*)"', tag).group(1)
        assert len(alt) > 30, f"{page}: alt text is too thin to describe the graph"
    css = (SITE / "docs.css").read_text(encoding="utf-8")
    assert "iframe.graph-embed" in css and "height:" in css
