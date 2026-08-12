#!/usr/bin/env python3
"""Generate clean, brand-styled SVG pipeline diagrams for the docs.
SVG renders on both GitHub and the docs site, no JS, offline-safe."""
import pathlib

IMG = pathlib.Path(__file__).resolve().parents[2] / "docs/img"  # <repo>/docs/img
DEEP = "#0E2A33"; LAKE = "#137A8B"; CUR = "#2BB3A3"; MIST = "#EAF4F4"
INK = "#0E2A33"; MUTED = "#41606a"; LINE = "#cfe0e3"; SUN = "#E7B53C"
FF = "'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif"
FM = "'JetBrains Mono',ui-monospace,monospace"


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
    print("wrote", fname, f"({W}x{H})")


def pipeline(stages, fname, title, accent_last=True, sub=None):
    """Horizontal pipeline: rounded chips joined by arrows."""
    n = len(stages)
    pad, chip_h, gap = 24, 58, 26
    chip_w = 132
    W = pad * 2 + n * chip_w + (n - 1) * gap
    H = 132 if sub else 108
    cy = 70 if sub else 58
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" aria-label="{title}" font-family="{FF}">',
        '<defs><marker id="ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{LAKE}"/></marker></defs>',
    ]
    if sub:
        parts.append(f'<text x="{pad}" y="26" font-size="13" font-weight="600" '
                     f'fill="{MUTED}" letter-spacing=".02em">{sub}</text>')
    for i, (label, note) in enumerate(stages):
        x = pad + i * (chip_w + gap)
        last = i == n - 1
        fill = MIST if not (accent_last and last) else "#e4f5f1"
        stroke = CUR if (accent_last and last) else LINE
        parts.append(
            f'<rect x="{x}" y="{cy - chip_h//2}" width="{chip_w}" height="{chip_h}" rx="12" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        parts.append(
            f'<text x="{x + chip_w//2}" y="{cy - 3}" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{INK}" font-family="{FM}">{label}</text>')
        if note:
            parts.append(
                f'<text x="{x + chip_w//2}" y="{cy + 16}" text-anchor="middle" font-size="10.5" '
                f'fill="{MUTED}">{note}</text>')
        if i < n - 1:
            ax = x + chip_w + 3
            parts.append(f'<line x1="{ax}" y1="{cy}" x2="{ax + gap - 8}" y2="{cy}" '
                         f'stroke="{LAKE}" stroke-width="2" marker-end="url(#ar)"/>')
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
    (see docs/style-guide-formatting.md's "Generated, not hand-drawn" rule)."""
    W, H = 900, 340
    colW, gap = 210, 60
    x1, x2, x3 = 40, 40 + colW + gap, 40 + 2 * (colW + gap)

    def col_head(x, label):
        return (f'<text x="{x + colW/2}" y="34" text-anchor="middle" font-size="13" '
               f'font-weight="700" fill="{MUTED}" letter-spacing=".04em" '
               f'font-family="{FF}">{label.upper()}</text>')

    def chip(x, y, w, h, label, sub=None, fill=MIST, stroke=LINE, bold=False):
        parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>']
        ty = y + h/2 + (-6 if sub else 5)
        parts.append(f'<text x="{x + w/2}" y="{ty}" text-anchor="middle" font-size="14" '
                     f'font-weight="{700 if bold else 600}" fill="{INK}" '
                     f'font-family="{FM}">{_esc(label)}</text>')
        if sub:
            parts.append(f'<text x="{x + w/2}" y="{ty + 20}" text-anchor="middle" '
                         f'font-size="10.5" fill="{MUTED}" font-family="{FF}">{_esc(sub)}</text>')
        return "".join(parts)

    def arrow(x1_, y1_, x2_, y2_):
        return (f'<line x1="{x1_}" y1="{y1_}" x2="{x2_}" y2="{y2_}" stroke="{LAKE}" '
               f'stroke-width="2" marker-end="url(#ar3)"/>')

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" font-family="{FF}" '
        'aria-label="contextlake architecture. On the left, your repos: a GitLab group, '
        'plus optional Figma, Jira, and other MCP connectors. In the centre, contextlake '
        'indexes and mirrors them into a graph and embeddings, a wiki, and connectors. On '
        'the right, it serves the result over MCP to your AI tools: Claude Code, Windsurf, '
        'Kiro, Cursor, and Postman.">',
        '<defs><marker id="ar3" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" '
        f'markerHeight="8" orient="auto"><path d="M0 0L10 5L0 10z" fill="{LAKE}"/></marker></defs>',
        col_head(x1, "Your workspace"), col_head(x2, "contextlake"), col_head(x3, "Your AI tools"),
    ]

    # left column: repos + connectors
    parts.append(chip(x1, 54, colW, 56, "Repos", "GitLab / GitHub / Bitbucket", bold=True))
    for i, label in enumerate(("Figma", "Jira", "other MCP")):
        parts.append(chip(x1, 128 + i * 46, colW, 36, label, fill="#fff", stroke=LINE))

    # centre column: the contextlake hub + its four generated layers
    hub_y = 54
    parts.append(chip(x2, hub_y, colW, 56, "contextlake", "index + mirror",
                       fill="#e4f5f1", stroke=CUR, bold=True))
    for i, label in enumerate(("Graph", "Embeddings", "Wiki", "Connectors")):
        parts.append(chip(x2, 128 + i * 46, colW, 36, label, fill="#fff", stroke=LINE))

    # right column: AI tool consumers (served over MCP)
    parts.append(chip(x3, 54, colW, 40, "Served over MCP", fill="#fff", stroke=LINE))
    for i, label in enumerate(("Claude Code", "Windsurf", "Kiro", "Cursor", "Postman")):
        parts.append(chip(x3, 110 + i * 40, colW, 32, label, fill=MIST, stroke=LINE))

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
    W, top = 900, 74
    parts = [None]  # placeholder for the <svg> header (H computed at the end)
    parts.append(f'<text x="{pad}" y="30" font-size="20" font-weight="700" '
                 f'fill="{INK}" font-family="{FF}">The knowledge graph vocabulary</text>')
    parts.append(f'<text x="{pad}" y="52" font-size="12.5" fill="{MUTED}" '
                 f'font-family="{FF}">node kinds and edge relations, colored exactly as '
                 f'<tspan font-family="{FM}">contextlake kb graph</tspan> renders them</text>')

    # left column: node kinds
    y = top
    parts.append(f'<text x="{colL}" y="{y}" font-size="13" font-weight="700" '
                 f'fill="{INK}" font-family="{FF}">Node kinds</text>')
    y += 22
    for glabel, kinds in groups:
        parts.append(f'<text x="{colL}" y="{y}" font-size="11" font-weight="600" '
                     f'fill="{MUTED}" letter-spacing=".04em" font-family="{FF}">'
                     f'{glabel.upper()}</text>')
        y += 18
        for k in kinds:
            c = KIND_COLORS.get(k, "#c9c9c9")
            parts.append(f'<rect x="{colL}" y="{y-11}" width="15" height="15" rx="4" '
                         f'fill="{c}" stroke="{INK}" stroke-opacity=".18"/>')
            parts.append(f'<text x="{colL+24}" y="{y+1}" font-size="13" fill="{INK}" '
                         f'font-family="{FM}">{k}</text>')
            y += 22
        y += 8
    left_bottom = y

    # right column: edge relations + confidence
    y = top
    parts.append(f'<text x="{colR}" y="{y}" font-size="13" font-weight="700" '
                 f'fill="{INK}" font-family="{FF}">Edge relations</text>')
    y += 22
    for name, c in rels:
        parts.append(f'<line x1="{colR}" y1="{y-4}" x2="{colR+52}" y2="{y-4}" '
                     f'stroke="{c}" stroke-width="3" marker-end="url(#ar2)"/>')
        parts.append(f'<text x="{colR+68}" y="{y}" font-size="13" fill="{INK}" '
                     f'font-family="{FM}">{name}</text>')
        y += 24
    y += 14
    parts.append(f'<text x="{colR}" y="{y}" font-size="13" font-weight="700" '
                 f'fill="{INK}" font-family="{FF}">Confidence (line style)</text>')
    y += 22
    for dash, label, note in conf:
        da = "" if dash == "none" else f' stroke-dasharray="{dash}"'
        parts.append(f'<line x1="{colR}" y1="{y-4}" x2="{colR+52}" y2="{y-4}" '
                     f'stroke="{INK}" stroke-width="2.5"{da}/>')
        parts.append(f'<text x="{colR+68}" y="{y}" font-size="13" fill="{INK}" '
                     f'font-family="{FF}">{label}</text>')
        parts.append(f'<text x="{colR+68}" y="{y+15}" font-size="10.5" fill="{MUTED}" '
                     f'font-family="{FF}">{_esc(note)}</text>')
        y += 38
    right_bottom = y

    parts.append("</svg>")
    H = max(left_bottom, right_bottom) + 10
    parts[0] = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" '
        f'aria-label="contextlake knowledge graph node kinds and edge relations">'
        f'<defs><marker id="ar2" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="{MUTED}"/></marker>'
        f'</defs><rect x="0" y="0" width="{W}" height="{H}" fill="none"/>')
    _write(fname, parts, W, H)


def palette(fname):
    """The locked six brand primitives as a labeled swatch strip (BRANDING section 3.3).
    Generated so it renders identically on the site and on GitHub, unlike inline CSS."""
    swatches = [
        ("deepwater", "#0E2A33"), ("lake", "#137A8B"), ("current", "#2BB3A3"),
        ("mist", "#EAF4F4"), ("shore", "#D7C5A0"), ("sun", "#E7B53C"),
    ]
    pad, sw, gap, sh = 24, 150, 18, 96
    n = len(swatches)
    W = pad * 2 + n * sw + (n - 1) * gap
    H = pad * 2 + sh + 44
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-label="contextlake brand color primitives: '
        f'deepwater, lake, current, mist, shore, sun" font-family="{FF}">',
    ]
    for i, (name, hexv) in enumerate(swatches):
        x = pad + i * (sw + gap)
        # mist is near-white, so give it a hairline border to stay visible on light grounds
        stroke = f' stroke="{LINE}" stroke-width="1"' if name == "mist" else ""
        parts.append(f'<rect x="{x}" y="{pad}" width="{sw}" height="{sh}" rx="10" '
                     f'fill="{hexv}"{stroke}/>')
        parts.append(f'<text x="{x + 4}" y="{pad + sh + 22}" font-size="15" font-weight="600" '
                     f'fill="{INK}">{name}</text>')
        parts.append(f'<text x="{x + 4}" y="{pad + sh + 40}" font-size="13" fill="{MUTED}" '
                     f'font-family="{FM}">{hexv}</text>')
    parts.append("</svg>")
    _write(fname, parts, W, H)


def main():
    """Regenerate every committed diagram into :data:`IMG`.

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

    # the bootstrap pipeline (knowledge layer / quickstart)
    pipeline(
        [("sync", "mirror"), ("index", "graph"), ("connect", "links"),
         ("embed", "vectors"), ("enrich", "context"), ("wiki", "prose"),
         ("steer", "editors")],
        "pipeline-bootstrap.svg", "contextlake bootstrap pipeline",
        sub="contextlake bootstrap")


if __name__ == "__main__":
    main()
