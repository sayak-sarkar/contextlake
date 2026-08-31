"""The shell grid must not reserve a track for something out of flow (E13).

`.cl-shell` is a grid. The rail fills its first track and the drawer fills a
third one, but both go `position: fixed` at narrow widths and leave the grid.
An override that still names their columns then reserves space for something
that is not there.

That is what put `#app` in a 64px track on a phone. `[data-rail="collapsed"]
.cl-shell` (specificity 0,2,0) sat outside every media query, so it outranked
`.cl-shell { grid-template-columns: 1fr }` (0,1,0) inside
`@media (max-width: 767px)`. **Specificity is resolved before any media query is
considered**, so an unscoped rule wins at every width no matter where it sits in
the file.

Measured in a browser at a 700px viewport before the fix, three of the four
rail/drawer combinations were wrong:

| rail | drawer | `#app` width |
|---|---|---|
| open | closed | 685px, correct |
| open | open | 224px |
| collapsed | closed | 64px |
| collapsed | open | 64px |

After scoping, all four give 685px, and 1000px and 1400px were re-checked to
confirm the desktop layout did not regress.

These are source assertions because there is no browser in this suite. They pin
the property that was violated, which is *where* the rules live, rather than
restating the declarations.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"
       / "dashboard" / "static" / "dashboard.css").read_text(encoding="utf-8")


def _blocks(condition: str) -> list[str]:
    """The bodies of every `@media` block whose condition contains `condition`."""
    out = []
    for m in re.finditer(r"@media\s*\(([^)]*)\)\s*\{", CSS):
        if condition not in m.group(1):
            continue
        depth, i = 1, m.end()
        while i < len(CSS) and depth:
            if CSS[i] == "{":
                depth += 1
            elif CSS[i] == "}":
                depth -= 1
            i += 1
        out.append(CSS[m.end():i - 1])
    return out


def _unscoped() -> str:
    """The stylesheet with every @media block removed.

    What is left applies at all widths, which is the whole question here.
    """
    out, i = [], 0
    while i < len(CSS):
        m = re.compile(r"@media[^{]*\{").search(CSS, i)
        if not m:
            out.append(CSS[i:])
            break
        out.append(CSS[i:m.start()])
        depth, j = 1, m.end()
        while j < len(CSS) and depth:
            if CSS[j] == "{":
                depth += 1
            elif CSS[j] == "}":
                depth -= 1
            j += 1
        i = j
    return "".join(out)


def test_the_collapsed_rail_track_is_scoped_to_widths_where_the_rail_is_in_flow():
    """Below 768px the rail is fixed, so no rule may name its column."""
    assert '[data-rail="collapsed"] .cl-shell' not in _unscoped(), (
        'the collapsed-rail grid override must sit inside a min-width media '
        'query. Unscoped it outranks the single-column rule for phones, and #app '
        'lands in the 64px track the rail no longer occupies.'
    )
    assert any('[data-rail="collapsed"] .cl-shell' in b for b in _blocks("min-width: 768px")), (
        "the collapsed-rail override should live in @media (min-width: 768px)"
    )


def test_the_drawer_track_is_scoped_to_widths_where_the_drawer_is_in_flow():
    """Below 1280px the drawer is fixed, so the 380px track must not exist."""
    unscoped = _unscoped()
    assert ".cl-shell:has(.cl-drawer" not in unscoped, (
        "the drawer grid track must sit inside a min-width media query. Unscoped "
        "it reserved 380px for a drawer that is position:fixed at that width."
    )
    assert any("380px" in b for b in _blocks("min-width: 1280px")), (
        "the 380px drawer track should live in @media (min-width: 1280px)"
    )


def test_the_phone_breakpoint_still_collapses_the_shell_to_one_column():
    """The rule the specificity bug was defeating."""
    assert any(re.search(r"\.cl-shell\s*\{[^}]*grid-template-columns:\s*1fr", b)
               for b in _blocks("max-width: 767px")), (
        "@media (max-width: 767px) must still give .cl-shell a single column"
    )


def test_forced_colors_is_handled():
    """`forced-colors` had zero rules; counted at runtime in a browser, not read."""
    blocks = _blocks("forced-colors: active")
    assert blocks, "the dashboard needs a @media (forced-colors: active) block"
    body = "\n".join(blocks)
    # The trust bar is the one place that separated things by background-color
    # alone, so it is the one place a forced palette actually loses information.
    assert ".cl-trustbar__seg" in body, (
        "the trust bar's segments are told apart only by background-color, which "
        "a forced palette replaces, so they need a non-colour divider"
    )
    assert "CanvasText" in body or "Highlight" in body, (
        "a forced-colors block should use system colour keywords; brand tokens "
        "are exactly what the mode overrides"
    )


def test_the_forced_colors_selectors_are_not_dead():
    """A rule that matches nothing fails silently.

    The class is checked against the markup the dashboard actually renders,
    which is where it is written.
    """
    js = (Path(__file__).resolve().parents[2] / "src" / "contextlake" / "kb"
          / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "cl-trustbar__seg" in js, (
        "dashboard.js no longer renders .cl-trustbar__seg, so the forced-colors "
        "rule targeting it is a no-op"
    )
