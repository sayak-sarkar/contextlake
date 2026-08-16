"""CSS, HTML and Nix: the tier whose "definitions" are not code symbols.

These use purpose-built kinds (`css_class`, `css_id`, `css_element`, `html_id`,
`nix_attr`) rather than borrowing `class` or `global_variable`. A CSS class selector is
not a class in the OOP sense, and folding it in would make `--kind class` start returning
stylesheet selectors, which costs every existing filter its precision.

They also do NOT go through the tree-sitter definition query. They cannot: in CSS the
pseudo-class in `a.nav:hover` is the same `class_name` node as the real class in `.nav`,
so a node type cannot tell a selector from a pseudo-class. Extraction is code, in
`_member_symbols`, which returns the kind in the tuple.
"""

from __future__ import annotations

from contextlake.kb.parse import parse_source

CSS = b""".btn-primary { color: red; }
#main { margin: 0; }
button { padding: 1px; }
a.nav:hover { color: blue; }
"""

HTML = b"""<div id="main" class="wrap grid">
  <button class="btn-primary" id="go">Go</button>
</div>
"""

NIX = b"""{
  packageName = "demo";
  buildInputs = [ pkgs.gcc ];
}
"""


def _syms(lang, path, src):
    nodes, _e, _c, _i = parse_source("r", path, src, lang)
    return {(n.kind, n.name) for n in nodes if n.kind != "file"}


def test_css_selectors_split_into_three_kinds():
    got = _syms("css", "a.css", CSS)
    assert ("css_class", "btn-primary") in got
    assert ("css_id", "main") in got
    assert ("css_element", "button") in got


def test_a_pseudo_class_is_not_a_css_class():
    """THE LOAD-BEARING ASSERTION. `a.nav:hover` holds two `class_name` nodes: the real
    class `nav`, and the pseudo-class `hover`. A query on the node type captures both and
    invents a class called `hover` on every hover rule in the codebase, which is a name
    nobody wrote appearing in a graph whose claim is that its contents came from source."""
    got = _syms("css", "a.css", CSS)
    assert ("css_class", "nav") in got, "the real class in `a.nav:hover` was missed"
    assert ("css_class", "hover") not in got, "the pseudo-class became a CSS class"


def test_a_compound_selector_yields_both_its_parts():
    """`a.nav:hover` styles `a` elements carrying `.nav`, so both are real referents."""
    got = _syms("css", "a.css", CSS)
    assert ("css_element", "a") in got
    assert ("css_class", "nav") in got


def test_html_ids_are_definitions():
    got = _syms("html", "a.html", HTML)
    assert ("html_id", "main") in got
    assert ("html_id", "go") in got


def test_html_classes_are_not_definitions():
    """A page USES `.btn-primary`; a stylesheet DEFINES it. Emitting the use as a
    definition would give the name two definitions and make "where is this defined"
    ambiguous. The reference edge is separate work, and until it lands this is the
    honest state rather than a half-built one."""
    got = _syms("html", "a.html", HTML)
    assert not [n for k, n in got if k in ("css_class", "html_class")], (
        f"an HTML class attribute became a definition: {got}")


def test_nix_attribute_names():
    got = _syms("nix", "a.nix", NIX)
    assert ("nix_attr", "packageName") in got
    assert ("nix_attr", "buildInputs") in got
