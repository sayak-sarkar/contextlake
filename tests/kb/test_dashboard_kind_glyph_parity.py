"""KIND_GLYPHS and the SVG sprite must agree (G13, and a regression guard).

`kindIcon` is `KIND_GLYPHS[kind] ? kind : "file"`, so registering a kind in
KIND_GLYPHS is what DISABLES the generic file fallback. Registering a kind there
without adding its sprite symbol therefore makes things worse than leaving it
unregistered: instead of a file icon the browser renders `<use href="#g-thing">`
against a symbol that does not exist, which is a blank box.

That is not hypothetical -- it happened when `config_key` and `test` were added to
KIND_GLYPHS in this cycle and the sprite was not updated with them. Regex over the two
files, so this needs no JavaScript runtime.
"""

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[2] / "src/contextlake/kb/dashboard/static"
_JS = _STATIC / "dashboard.js"
_HTML = _STATIC / "dashboard.html"


def _glyph_kinds() -> set[str]:
    m = re.search(r"var KIND_GLYPHS = \{(.*?)\}", _JS.read_text(), re.DOTALL)
    assert m, "KIND_GLYPHS not found; this test's parsing assumption is stale"
    return set(re.findall(r"(\w+)\s*:\s*1", m.group(1)))


def _sprite_ids() -> set[str]:
    return set(re.findall(r'id="g-(\w+)"', _HTML.read_text()))


def test_every_registered_kind_has_a_sprite_symbol():
    missing = sorted(_glyph_kinds() - _sprite_ids())
    assert not missing, (
        f"in KIND_GLYPHS but with no <symbol id='g-...'>: {missing}. These render as a "
        "BLANK BOX, not as the file icon, because registering the kind disables the "
        "fallback. Add the symbol to dashboard.html or remove the kind from KIND_GLYPHS."
    )


def test_the_fallback_symbol_exists():
    """Everything unregistered falls back to `g-file`, so that one is load-bearing."""
    assert "file" in _sprite_ids()


def test_the_kinds_added_this_cycle_are_covered():
    """Named explicitly: these are the two that broke the invariant."""
    assert {"config_key", "test"} <= _glyph_kinds() & _sprite_ids()
