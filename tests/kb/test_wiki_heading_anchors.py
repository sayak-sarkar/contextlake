"""`_md_to_html` gives every heading a stable, unique `id`.

Without one, nothing inside a rendered wiki page can be linked to -- which is the actual
blocker under the roadmap's "diagram-node to wiki-SECTION jump" (V3), not the jump itself.

The uniqueness half is not a nicety. This renderer also renders arbitrary repository
READMEs (`kb/dashboard/data.py`), so two headings reading the same thing is ordinary, and
duplicate `id`s are a WCAG 4.1.1 failure that additionally makes any link to them land
non-deterministically.
"""

import re

from contextlake.kb.visualize.html_render import _md_to_html, _slugify


def _ids(html: str) -> list[str]:
    return re.findall(r'<h\d id="([^"]*)"', html)


# --- the slug itself -------------------------------------------------------------------

def test_a_heading_anchors_on_its_words():
    assert _slugify("Entry points and how to run it") == "entry-points-and-how-to-run-it"


def test_markdown_punctuation_is_stripped_not_slugified():
    """The id comes from the raw heading text, so the markup a reader never sees must not
    reach the anchor."""
    assert _slugify("`parse.py` internals") == "parse-py-internals"
    assert _slugify("**Bold** heading") == "bold-heading"
    assert _slugify("[link](https://example.invalid)") == "link"


def test_dots_and_slashes_separate_rather_than_vanish():
    """`parse.py` must anchor on `parse-py`, not `parsepy`. Dotted names and paths are the
    common case in these headings, and eliding the separator runs words together into
    something a reader would never guess."""
    assert _slugify("`parse.py` internals") == "parse-py-internals"
    assert _slugify("API / Design notes") == "api-design-notes"
    assert _slugify("kb:embed") == "kb-embed"


def test_non_ascii_is_kept_not_transliterated():
    """`id` allows it and browsers resolve it. Transliterating would hand an author an
    anchor they cannot predict for a heading in their own language."""
    assert _slugify("Café résumé") == "café-résumé"


def test_a_heading_with_no_words_still_gets_an_anchor():
    assert _slugify("🎉") == "section"
    assert _slugify("   ") == "section"
    assert _slugify("") == "section"


def test_runs_of_separators_collapse():
    assert _slugify("a--b__c") == "a-b-c"


# --- the document ----------------------------------------------------------------------

def test_every_heading_in_a_document_gets_an_id():
    html = _md_to_html("# One\n\ntext\n\n## Two\n\n### Three\n")
    assert _ids(html) == ["one", "two", "three"]


def test_repeated_headings_are_disambiguated_not_duplicated():
    html = _md_to_html("## Overview\n\n## Overview\n\n## Overview\n")
    assert _ids(html) == ["overview", "overview-2", "overview-3"]


def test_no_document_ever_emits_a_duplicate_id():
    """The property the WCAG rule actually cares about, asserted over a document built to
    collide in several ways at once: same words, same words after punctuation is stripped,
    and two headings that both fall back."""
    md = "\n\n".join([
        "# Overview", "## Overview", "## overview", "## OVERVIEW",
        "## `overview`", "## 🎉", "## ✨", "## a.b", "## a/b",
    ])
    ids = _ids(_md_to_html(md))
    assert len(ids) == 9
    assert len(set(ids)) == 9, f"duplicate id emitted: {ids}"


def test_the_same_input_always_produces_the_same_ids():
    """A link into a wiki page has to survive the page being regenerated, so the anchor
    maps the words and not the position."""
    md = "# Alpha\n\n## Beta\n\n## Alpha\n"
    assert _ids(_md_to_html(md)) == _ids(_md_to_html(md))


def test_the_heading_text_is_still_rendered_normally():
    """The id is additive. Adding it must not change what a reader sees."""
    html = _md_to_html("## `parse.py` internals\n")
    assert "<code>parse.py</code> internals" in html


def test_an_id_cannot_break_out_of_its_attribute():
    """The renderer's whole premise is that its input is untrusted (LLM-derived from repo
    content). A heading carrying a quote must not escape the `id="..."`."""
    html = _md_to_html('## evil" onload="alert(1)\n')
    assert 'onload=' not in html.split(">", 1)[0]
    for got in _ids(html):
        assert '"' not in got and "<" not in got and ">" not in got
