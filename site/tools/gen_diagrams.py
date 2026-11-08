#!/usr/bin/env python3
"""Generate clean, brand-styled SVG pipeline diagrams for the docs.
SVG renders on both GitHub and the docs site, no JS, offline-safe."""
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

IMG = pathlib.Path(__file__).resolve().parents[2] / "docs/img"  # <repo>/docs/img
DEEP = "#0E2A33"; LAKE = "#137A8B"; CUR = "#2BB3A3"; MIST = "#EAF4F4"
SHORE = "#D7C5A0"; SUN = "#E7B53C"
# Tier-2 light-theme roles (BRANDING.md 3.5): text = deepwater, card surface, muted text, divider.
INK = DEEP; SURFACE = "#FFFFFF"; MUTED = "#566B70"; LINE = "#D2E0E0"


def _mix(a, b, t):
    """``a`` blended toward ``b`` by ``t`` (0..1), as an upper-case hex colour."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


# The result fill: mist blended 12% toward current. A derived tier-2 tint (BRANDING.md 3.2),
# named here so no diagram carries a stray hex for it.
TINT = _mix(MIST, CUR, 0.12)
FF = "'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif"
FM = "'JetBrains Mono',ui-monospace,monospace"
# Type roles (BRANDING.md 4): Inter reads (notes, labels, headings under 20px), JetBrains Mono
# is for identifiers (commands, flags, paths, kind names), Space Grotesk is display only (20px
# and up). Nothing is set under 12px, the caption floor. site/fonts.css ships Inter 400-600
# and JetBrains Mono 400-500, so those are the weights used.
FI = "Inter,-apple-system,Segoe UI,Roboto,sans-serif"
FONTS_CSS = pathlib.Path(__file__).resolve().parents[1] / "fonts.css"  # <repo>/site/fonts.css
# One corner radius for every card and chip in every diagram. The 15px legend swatches in
# taxonomy() are exempt (rx=4), since a 12px radius would turn them into circles.
# BRANDING.md sets no diagram-card radius, so this is a diagram rule: change it here.
RADIUS = 12
# One connector style for every diagram: 1.75px (BRANDING.md's icon stroke), round caps and
# joins, one arrowhead. The head is sized in user units so it does not grow with the stroke.
LINK_W = 1.75
# One chip rhythm for every diagram family: primary chips are CHIP_H tall (two lines of text),
# list chips CHIP_S (one line), and the gap a connector crosses is GAP.
CHIP_H = 56; CHIP_S = 36; GAP = 30
# A result chip carries two cues besides its tint: a heavier outline and a small sun dot
# (decorative gold, never text), so the role does not rest on fill colour alone.
RESULT_W = 2.5


def result_dot(x, y, w):
    """The sun dot in the top-right corner of a result chip at (x, y) of width ``w``."""
    return f'<circle cx="{x + w - 14}" cy="{y + 14}" r="4" fill="{SUN}"/>'
WRITTEN = []  # every SVG name this process wrote, in order, for rasterize()


def _write(fname, parts, W, H):
    """Write one diagram, newline-terminated, and report it.

    The trailing newline is not cosmetic: these SVGs are committed build outputs, and
    `tests/kb/test_kind_generated_surface_parity.py` proves the committed copy still
    equals what this script would write. The four files on disk already ended with a
    newline (every text file in the tree does) while the writer omitted one, so a
    byte-comparison gate would have failed on all four for a reason no reader could
    act on. Terminating here makes "regenerate and commit" the only way to be in sync.
    """
    (IMG / fname).write_text("\n".join(p for p in parts if p is not None) + "\n",
                             encoding="utf-8")
    WRITTEN.append(fname)
    print("wrote", fname, f"({W}x{H})")


def marker(mid="ar", color=None):
    """The shared arrowhead, for a ``<defs>`` block."""
    return (f'<marker id="{mid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="12" '
            f'markerHeight="12" markerUnits="userSpaceOnUse" orient="auto">'
            f'<path d="M0 0L10 5L0 10z" fill="{color or LAKE}"/></marker>')


def link(x1, y1, x2, y2, mid="ar"):
    """A straight connector with the shared arrowhead."""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LAKE}" '
            f'stroke-width="{LINK_W}" stroke-linecap="round" marker-end="url(#{mid})"/>')


def link_path(path, mid="ar"):
    """A bent connector (an SVG path ``d``) in the same style."""
    return (f'<path d="{path}" fill="none" stroke="{LAKE}" stroke-width="{LINK_W}" '
            f'stroke-linecap="round" stroke-linejoin="round" marker-end="url(#{mid})"/>')


def _chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    if pathlib.Path("/usr/sbin/google-chrome").is_file():
        return "/usr/sbin/google-chrome"
    raise SystemExit("rasterize needs headless Chrome (google-chrome or chromium) on PATH")


def rasterize(fnames, scale=2):
    """Export each SVG in :data:`IMG` to a PNG beside it, at ``scale`` times its size.

    The docs embed PNGs because raw.githubusercontent.com serves an SVG as text/plain, so
    a bare ``<img src=...svg>`` does not render on GitHub. Chrome draws the SVG with the
    brand faces from ``site/fonts.css`` (Space Grotesk, Inter, JetBrains Mono), which
    cairosvg cannot do: it falls back to the system sans. The background stays
    transparent. The output must be exactly ``scale`` times the declared size; the
    function fails otherwise, because ``tests/kb/test_kind_generated_surface_parity.py``
    compares those sizes.
    """
    chrome = _chrome()
    for fname in fnames:
        svg = (IMG / fname).read_text(encoding="utf-8")
        m = re.search(r'width="(\d+)" height="(\d+)"', svg)
        if not m:
            raise SystemExit(f"{fname}: no declared width and height")
        w, h = int(m.group(1)), int(m.group(2))
        out = IMG / (pathlib.Path(fname).stem + ".png")
        with tempfile.TemporaryDirectory() as tmp:
            page = pathlib.Path(tmp) / "page.html"
            page.write_text(
                '<!doctype html><meta charset="utf-8">'
                f'<link rel="stylesheet" href="{FONTS_CSS.as_uri()}">'
                "<style>html,body{margin:0;background:transparent}svg{display:block}</style>"
                + svg, encoding="utf-8")
            subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 "--default-background-color=00000000", f"--force-device-scale-factor={scale}",
                 f"--window-size={w},{h}", "--virtual-time-budget=10000",
                 f"--screenshot={out}", page.as_uri()],
                check=True, capture_output=True, timeout=180)
        head = out.read_bytes()[:24]
        got = struct.unpack(">II", head[16:24])
        if got != (w * scale, h * scale):
            raise SystemExit(f"{out.name} is {got[0]}x{got[1]}, expected {w * scale}x{h * scale}")
        print("exported", out.name, f"({got[0]}x{got[1]})")


def finish(png):
    """End of a generator run: export what was written when ``--png`` was passed."""
    if png:
        rasterize(WRITTEN)


def pipeline(stages, fname, title, accent_last=True, sub=None, per_row=None):
    """Horizontal pipeline: rounded chips joined by arrows.

    ``per_row`` wraps a long pipeline onto further rows: the connector leaves the last chip
    of a row, runs left in the gap and drops into the first chip of the next row.
    """
    n = len(stages)
    pad, chip_h, gap = 24, CHIP_H, GAP
    chip_w = 132
    cols = per_row or n
    rows = (n + cols - 1) // cols
    row_gap = 56
    W = pad * 2 + cols * chip_w + (cols - 1) * gap
    top = 44 if sub else 22
    H = top + rows * chip_h + (rows - 1) * row_gap + 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" aria-label="{title}" font-family="{FI}">',
        f'<defs>{marker()}</defs>',
    ]
    if sub:
        parts.append(f'<text x="{pad}" y="26" font-size="13" font-weight="600" '
                     f'fill="{MUTED}" letter-spacing=".02em">{sub}</text>')

    def pos(i):
        return pad + (i % cols) * (chip_w + gap), top + (i // cols) * (chip_h + row_gap)

    for i, (label, note) in enumerate(stages):
        x, y = pos(i)
        cy = y + chip_h // 2
        last = i == n - 1
        result = accent_last and last
        fill = TINT if result else MIST
        stroke = CUR if result else LINE
        parts.append(
            f'<rect x="{x}" y="{y}" width="{chip_w}" height="{chip_h}" rx="{RADIUS}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{RESULT_W if result else 1.5}"/>')
        if result:
            parts.append(result_dot(x, y, chip_w))
        parts.append(
            f'<text x="{x + chip_w//2}" y="{cy - 3}" text-anchor="middle" font-size="15" '
            f'font-weight="500" fill="{INK}" font-family="{FM}">{label}</text>')
        if note:
            parts.append(
                f'<text x="{x + chip_w//2}" y="{cy + 16}" text-anchor="middle" font-size="12" '
                f'fill="{MUTED}">{note}</text>')
        if i < n - 1 and (i + 1) % cols:
            ax = x + chip_w + 3
            parts.append(link(ax, cy, ax + gap - 8, cy))
        elif i < n - 1:
            nx, ny = pos(i + 1)
            mid = y + chip_h + row_gap // 2
            parts.append(link_path(f'M{x + chip_w//2} {y + chip_h + 4} V{mid} '
                                   f'H{nx + chip_w//2} V{ny - 6}'))
    parts.append("</svg>")
    _write(fname, parts, W, H)


def _esc(s):
    # house style: no em-dashes in any copy (the package CONF_META notes carry them)
    s = s.replace(" — ", ", ").replace("—", ", ")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def architecture_overview(fname):
    """The README hero diagram: sources -> contextlake -> AI tools, three columns
    joined by arrows. Replaces a hand-made, non-transparent PNG with the same
    generated/brand-palette/transparent treatment every other diagram here uses
    (see docs/style-guide-formatting.md's "Generated, not hand-drawn" rule).

    README.md embeds docs/img/architecture.png, which is the 2x export of this SVG
    (``python site/tools/gen_diagrams.py --png``), because GitHub serves SVG as text/plain."""
    W, H = 900, 372
    colW, gap = 210, 60
    x1, x2, x3 = 40, 40 + colW + gap, 40 + 2 * (colW + gap)

    def col_head(x, label):
        return (f'<text x="{x + colW/2}" y="34" text-anchor="middle" font-size="12" '
               f'font-weight="600" fill="{MUTED}" '
               f'font-family="{FI}">{label.upper()}</text>')

    def chip(x, y, w, h, label, sub=None, fill=MIST, stroke=LINE, bold=False, mono=False):
        parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{RADIUS}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="{RESULT_W if stroke == CUR else 1.5}"/>']
        if stroke == CUR:
            parts.append(result_dot(x, y, w))
        ty = y + h/2 + (-6 if sub else 5)
        parts.append(f'<text x="{x + w/2}" y="{ty}" text-anchor="middle" font-size="14" '
                     f'font-weight="{500 if mono else 600}" fill="{INK}" '
                     f'font-family="{FM if mono else FI}">{_esc(label)}</text>')
        if sub:
            parts.append(f'<text x="{x + w/2}" y="{ty + 20}" text-anchor="middle" '
                         f'font-size="12" fill="{MUTED}" font-family="{FI}">{_esc(sub)}</text>')
        return "".join(parts)

    def arrow(x1_, y1_, x2_, y2_):
        return link(x1_, y1_, x2_, y2_)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" font-family="{FI}" '
        'aria-label="contextlake architecture. On the left, your repos: a GitLab group, '
        'plus optional Figma, Jira, and other MCP connectors. In the centre, contextlake '
        'indexes and mirrors them into a graph and embeddings, a wiki, and connectors. On '
        'the right, it serves the result over MCP to your AI tools: Claude Code, Windsurf, '
        'Kiro, Cursor, and Postman.">',
        f'<defs>{marker()}</defs>',
        col_head(x1, "Your workspace"), col_head(x2, "contextlake"), col_head(x3, "Your AI tools"),
    ]

    # left column: repos + connectors
    parts.append(chip(x1, 54, colW, CHIP_H, "Repos", "GitLab / GitHub / Bitbucket", bold=True))
    for i, label in enumerate(("Figma", "Jira", "other MCP")):
        parts.append(chip(x1, 128 + i * 46, colW, CHIP_S, label, fill=SURFACE, stroke=LINE))

    # centre column: the contextlake hub + its four generated layers
    hub_y = 54
    parts.append(chip(x2, hub_y, colW, CHIP_H, "contextlake", "index + mirror",
                       fill=TINT, stroke=CUR, bold=True, mono=True))
    for i, label in enumerate(("Graph", "Embeddings", "Wiki", "Connectors")):
        parts.append(chip(x2, 128 + i * 46, colW, CHIP_S, label, fill=SURFACE, stroke=LINE))

    # right column: AI tool consumers (served over MCP)
    parts.append(chip(x3, 54, colW, CHIP_H, "Served over MCP", fill=SURFACE, stroke=LINE))
    for i, label in enumerate(("Claude Code", "Windsurf", "Kiro", "Cursor", "Postman")):
        parts.append(chip(x3, 128 + i * 46, colW, CHIP_S, label, fill=MIST, stroke=LINE))

    # every column shares one rhythm: head at y=34, primary chip 54..110, then 36px chips every 46px from y=128
    # arrows between the hub row and each column's top chip
    midy = 54 + 28
    parts.append(arrow(x1 + colW, midy, x2 - 6, midy))
    parts.append(arrow(x2 + colW, midy, x3 - 6, midy))

    parts.append("</svg>")
    _write(fname, parts, W, H)


def taxonomy(fname):
    """The knowledge-graph vocabulary: node kinds + edge relations, colored
    EXACTLY as ``contextlake kb graph`` renders them (colors imported from the
    package so the doc diagram can never drift from the real output)."""
    from contextlake.kb.kinds import kind_groups
    from contextlake.kb.visualize import (
        KIND_COLORS, RELATION_COLORS, DEFAULT_EDGE_COLOR, CONF_META,
    )
    # node-kind groups (label -> kinds), projected from the kind registry so this diagram
    # documents the WHOLE vocabulary. The hand-written list this replaced had drifted to 16
    # of 35 kinds -- missing every SQL, HCL, ADR, connector and config kind -- while the
    # docstring above claimed it could never drift. Only the colours it imported were safe.
    groups = kind_groups()
    # edge relations to show: explicitly-hued first, then the two that ride the
    # neutral default (inherits/references have no dedicated hue in the real graph).
    rels = [
        ("calls", RELATION_COLORS["calls"]), ("imports", RELATION_COLORS["imports"]),
        ("contains", RELATION_COLORS["contains"]), ("depends_on", RELATION_COLORS["depends_on"]),
        ("publishes", RELATION_COLORS["publishes"]), ("flow", RELATION_COLORS["flow"]),
        ("exposes", RELATION_COLORS["exposes"]), ("calls_http", RELATION_COLORS["calls_http"]),
        ("tracked_by", RELATION_COLORS["tracked_by"]),
        ("documented_by", RELATION_COLORS["documented_by"]),
        ("inherits", DEFAULT_EDGE_COLOR), ("references", DEFAULT_EDGE_COLOR),
    ]
    conf = [  # style name -> (label, note); matches visualize._CONF_DOT
        ("none", CONF_META["EXTRACTED"][0], CONF_META["EXTRACTED"][2]),
        ("6 4", CONF_META["INFERRED"][0], CONF_META["INFERRED"][2]),
        ("2 3", CONF_META["AMBIGUOUS"][0], CONF_META["AMBIGUOUS"][2]),
    ]

    pad = 26
    colL, colR = pad, 470
    W, top = 900, 90
    parts = [None]  # placeholder for the <svg> header (H computed at the end)
    parts.append(f'<text x="{pad}" y="30" font-size="20" font-weight="700" '
                 f'fill="{INK}" font-family="{FF}">The knowledge graph vocabulary</text>')
    parts.append(f'<text x="{pad}" y="52" font-size="14" fill="{MUTED}" '
                 f'font-family="{FI}">node kinds and edge relations, colored exactly as '
                 f'<tspan font-family="{FM}" font-size="14">contextlake kb graph</tspan> renders them</text>')

    # left column: node kinds
    y = top
    parts.append(f'<text x="{colL}" y="{y}" font-size="14" font-weight="600" '
                 f'fill="{INK}" font-family="{FI}">Node kinds</text>')
    y += 22
    for glabel, kinds in groups:
        parts.append(f'<text x="{colL}" y="{y}" font-size="12" font-weight="600" '
                     f'fill="{MUTED}" font-family="{FI}">'
                     f'{glabel.upper()}</text>')
        y += 18
        for k in kinds:
            c = KIND_COLORS.get(k, MUTED)
            parts.append(f'<rect x="{colL}" y="{y-11}" width="15" height="15" rx="4" '
                         f'fill="{c}" stroke="{INK}" stroke-opacity=".18"/>')
            parts.append(f'<text x="{colL+24}" y="{y+1}" font-size="14" fill="{INK}" '
                         f'font-family="{FM}">{k}</text>')
            y += 22
        y += 14
    left_bottom = y

    # right column: edge relations + confidence
    y = top
    parts.append(f'<text x="{colR}" y="{y}" font-size="14" font-weight="600" '
                 f'fill="{INK}" font-family="{FI}">Edge relations</text>')
    y += 22
    for name, c in rels:
        parts.append(f'<line x1="{colR}" y1="{y-4}" x2="{colR+52}" y2="{y-4}" '
                     f'stroke="{c}" stroke-width="3" marker-end="url(#ar)"/>')
        parts.append(f'<text x="{colR+68}" y="{y}" font-size="14" fill="{INK}" '
                     f'font-family="{FM}">{name}</text>')
        y += 24
    y += 14
    parts.append(f'<text x="{colR}" y="{y}" font-size="14" font-weight="600" '
                 f'fill="{INK}" font-family="{FI}">Confidence (line style)</text>')
    y += 22
    for dash, label, note in conf:
        da = "" if dash == "none" else f' stroke-dasharray="{dash}"'
        parts.append(f'<line x1="{colR}" y1="{y-4}" x2="{colR+52}" y2="{y-4}" '
                     f'stroke="{INK}" stroke-width="2.5"{da}/>')
        parts.append(f'<text x="{colR+68}" y="{y}" font-size="13" fill="{INK}" '
                     f'font-family="{FI}">{label}</text>')
        parts.append(f'<text x="{colR+68}" y="{y+15}" font-size="12" fill="{MUTED}" '
                     f'font-family="{FI}">{_esc(note)}</text>')
        y += 38
    right_bottom = y

    parts.append("</svg>")
    H = max(left_bottom, right_bottom) + 22
    # one sunken-surface panel (mist, BRANDING.md 3.5) per column, drawn under the text
    panels = [f'<rect x="{px}" y="62" width="432" height="{bottom + 12 - 62}" rx="{RADIUS}" '
              f'fill="{MIST}"/>' for px, bottom in ((12, left_bottom), (456, right_bottom))]
    parts[1:1] = panels
    parts[0] = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" '
        f'aria-label="contextlake knowledge graph node kinds and edge relations">'
        f'<defs>{marker(color=MUTED)}'
        f'</defs><rect x="0" y="0" width="{W}" height="{H}" fill="none"/>')
    _write(fname, parts, W, H)


def palette(fname):
    """The locked six brand primitives as a labeled swatch strip (BRANDING section 3.3).
    Generated so it renders identically on the site and on GitHub, unlike inline CSS."""
    swatches = [
        ("deepwater", DEEP), ("lake", LAKE), ("current", CUR),
        ("mist", MIST), ("shore", SHORE), ("sun", SUN),
    ]
    pad, sw, gap, sh = 24, 150, 18, 96
    n = len(swatches)
    W = pad * 2 + n * sw + (n - 1) * gap
    H = pad * 2 + sh + 44
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-label="contextlake brand color primitives: '
        f'deepwater, lake, current, mist, shore, sun" font-family="{FI}">',
    ]
    for i, (name, hexv) in enumerate(swatches):
        x = pad + i * (sw + gap)
        # mist is near-white, so give it a hairline border to stay visible on light grounds
        stroke = f' stroke="{LINE}" stroke-width="1"' if name == "mist" else ""
        parts.append(f'<rect x="{x}" y="{pad}" width="{sw}" height="{sh}" rx="{RADIUS}" '
                     f'fill="{hexv}"{stroke}/>')
        parts.append(f'<text x="{x + 4}" y="{pad + sh + 22}" font-size="15" font-weight="600" '
                     f'fill="{INK}">{name}</text>')
        parts.append(f'<text x="{x + 4}" y="{pad + sh + 40}" font-size="13" fill="{MUTED}" '
                     f'font-family="{FM}">{hexv}</text>')
    parts.append("</svg>")
    _write(fname, parts, W, H)


def main(png=False):
    """Regenerate every committed diagram into :data:`IMG` (and PNGs when ``png``).

    ``python site/tools/gen_diagrams.py --png`` also exports each PNG the docs embed.
    ``gen_trees.py`` and ``gen_flows.py`` take the same flag.

    Behind a ``__main__`` guard rather than at module scope so a test can import this
    module, point ``IMG`` at a temp directory and recompute the diagrams to compare
    against the committed copies. Generating on import made that impossible: reading
    the expected bytes would have overwritten the very files under test.
    """
    # the README hero: sources -> contextlake -> AI tools (replaces a hand-made PNG)
    architecture_overview("architecture.svg")

    # the knowledge-graph vocabulary taxonomy (knowledge-layer.md / internals.md)
    taxonomy("graph-vocabulary.svg")

    # the brand color primitives (BRANDING.md)
    palette("brand-palette.svg")

    # the mirror sync pipeline (usage.md / README)
    pipeline(
        [("fetch", "list repos"), ("clone", "missing"), ("update", "pull"),
         ("branches", "most active"), ("verify", "structure"), ("audit", "health")],
        "pipeline-sync.svg", "contextlake mirror sync pipeline",
        sub="contextlake mirror sync")

    # the bootstrap pipeline (knowledge layer / quickstart). Mirrors cli.py `_bootstrap`:
    # mirror sync, then the audit, then `_bootstrap_stages` (index, connect, embed, enrich,
    # wiki, "Draw the architecture", "Write the API reference", steer).
    pipeline(
        [("sync", "mirror"), ("audit", "health"), ("index", "graph"), ("connect", "links"),
         ("embed", "vectors"), ("enrich", "context"), ("wiki", "prose"),
         ("diagrams", "architecture"), ("docs", "API reference"), ("steer", "editors")],
        "pipeline-bootstrap.svg", "contextlake bootstrap pipeline",
        sub=f'contextlake bootstrap, in order. Every stage but index and steer has a '
            f'<tspan font-family="{FM}" font-size="14">{_esc("--no-<stage>")}</tspan> flag.',
        per_row=5)
    finish(png)


if __name__ == "__main__":
    main(png="--png" in sys.argv[1:])
