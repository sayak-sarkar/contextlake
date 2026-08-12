"""The four copies of the glyph vocabulary must agree (G13, and a regression guard).

`kindIcon` is `KIND_GLYPHS[kind] ? kind : "file"`, so registering a kind in
KIND_GLYPHS is what DISABLES the generic file fallback. Registering a kind there
without adding its sprite symbol therefore makes things worse than leaving it
unregistered: instead of a file icon the browser renders `<use href="#g-thing">`
against a symbol that does not exist, which is a blank box.

That is not hypothetical -- it happened when `config_key` and `test` were added to
KIND_GLYPHS in this cycle and the sprite was not updated with them. Regex over the two
files, so this needs no JavaScript runtime.

There are FOUR copies of this vocabulary, not two: the registry's glyph field
(``kb/kinds.py``, whose artwork ``visualize/styling._KIND_ICON_PATHS`` now projects), the
sprite, and the JS map. ``dashboard.html`` states the sprite-mirrors-_KIND_ICON_PATHS
invariant in a comment, and it had drifted the other way -- the sprite held 17 symbols
while the Python table held 15. ``test_all_four_glyph_vocabularies_agree`` is the one
assertion that holds every copy to every other; the narrower tests below are kept because
their failure messages name the specific consequence.
"""

import re
from pathlib import Path

from contextlake.kb.kinds import KIND_REGISTRY

_STATIC = Path(__file__).resolve().parents[2] / "src/contextlake/kb/dashboard/static"
_JS = _STATIC / "dashboard.js"
_HTML = _STATIC / "dashboard.html"


def _glyph_kinds() -> set[str]:
    m = re.search(r"var KIND_GLYPHS = \{(.*?)\}", _JS.read_text(), re.DOTALL)
    assert m, "KIND_GLYPHS not found; this test's parsing assumption is stale"
    return set(re.findall(r"(\w+)\s*:\s*1", m.group(1)))


def _sprite_ids() -> set[str]:
    return set(re.findall(r'id="g-(\w+)"', _HTML.read_text()))


def _sprite_artwork() -> dict[str, str]:
    return dict(re.findall(r'<symbol id="g-(\w+)" viewBox="0 0 24 24">(.*?)</symbol>',
                           _HTML.read_text()))


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


def test_all_four_glyph_vocabularies_agree():
    """Registry artwork == Python projection == sprite == JS map.

    Equality in every direction. A glyph in the registry but not the sprite is a blank box;
    a symbol in the sprite that no kind claims is dead weight the sprite comment promises
    is a mirror; and a kind in the JS map alone is the regression described above.
    """
    registry = {k for k, s in KIND_REGISTRY.items() if s.glyph}
    from contextlake.kb.visualize.styling import _KIND_ICON_PATHS

    assert registry == set(_KIND_ICON_PATHS), "styling no longer projects the registry"
    assert registry == _sprite_ids(), (
        "registry glyphs vs dashboard.html <symbol id='g-...'>: "
        f"registry-only={sorted(registry - _sprite_ids())} "
        f"sprite-only={sorted(_sprite_ids() - registry)}"
    )
    assert registry == _glyph_kinds(), (
        "registry glyphs vs dashboard.js KIND_GLYPHS: "
        f"registry-only={sorted(registry - _glyph_kinds())} "
        f"js-only={sorted(_glyph_kinds() - registry)}"
    )


def test_the_sprite_draws_the_same_artwork_the_registry_holds():
    """Same *ids* is not the invariant the sprite comment promises; same *shape* is.

    ``kb/kinds.py`` says "one artwork per kind, so a class reads identically in the graph
    page and in the dashboard", and the sprite is a hand-copy of those path constants. Only
    the id sets were compared, so retouching a glyph in the registry left the dashboard
    drawing the old shape with nothing failing -- the two surfaces would silently disagree
    about what a class looks like, which is the entire point of having glyphs.
    """
    sprite = _sprite_artwork()
    drift = sorted(k for k, s in KIND_REGISTRY.items()
                   if s.glyph and sprite.get(k) != s.glyph)
    assert not drift, (
        f"dashboard.html draws different artwork than kb/kinds.py for: {drift}. Copy the "
        "registry's glyph constant into that <symbol> so both surfaces draw one shape."
    )


def test_the_kinds_added_this_cycle_are_covered():
    """Named explicitly: these are the two that broke the invariant."""
    assert {"config_key", "test"} <= _glyph_kinds() & _sprite_ids()
