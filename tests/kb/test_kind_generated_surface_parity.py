"""The kind vocabulary's *generated* surfaces must still equal the registry.

``tests/kb/test_kind_registry_parity.py`` holds ``kb/kinds.py`` to every vocabulary that
lives in Python, and ``test_dashboard_kind_glyph_parity.py`` holds it to the dashboard's
glyph copies. Neither can see a surface that is **generated once and committed**, and
those are the ones that go stale silently: a projection recomputes on import, a build
output does not. A renderer fix does not reach a file already on disk.

That is measured, not hypothetical. When this file was written the committed
``docs/img/graph-vocabulary.svg`` documented **16 of 40 kinds** and five of the nine
bands, because ``kind_groups()`` was introduced to end exactly that drift and nobody
re-ran the generator afterwards. The diagram a reader would trust for "what kinds
exist?" was missing every SQL, Terraform, document, connector and C/C++ symbol kind, and
the whole suite was green. ``site/graph-embed.html``'s ``COLORS`` map was stale the same
way, at 17 of 40.

Each check recomputes rather than restating an expectation, the way
``tests/test_llms_full_is_in_sync.py`` does: it imports the real generator, points it at
a temp directory and compares bytes. A hand-copied list of 40 kinds here would be a
seventeenth vocabulary to drift.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import struct
import sys

import pytest

from contextlake.kb.kinds import KIND_REGISTRY, kind_groups
from contextlake.kb.visualize.styling import KIND_COLORS

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS_IMG = REPO / "docs/img"
SITE_IMG = REPO / "site/img"
TOOLS = REPO / "site/tools"
VOCAB_SVG = "graph-vocabulary.svg"
VOCAB_PNG = "graph-vocabulary.png"
# the generator imports contextlake, so it needs the package importable: an installed
# checkout works as-is, a bare worktree needs PYTHONPATH=src
REGEN = "PYTHONPATH=src .venv/bin/python site/tools/gen_diagrams.py"

# One kind row in the taxonomy diagram: a 15x15 colour swatch immediately followed by the
# kind name in the monospace face. Nothing else in that diagram is 15x15, so this cannot
# pick up the legend or the headings.
_SWATCH = re.compile(
    r'<rect x="\d+" y="\d+" width="15" height="15" rx="4" fill="(#[0-9a-fA-F]{6})"'
    r'[^>]*/>\n<text [^>]*font-family="\'JetBrains Mono\',ui-monospace,monospace">'
    r"(\w+)</text>"
)
_BAND = re.compile(r'<text x="\d+" y="\d+" font-size="11" font-weight="600"[^>]*>([A-Z -]+)</text>')


def _gen_diagrams():
    """Import ``site/tools/gen_diagrams.py`` without generating anything.

    Its generators used to run at module scope, so importing it overwrote the files a
    test would want to read. They are behind a ``main()`` guard now; if that regresses,
    this import silently rewrites ``docs/img`` and the comparison below becomes vacuous,
    which is what ``test_importing_the_generator_writes_nothing`` pins.
    """
    if not (TOOLS / "gen_diagrams.py").is_file():
        pytest.skip("site/tools/gen_diagrams.py is not present in this checkout")
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return pytest.importorskip("gen_diagrams", reason="site/tools/gen_diagrams.py not importable")


def _recompute_vocab_svg(tmp_path, monkeypatch) -> str:
    """Generate the diagram into `tmp_path`, never over the committed copy.

    ``IMG`` is redirected through ``monkeypatch`` so the module global is restored even
    on failure; the module is import-cached, so a leaked ``IMG`` would point later tests
    at a deleted directory.
    """
    gd = _gen_diagrams()
    monkeypatch.setattr(gd, "IMG", tmp_path)
    gd.taxonomy(VOCAB_SVG)
    return (tmp_path / VOCAB_SVG).read_text(encoding="utf-8")


def _swatches(text: str) -> dict[str, str]:
    return {kind: color for color, kind in _SWATCH.findall(text)}


def _png_size(path: pathlib.Path) -> tuple[int, int]:
    """Width/height straight out of the PNG IHDR chunk, so this needs no image library."""
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR", (
        f"{path.name} is not a PNG with a leading IHDR chunk"
    )
    return struct.unpack(">II", head[16:24])


def test_the_generator_does_not_generate_at_import():
    """The guard every other check in this file rests on.

    A fresh import computes ``IMG`` as the real ``docs/img`` from ``__file__``, so if
    generation moved back to module scope the import below would rewrite the committed
    diagrams and the comparison would become a file against itself: green no matter how far
    the vocabulary had drifted.

    Checked on the source, not by importing and diffing ``docs/img``: while the files are in
    sync, a module-scope run rewrites them with identical bytes and a before/after diff sees
    nothing. The property is "no generation happens at import", and that is a property of the
    source, so ``ast`` is what can actually observe it.
    """
    if not (TOOLS / "gen_diagrams.py").is_file():
        pytest.skip("site/tools/gen_diagrams.py is not present in this checkout")
    tree = ast.parse((TOOLS / "gen_diagrams.py").read_text(encoding="utf-8"))
    calls = [
        f"line {node.lineno}: {getattr(node.value.func, 'id', '?')}(...)"
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert not calls, (
        "site/tools/gen_diagrams.py calls a generator at module scope: "
        + "; ".join(calls)
        + ". Importing it therefore rewrites docs/img, which makes every diagram "
        "comparison in this file vacuous. Move the calls into main(), behind the "
        '`if __name__ == "__main__"` guard.'
    )
    assert any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body), (
        "gen_diagrams.main() is gone; see the docstring above."
    )


def test_the_committed_vocabulary_diagram_is_what_the_generator_would_write(
        tmp_path, monkeypatch):
    """`docs/img/graph-vocabulary.svg` is the published answer to "what kinds exist?"."""
    expected = _recompute_vocab_svg(tmp_path, monkeypatch)
    fresh = _swatches(expected)
    assert set(fresh) == set(KIND_REGISTRY), (
        "this test's SVG parsing assumption is stale: a freshly generated diagram does "
        f"not read back as the registry (parsed {len(fresh)} of {len(KIND_REGISTRY)} kinds)"
    )

    committed = (DOCS_IMG / VOCAB_SVG).read_text(encoding="utf-8")
    if committed == expected:
        return

    on_disk = _swatches(committed)
    missing = sorted(set(fresh) - set(on_disk))
    stale = sorted(set(on_disk) - set(fresh))
    recolored = sorted(k for k, c in on_disk.items() if k in fresh and fresh[k] != c)
    # the generator upper-cases band headings, so compare upper-cased or every band
    # reads as missing and the message misdirects
    drawn_bands = {b.strip().upper() for b in _BAND.findall(committed)}
    bands = sorted(g for g, kinds in kind_groups() if kinds and g.upper() not in drawn_bands)
    pytest.fail(
        f"docs/img/{VOCAB_SVG} is out of date with the kind registry.\n"
        f"  kinds in the registry but not on the diagram: {missing or 'none'}\n"
        f"  kinds on the diagram the registry dropped: {stale or 'none'}\n"
        f"  kinds drawn in the wrong colour: {recolored or 'none'}\n"
        f"  bands missing their heading: {bands or 'none'}\n"
        f"Regenerate it (and re-raster the PNG) with: {REGEN}"
    )


def test_the_rastered_vocabulary_diagram_matches_the_svg(tmp_path):
    """The docs embed the PNG, not the SVG, so an un-rastered fix reaches no reader.

    Every committed diagram PNG here is exactly 2x its SVG (docs/style-guide-formatting.md
    section "Diagrams and visuals": rasterized with cairosvg). Adding a kind makes the SVG
    taller, so comparing declared sizes catches a forgotten raster without needing to
    re-run cairosvg -- which is not a test dependency.
    """
    svg = (DOCS_IMG / VOCAB_SVG).read_text(encoding="utf-8")
    m = re.search(r'width="(\d+)" height="(\d+)"', svg)
    assert m, f"no declared width/height in {VOCAB_SVG}; parsing assumption is stale"
    sw, sh = int(m.group(1)), int(m.group(2))
    pw, ph = _png_size(DOCS_IMG / VOCAB_PNG)
    assert (pw, ph) == (sw * 2, sh * 2), (
        f"docs/img/{VOCAB_PNG} is {pw}x{ph} but docs/img/{VOCAB_SVG} is {sw}x{sh}, whose "
        f"2x raster is {sw * 2}x{sh * 2}. The docs embed the PNG, so the SVG was "
        "regenerated and the raster was not. Re-raster with:\n"
        f"  .venv/bin/python -c \"import cairosvg; cairosvg.svg2png("
        f"url='docs/img/{VOCAB_SVG}', write_to='docs/img/{VOCAB_PNG}', scale=2)\"\n"
        "then copy both files to site/img/."
    )


@pytest.mark.parametrize("name", [VOCAB_SVG, VOCAB_PNG])
def test_the_site_copy_of_the_vocabulary_diagram_is_not_a_second_original(name):
    """These two files exist twice, and `site/img` is the copy a visitor is served.

    `site/build_docs.py` rebuilds `site/img` by copying `docs/img`, and `site/deploy.sh`
    publishes `site/img`, so `docs/img` is the source and this is the surface a reader sees.
    Regenerating one and not the other publishes the stale picture. Scoped to these two
    files deliberately: `site/img` is currently a *subset* of `docs/img` (it predates
    several assets that were only added under `docs/img`), so asserting whole-directory
    equality would fail for reasons that have nothing to do with the kind vocabulary.
    """
    docs, site = DOCS_IMG / name, SITE_IMG / name
    if not site.is_file():
        pytest.skip(f"site/img/{name} is not present in this checkout")
    assert docs.read_bytes() == site.read_bytes(), (
        f"site/img/{name} differs from docs/img/{name}. docs/img is the source; refresh "
        f"the published copy with: cp docs/img/{name} site/img/{name}"
    )


def test_the_committed_graph_page_carries_every_kind_colour():
    """`site/graph-embed.html` is a real graph page, generated once and committed.

    `visualize/html_render` emits the WHOLE colour map (not just the kinds in view) because
    the page's filter is built from it, so this comparison is independent of which nodes the
    demo happens to show and cannot flake on a different export. Its generator's own
    docstring makes the case: a static artifact does not receive a later fix, and the 6.2.0
    stored-XSS patch left this exact file stale while the advisory told users to regenerate.
    """
    target = REPO / "site/graph-embed.html"
    if not target.is_file():
        pytest.skip("site/graph-embed.html is not present in this checkout")
    m = re.search(r"^  var COLORS = (\{.*\});$", target.read_text(encoding="utf-8"),
                  re.MULTILINE)
    assert m, "COLORS not found in site/graph-embed.html; parsing assumption is stale"
    colors = json.loads(m.group(1))
    missing = sorted(set(KIND_COLORS) - set(colors))
    unknown = sorted(set(colors) - set(KIND_COLORS))
    recolored = sorted(k for k, c in colors.items() if k in KIND_COLORS and KIND_COLORS[k] != c)
    assert not (missing or unknown or recolored), (
        "site/graph-embed.html was exported by an older renderer and its kind filter can "
        "no longer offer every kind a button.\n"
        f"  kinds with no colour on the page: {missing or 'none'}\n"
        f"  colours for kinds the registry dropped: {unknown or 'none'}\n"
        f"  kinds coloured differently from the registry: {recolored or 'none'}\n"
        "Regenerate with: PYTHONPATH=src .venv/bin/python site/tools/gen_graph_embed.py  "
        "(from a checkout whose directory is named contextlake; the exporter selects --repo "
        "by directory name, so it finds nothing in a worktree)"
    )
