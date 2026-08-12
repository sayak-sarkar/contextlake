"""WCAG 2.2 AA regressions for the graph viewer (`kb/visualize/`, `kb/static/`).

These pin the arithmetic and the markup that a colour tweak or an "obviously
harmless" style edit would silently undo. The contrast maths is re-derived here
rather than quoted, so the numbers in the report stay checkable.

Canvas backdrop: `--cy-bg` is a three-stop radial gradient, so every graphic is
measured against ALL three stops and judged on the worst one. The hardest light
stop is the darkest (#e3f1f2) and the hardest dark stop is the lightest
(#123351) -- deriving a palette against #ffffff alone overstates light ratios by
roughly a tenth and ships a 2.9:1.
"""

from __future__ import annotations

import re

import pytest

from contextlake.kb.visualize import html_render as hr
from contextlake.kb.visualize.payload import to_payload
from contextlake.kb.visualize.styling import (
    DEFAULT_EDGE_COLOR,
    DEFAULT_EDGE_COLOR_DARK,
    FOUND_COLOR,
    FOUND_COLOR_DARK,
    HILITE_COLOR,
    HILITE_COLOR_DARK,
    NODE_BORDER_COLOR,
    NODE_BORDER_COLOR_DARK,
    NS_COLOR,
    NS_COLOR_DARK,
    RELATION_COLORS,
    RELATION_COLORS_DARK,
    SCAFFOLD_EDGE_COLOR,
    SCAFFOLD_EDGE_COLOR_DARK,
)

LIGHT_STOPS = ("#ffffff", "#f1fafb", "#e3f1f2")
DARK_STOPS = ("#123351", "#0C2438", "#081D30")
NON_TEXT_MIN = 3.0          # WCAG 1.4.11
TEXT_MIN = 4.5              # WCAG 1.4.3, small text


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        parts.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = parts
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def worst(color: str, stops) -> float:
    return min(contrast(color, s) for s in stops)


def test_contrast_helper_matches_known_values():
    # black on white is exactly 21:1, and a known WCAG example, so the helper the
    # rest of this file trusts is checked against something independent of it
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.001)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.02)


@pytest.mark.parametrize("relation", sorted(RELATION_COLORS))
def test_light_relation_colours_clear_non_text_contrast(relation):
    """1.4.11: an edge hue is the only static encoding of the relation."""
    got = worst(RELATION_COLORS[relation], LIGHT_STOPS)
    assert got >= NON_TEXT_MIN, f"{relation} {RELATION_COLORS[relation]} = {got:.2f}:1"


@pytest.mark.parametrize("relation", sorted(RELATION_COLORS_DARK))
def test_dark_relation_colours_clear_non_text_contrast(relation):
    got = worst(RELATION_COLORS_DARK[relation], DARK_STOPS)
    assert got >= NON_TEXT_MIN, f"{relation} {RELATION_COLORS_DARK[relation]} = {got:.2f}:1"


def test_both_relation_palettes_cover_the_same_vocabulary():
    # a relation present in one palette and missing from the other renders in the
    # open-vocab fallback colour on that theme only -- a silent per-theme miscolour
    assert set(RELATION_COLORS) == set(RELATION_COLORS_DARK)


@pytest.mark.parametrize(
    "name,color,stops",
    [
        ("fallback edge (light)", DEFAULT_EDGE_COLOR, LIGHT_STOPS),
        ("fallback edge (dark)", DEFAULT_EDGE_COLOR_DARK, DARK_STOPS),
        ("scaffold edge (light)", SCAFFOLD_EDGE_COLOR, LIGHT_STOPS),
        ("scaffold edge (dark)", SCAFFOLD_EDGE_COLOR_DARK, DARK_STOPS),
        ("node border (light)", NODE_BORDER_COLOR, LIGHT_STOPS),
        ("node border (dark)", NODE_BORDER_COLOR_DARK, DARK_STOPS),
        ("selection ring (light)", HILITE_COLOR, LIGHT_STOPS),
        ("selection ring (dark)", HILITE_COLOR_DARK, DARK_STOPS),
        ("search ring (light)", FOUND_COLOR, LIGHT_STOPS),
        ("search ring (dark)", FOUND_COLOR_DARK, DARK_STOPS),
        ("namespace cluster (light)", NS_COLOR, LIGHT_STOPS),
        ("namespace cluster (dark)", NS_COLOR_DARK, DARK_STOPS),
    ],
)
def test_graph_inks_clear_non_text_contrast(name, color, stops):
    got = worst(color, stops)
    assert got >= NON_TEXT_MIN, f"{name} {color} = {got:.2f}:1"


def test_node_border_is_what_supplies_the_boundary():
    """The fills come from the shared kind registry and cannot all be changed.

    So the regression to catch is the border quietly going back to the surface
    colour, which is invisible in light theme and *navy on navy* in dark.
    """
    css = hr._app_css()
    js = hr._app_js()
    assert "EDGE_INK" in js
    assert '"border-color": ink.node' in js
    assert '"border-color": surf' not in js
    assert "--on-brand" in css


def _markup(html: str) -> str:
    """The page's markup only: comments, <script> and <style> stripped.

    Both the HTML comments and the inlined app.js explain *why* a role was
    dropped, and therefore quote the very strings these tests assert are gone. A
    bare substring check over the whole file passes or fails on the prose, which
    is exactly the unanchored-match trap.
    """
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    return re.sub(r"<style\b.*?</style>", "", html, flags=re.S)


def _page(**kw):
    nodes = [
        {"id": "acme/widget-api", "kind": "repo", "name": "acme/widget-api", "deg": 1},
        {"id": "acme/box-service", "kind": "repo", "name": "acme/box-service", "deg": 1},
    ]
    edges = [{"src": "acme/widget-api", "dst": "acme/box-service",
              "relation": "depends_on", "confidence": "INFERRED", "weight": 1.0}]
    return hr.to_html(to_payload(nodes, edges, {"mode": "overview"}), **kw)


def test_canvas_is_not_an_application_region():
    """2.1.1 / 4.1.2.

    role="application" tells AT to suppress browse mode and hand every keystroke
    to the page. The page implemented none, so it announced a named empty region.
    """
    html = _markup(_page())
    assert 'role="application"' not in html
    assert 'id="cy"' in html
    assert 'aria-describedby="cy-help"' in html
    assert 'id="cy-help"' in html


def test_legend_buttons_expose_pressed_state():
    """4.1.2: filtering a whole node kind out used to change only a CSS class."""
    html = _page()
    for button in re.findall(r"<button[^>]*class=\"lg[^\"]*\"[^>]*>", html):
        assert 'aria-pressed="true"' in button, button
    # ...and the live code must write the SAME polarity, or the server-rendered
    # state and the toggled state disagree after the first click
    js = hr._app_js()
    assert 'el.setAttribute("aria-pressed", String(!off))' in js


def test_view_mode_buttons_are_toggles_not_tabs():
    """4.1.2 / 1.3.1: there is no tabpanel, aria-controls or arrow-key model."""
    html = _markup(_page())
    assert 'role="tablist"' not in html
    assert 'role="tab"' not in html
    assert "aria-selected" not in html
    assert 'role="group" aria-label="Overview mode"' in html
    assert 'id="vm-clusters" aria-pressed="true"' in html


def test_page_carries_a_text_alternative_container():
    """1.1.1: the canvas has no accessible content of its own."""
    html = _page()
    assert 'id="textview"' in html
    assert 'id="tv-body"' in html
    assert "Skip to the graph as text" in html
    js = hr._app_js()
    # the list is built from the SAME collection cytoscape paints, and activating
    # an item runs the same handler a mouse tap runs -- that is what makes it an
    # equivalent rather than a description
    assert "function renderTextView()" in js
    assert "activateNode(n, true)" in js
    assert "activateEdge(ed, true)" in js


def test_tooltip_is_dismissable_and_hoverable():
    """1.4.13."""
    css = hr._app_css()
    js = hr._app_js()
    assert re.search(r"#tip\{[^}]*pointer-events:auto", css), "tooltip must be hoverable"
    assert "hideTip(true)" in js, "Escape must dismiss the tooltip"
    assert 'tvBody.addEventListener("focusin"' in js, "tooltip content must reach focus"


def test_confidence_is_line_style_not_opacity():
    """1.4.11: at 0.45 opacity no hue reaches 3:1 over a light canvas."""
    js = hr._app_js()
    for conf in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        block = re.search(
            r'edge\[confidence = "' + conf + r'"\][^}]*\}[^}]*\}', js)
        assert block, conf
        assert '"opacity": 1' in block.group(0), f"{conf} edges must paint opaque"


def test_theme_swap_goes_through_one_entry_point():
    """The theme has four entry points; the palette has to follow all of them."""
    js = hr._app_js()
    assert js.count("function applyTheme(") == 1
    assert "REL_COLORS = (t === \"dark\") ? REL_COLORS_DARK : REL_COLORS_LIGHT;" in js
    # the minimap used to wrap the theme BUTTON's onclick, missing the OS
    # preference, ?theme= and postMessage paths
    assert "themeBtn.onclick = function" not in js


def test_initial_theme_is_re_applied_after_every_hook_is_registered():
    """The OS-preference and ?theme= paths fire while app.js is still evaluating.

    They ran before the legend-repaint and minimap hooks existed, so an OS-dark or
    ?theme=dark FIRST paint drew the canvas in the dark relation palette while the
    legend that documents it kept the server-rendered light hues -- measured: the
    swatch painted #a37914 while the canvas painted #E7B53C. One forced re-apply,
    placed after the last registration, settles every hook.
    """
    js = hr._app_js()
    last_hook = js.rfind("onTheme(")
    settle = js.rfind("applyTheme(themeName(), true)")
    assert last_hook != -1 and settle != -1
    assert settle > last_hook, (
        "applyTheme(themeName(), true) must come after the last onTheme() registration"
    )
