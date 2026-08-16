"""An HTML page and the stylesheet it uses, joined across files.

A stylesheet DEFINES `.btn-primary`; a page USES it. Emitting the use as a definition
would give one name two definitions and make "where is this defined" ambiguous, so the use
is an unresolved reference resolved repo-wide, exactly like a call into another file.

That join is the point of the pair. Neither side answers "which stylesheet defines the
class this page uses" alone, and neither answers "which stylesheet styles every button".
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import index_repo_dir

CSS = b""".btn-primary { color: red; }
#main { margin: 0; }
button { padding: 1px; }
"""

# `class="wrap btn-primary"` is TWO names and is deliberately multi-valued: with a single
# class per attribute, an extractor that forgets to split still resolves everything and the
# split goes untested. Break-tested, and the first version of this fixture failed to catch
# exactly that.
HTML = b"""<div id="main" class="wrap">
  <button class="wrap btn-primary">Go</button>
</div>
"""


@pytest.fixture(scope="module")
def shard(tmp_path_factory):
    d = tmp_path_factory.mktemp("styles")
    (d / "site.css").write_bytes(CSS)
    (d / "page.html").write_bytes(HTML)
    return index_repo_dir(str(d), "demo", head_commit="h")


def _edges(shard, relation):
    by = {n.id: n for n in shard.nodes}
    return {(by[e.src].name, by[e.dst].kind, by[e.dst].name)
            for e in shard.edges
            if e.relation == relation and e.src in by and e.dst in by}


def test_the_page_references_the_class_the_stylesheet_defines(shard):
    """THE LOAD-BEARING ASSERTION: a cross-FILE edge, which is the whole reason these two
    languages are worth indexing together."""
    assert ("page.html", "css_class", "btn-primary") in _edges(shard, "references")


def test_the_page_references_the_element_rule_that_styles_it(shard):
    """A CSS rule keyed on `button` styles every button, so the tag is a use of it."""
    assert ("page.html", "css_element", "button") in _edges(shard, "references")


def test_a_multi_valued_class_attribute_is_split(shard):
    """`class="wrap btn-primary"` holds two names. Unsplit, the whole string resolves to
    nothing and the real edge is silently lost, which looks identical to a page that uses
    no styles at all."""
    assert ("page.html", "css_class", "btn-primary") in _edges(shard, "references")


def test_a_class_no_stylesheet_defines_produces_no_edge(shard):
    """`wrap` is used and never defined. An unresolved name must resolve to NOTHING rather
    than to a node invented to receive it, which is the failure mode that makes a graph
    look complete while being wrong."""
    refs = _edges(shard, "references")
    assert not [r for r in refs if r[2] == "wrap"], f"an undefined class got an edge: {refs}"


def test_the_stylesheet_still_owns_its_selectors(shard):
    """The reference edges must not replace containment: the CSS file is where these are
    defined, and that is what makes the reference resolvable in the first place."""
    contains = _edges(shard, "contains")
    assert ("site.css", "css_class", "btn-primary") in contains
    assert ("site.css", "css_id", "main") in contains


def test_the_reference_is_inferred_not_extracted(shard):
    """It is resolved by NAME across files, which is the definition of inferred here. A
    reader deciding whether to trust the edge needs that distinction, and the project's
    whole confidence vocabulary rests on not blurring it."""
    by = {n.id: n for n in shard.nodes}
    style_refs = [e for e in shard.edges
                  if e.relation == "references" and e.dst in by
                  and by[e.dst].kind.startswith("css_")]
    assert style_refs, "no style references at all; the rest of this file proves nothing"
    assert all(e.confidence.name == "INFERRED" for e in style_refs), (
        f"a name-resolved cross-file edge claimed to be extracted: "
        f"{[e.confidence for e in style_refs]}")
