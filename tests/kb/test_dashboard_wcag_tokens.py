"""WCAG 2.2 AA regression guards for the dashboard's colour tokens and roles.

These assertions exist because a colour token is the easiest thing in this
codebase to "improve" back into a failure: the values look arbitrary, the
relationships between them are invisible in the CSS text, and nothing else in
the suite reads a hex code. Each check restates, in the same terms the audit
measured, why a value is what it is.

Contrast is computed here rather than eyeballed anywhere: the WCAG relative
luminance formula, and CSS `color-mix(in srgb, A p%, B)` as a plain sRGB channel
interpolation, which is what the browser paints and therefore what the eye sees.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb" / "dashboard" / "static"
CSS = (STATIC / "dashboard.css").read_text(encoding="utf-8")
JS = (STATIC / "dashboard.js").read_text(encoding="utf-8")
HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")


def _js_code(src: str) -> str:
    """dashboard.js with comments removed.

    The comments in that file quote the roles they exist to warn against, so a
    plain substring search over the raw text would fail on its own explanation.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))


JS_CODE = _js_code(JS)
HTML_CODE = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)


# ---- colour maths ---------------------------------------------------------
def _channel(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def mix(front: str, back: str, fraction: float) -> str:
    """color-mix(in srgb, front <fraction>%, back) -> hex."""
    f, b = front.lstrip("#"), back.lstrip("#")
    out = []
    for i in (0, 2, 4):
        fv, bv = int(f[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(round(fv * fraction + bv * (1 - fraction)))
    return "#" + "".join(f"{v:02x}" for v in out)


# ---- token extraction -----------------------------------------------------
def _block(selector: str) -> str:
    """The declaration body of the first rule whose selector matches exactly."""
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert match, f"no rule found for {selector!r} in dashboard.css"
    return match.group(1)


def token(name: str, theme: str = "light") -> str:
    body = _block(":root") if theme == "light" else _block('[data-theme="dark"]')
    match = re.search(r"--" + re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", body)
    assert match, f"--{name} is not defined in the {theme} theme block"
    return match.group(1)


LIGHT = {name: token(name) for name in
         ("cl-bg", "cl-surface", "cl-surface-2", "cl-ink", "cl-ink-muted",
          "cl-line-strong", "cl-primary", "cl-on-lake")}
DARK = {name: token(name, "dark") for name in
        ("cl-bg", "cl-surface", "cl-surface-2", "cl-ink", "cl-ink-muted",
         "cl-line-strong", "cl-primary", "cl-on-lake", "cl-lake")}
LIGHT["cl-lake"] = token("cl-lake")

AA_TEXT = 4.5
AA_NON_TEXT = 3.0


# ---- V2: --cl-line-strong is the boundary token ---------------------------
@pytest.mark.parametrize("theme, palette", [("light", LIGHT), ("dark", DARK)])
def test_line_strong_delimits_a_control_against_every_surface_it_sits_on(theme, palette):
    """V2, WCAG 1.4.11.

    A control's fill is the same colour as the surface behind it, so its border is
    the only thing that says "this is a text box". The old shared --cl-line
    measured 1.32:1 against #ffffff. Every surface a bordered control can appear
    on has to clear 3:1, not just the one that was convenient to measure.
    """
    border = palette["cl-line-strong"]
    for surface_name in ("cl-surface", "cl-surface-2", "cl-bg"):
        ratio = contrast(border, palette[surface_name])
        assert ratio >= AA_NON_TEXT, (
            f"{theme}: --cl-line-strong {border} vs --{surface_name} "
            f"{palette[surface_name]} is {ratio:.2f}:1, under {AA_NON_TEXT}:1"
        )


def test_line_stays_decorative_and_separate_from_line_strong():
    """V2. Collapsing the two back into one token is the regression this guards.

    --cl-line is deliberately still light: it draws card edges and table rules
    across ten lenses, and darkening it to 3:1 would shout every one of them.
    That is only defensible while a separate strong token carries the controls.
    """
    assert token("cl-line") != token("cl-line-strong")
    assert token("cl-line", "dark") != token("cl-line-strong", "dark")


@pytest.mark.parametrize("selector", [
    ".cl-pinchip, .cl-cmdk, .cl-iconbtn, .cl-gt",
    ".cl-groundtruth",
    ".cl-modeseg",
    ".cl-trustbar__track",
    ".cl-cite",
    ".cl-select",
    ".cl-pathinput",
    ".cl-searchfield",
    ".cl-btn",
])
def test_every_bounded_control_uses_the_strong_boundary(selector):
    """V2. The token is worthless if a control quietly goes back to --cl-line."""
    body = _block(selector)
    assert "var(--cl-line-strong)" in body, f"{selector} does not use --cl-line-strong"
    assert not re.search(r"border[^;]*var\(--cl-line\)", body), (
        f"{selector} still draws a border with the decorative --cl-line"
    )


# ---- V3 / V4: the lake and what is painted on it --------------------------
def test_dark_theme_defines_its_own_lake():
    """V3, WCAG 1.4.11.

    The light lake on the dark card surface measured 2.66:1, and it reached four
    call sites at once (primary button, selected-tab underline, edge-weight bar,
    path step ring) because the token was never redefined for dark.
    """
    ratio = contrast(DARK["cl-lake"], DARK["cl-surface"])
    assert ratio >= AA_NON_TEXT, (
        f"dark --cl-lake {DARK['cl-lake']} vs card surface {DARK['cl-surface']} "
        f"is {ratio:.2f}:1, under {AA_NON_TEXT}:1"
    )
    # the edge-weight bar is also read on a hovered table row
    assert contrast(DARK["cl-lake"], DARK["cl-surface-2"]) >= AA_NON_TEXT


@pytest.mark.parametrize("theme, palette", [("light", LIGHT), ("dark", DARK)])
def test_on_lake_is_readable_on_its_own_themes_lake(theme, palette):
    """V3. The paired token is not optional.

    White clears 4.5:1 on the light lake but not on the lighter dark one, so a
    hardcoded `color: #fff` would turn the fixed 1.4.11 failure into a new 1.4.3
    failure on the primary button, the lettermark and the selected segment.
    """
    ratio = contrast(palette["cl-on-lake"], palette["cl-lake"])
    assert ratio >= AA_TEXT, (
        f"{theme}: --cl-on-lake {palette['cl-on-lake']} on --cl-lake "
        f"{palette['cl-lake']} is {ratio:.2f}:1, under {AA_TEXT}:1"
    )


def test_nothing_paints_hardcoded_white_on_a_lake_fill():
    """V3. The three sites that used to: .cl-btn--primary, .cl-lettermark,
    .cl-modeseg button[aria-pressed="true"]."""
    for line in CSS.splitlines():
        if "var(--cl-lake)" in line and re.search(r"color:\s*#fff", line):
            pytest.fail(f"hardcoded white on a lake fill: {line.strip()}")


@pytest.mark.parametrize("theme, palette", [("light", LIGHT), ("dark", DARK)])
def test_path_step_number_is_readable_on_its_own_tint(theme, palette):
    """V4, WCAG 1.4.3.

    The hop number is read on the ring's own color-mix(lake 16%, transparent)
    tint, not on the bare card, so the tint is the backdrop that counts. Against
    it the lake itself measures 4.05:1 light / 3.91:1 dark -- BOTH themes fail,
    which is why the number moved to --cl-primary and the ring kept the lake.
    """
    tint = mix(palette["cl-lake"], palette["cl-surface"], 0.16)
    ratio = contrast(palette["cl-primary"], tint)
    assert ratio >= AA_TEXT, (
        f"{theme}: step number {palette['cl-primary']} on composited tint {tint} "
        f"is {ratio:.2f}:1, under {AA_TEXT}:1"
    )
    assert contrast(palette["cl-lake"], palette["cl-surface"]) >= AA_NON_TEXT


# ---- V1 / V5: opacity is never a state encoding ---------------------------
@pytest.mark.parametrize("selector", [
    '.cl-gt[aria-pressed="false"]',
    '.cl-trustbar__seg[aria-pressed="false"]',
])
def test_off_states_do_not_use_opacity(selector):
    """V1 and V5.

    At opacity .45 a switched-off ground-truth chip faded its own label to
    2.62:1 -- unreadable exactly when the user needs to read which filter they
    just turned off. At opacity .3 the trust-bar segments' only state cue
    composited to 1.21-1.83:1. These are active controls, so 1.4.3's
    inactive-component exemption does not apply to either.
    """
    body = _block(selector)
    assert not re.search(r"(^|[\s;])opacity\s*:", body), (
        f"{selector} encodes its off state with opacity again: {body.strip()}"
    )


@pytest.mark.parametrize("theme, palette", [("light", LIGHT), ("dark", DARK)])
def test_off_chip_label_stays_readable(theme, palette):
    """V1. The replacement cue is strike-through plus muted ink, and the ink has
    to clear the 4.5:1 the opacity was destroying."""
    ratio = contrast(palette["cl-ink-muted"], palette["cl-surface-2"])
    assert ratio >= AA_TEXT, f"{theme}: off-chip label is {ratio:.2f}:1"
    assert "line-through" in _block('.cl-gt[aria-pressed="false"]'), (
        "the non-colour half of the off cue is gone"
    )


@pytest.mark.parametrize("theme, palette", [("light", LIGHT), ("dark", DARK)])
def test_off_trustbar_segment_carries_a_visible_hatch(theme, palette):
    """V5, WCAG 1.4.11. The hatch has to read against the fill it overlays."""
    body = _block('.cl-trustbar__seg[aria-pressed="false"]')
    assert "repeating-linear-gradient" in body
    assert "background-color:" in body and "background-image:" in body, (
        "the `background` shorthand would reset background-clip and undo the "
        "content-box clip that keeps the segment's 26px hit box"
    )
    ratio = contrast(palette["cl-ink"], palette["cl-surface-2"])
    assert ratio >= AA_NON_TEXT, f"{theme}: hatch vs emptied fill is {ratio:.2f}:1"


# ---- V7 / V8 / V9 / V12: roles that lied ----------------------------------
def test_no_explicit_role_overrides_a_buttons_own_role():
    """V7, WCAG 4.1.2.

    role="listitem" on a <button> REPLACES the button role rather than adding to
    it -- Chrome's accessibility tree showed the fleet's primary navigation as
    plain list items. Real <ul>/<li> wrappers keep both.
    """
    assert "listitem" not in JS_CODE
    assert 'role: "list"' not in JS_CODE


def test_no_tab_roles_without_tabpanels():
    """V8, WCAG 4.1.2 / 1.3.1. Three strips announced a tab structure the
    document never had: 0 tabpanels, aria-controls null, tabindex unmanaged, no
    arrow-key handler. They are labelled groups of toggle buttons."""
    assert 'role: "tab"' not in JS_CODE
    assert 'role: "tablist"' not in JS_CODE
    assert "aria-selected" not in _block('.cl-tab[aria-pressed="true"]')
    assert 'role="tab"' not in HTML_CODE


def test_search_results_do_not_nest_a_button_in_a_button():
    """V9, WCAG 4.1.2. The outer button's name used to absorb the inner "Blast"
    label and advertise an action the row does not perform."""
    assert 'class: "cl-result__main"' in JS
    start = JS.index("res.results.forEach")
    fragment = JS[start:start + 1200]
    assert 'class: "cl-result"' in fragment
    assert re.search(r'h\("div",\s*\{\s*class:\s*"cl-result"', fragment), (
        "the search result row is a button again"
    )


def test_info_popover_is_a_disclosure_not_a_dialog():
    """V12, WCAG 2.4.3 / 4.1.2. Nothing about the panel is modal, and it left
    focus outside itself, so AT announced a dialog the user was never in."""
    tag = re.search(r'<div[^>]*id="cl-infopop"[^>]*>', HTML_CODE)
    assert tag, "the info popover element is gone"
    assert 'role="dialog"' not in tag.group(0)
    assert 'role="group"' in tag.group(0)
    assert '$("#cl-info-close").focus()' in JS_CODE, "opening it must move focus into it"
    # the provenance drawer's own role="dialog" is correct and stays: it moves
    # focus to its close button on open and restores it on close.
    assert 'id="cl-drawer"' in HTML_CODE and 'role="dialog"' in HTML_CODE


# ---- V10 / V11: names and shortcuts ---------------------------------------
def test_command_palette_button_is_named_by_its_visible_text():
    """V10, WCAG 2.5.3. It said "Ask the lake" and was named "Search the lake"."""
    match = re.search(r'<button[^>]*id="cl-cmdk"[^>]*>', HTML)
    assert match and "aria-label" not in match.group(0)


def test_density_button_name_is_rebuilt_from_its_visible_word():
    """V10. The visible word flips; the old aria-label stayed frozen."""
    assert 'aria-label", word + " density"' in JS


def test_single_key_shortcuts_can_be_turned_off():
    """V11, WCAG 2.1.4. The opt-out lives in the info popover, not the Settings
    lens -- Settings is live-only, so a control there would not exist in the
    static export, where the shortcuts still fire."""
    assert 'id="cl-shortcuts-toggle"' in HTML
    assert 'lsSet("shortcuts"' in JS and 'lsGet("shortcuts"' in JS
    assert "if (!shortcutsOn()) return;" in JS


def test_shortcut_guard_covers_every_text_entry_surface():
    """V11. The old guard was matches("input, textarea"), which missed <select>
    typeahead and contenteditable, and did not look at ancestors."""
    body = JS[JS.index("function inTextEntry"):]
    body = body[:body.index("\n    }")]
    for surface in ("input", "textarea", "select", "contenteditable"):
        assert surface in body, f"the shortcut guard does not cover {surface}"
    assert ".closest(" in body, "matches() misses children of a contenteditable"


# ---- V6 / V13 -------------------------------------------------------------
def test_edge_weight_renders_as_text_not_only_as_a_bar():
    """V6, WCAG 1.1.1. The Weight cell was a 6-60px bar and nothing else, so a
    screen reader read an empty cell and a magnifier user could not compare two
    bars that are off-screen from each other. The number was in the data all
    along."""
    assert 'class: "cl-flowcell"' in JS
    start = JS.index('class: "cl-flowcell"')
    fragment = JS[start:start + 400]
    assert '"aria-hidden": "true"' in fragment, "the bar must not be read as content"
    assert "num(e.weight" in fragment, "the number itself is what carries the value"


def test_state_blocks_speak_through_the_persistent_live_region():
    """V13, WCAG 4.1.3. A live region created together with its content is the
    one thing screen readers do not announce, and the panel bodies it landed in
    are not live regions either."""
    assert "function announceState" in JS
    body = JS[JS.index("function stateBlock"):]
    body = body[:body.index("\n  }")]
    assert "announceState(" in body


# --- static-asset parity (V6) ----------------------------------------------------------
#
# Both of these failed silently while building the treemap, which is why they are guards
# rather than notes. A `<use href="#ui-treemap">` pointing at a symbol that does not exist
# renders nothing at all -- no console error, just a button with a missing icon. And an
# undefined custom property inside `color-mix()` makes the whole declaration invalid, so
# CSS drops it and the tile renders with no fill.


_STATIC = Path(__file__).resolve().parents[2] / "src/contextlake/kb/dashboard/static"


def _js():
    return (_STATIC / "dashboard.js").read_text(encoding="utf-8")


def _css():
    return (_STATIC / "dashboard.css").read_text(encoding="utf-8")


def _html():
    return (_STATIC / "dashboard.html").read_text(encoding="utf-8")


def test_every_icon_the_js_references_exists_in_the_sprite():
    have = set(re.findall(r'id="(ui-[a-z-]+)"', _html()))
    want = set(re.findall(r'"(ui-[a-z-]+)"', _js()))
    missing = sorted(want - have)
    assert not missing, f"referenced but not in the sprite (renders blank): {missing}"


def test_every_custom_property_the_css_uses_is_defined():
    css = _css()
    defined = set(re.findall(r"(--cl-[a-z0-9-]+)\s*:", css))
    bare = set(re.findall(r"var\(\s*(--cl-[a-z0-9-]+)\s*\)", css))
    missing = sorted(bare - defined)
    assert not missing, f"used with no fallback and never defined: {missing}"


def test_the_fleet_layout_modes_and_their_buttons_stay_in_step():
    """MODES drives aria-pressed by INDEX, so a list that drifts mislabels every button."""
    js = _js()
    modes = re.search(r'var MODES = \[([^\]]+)\]', js).group(1)
    modes = re.findall(r'"([a-z]+)"', modes)
    buttons = re.search(r'\[\["cards".*?\]\]\.forEach', js, re.S).group(0)
    labelled = re.findall(r'\["([a-z]+)", "[A-Za-z]+", "ui-[a-z-]+"\]', buttons)
    assert modes == labelled, (
        f"MODES {modes} and the button list {labelled} disagree; aria-pressed is set by "
        "index, so the wrong button would read as selected")
