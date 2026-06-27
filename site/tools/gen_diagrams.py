#!/usr/bin/env python3
"""Generate clean, brand-styled SVG pipeline diagrams for the docs.
SVG renders on both GitHub and the docs site, no JS, offline-safe."""
import pathlib

IMG = pathlib.Path("/home/sayak.sarkar/Work/contextlake/docs/img")
DEEP = "#0E2A33"; LAKE = "#137A8B"; CUR = "#2BB3A3"; MIST = "#EAF4F4"
INK = "#0E2A33"; MUTED = "#41606a"; LINE = "#cfe0e3"; SUN = "#E7B53C"
FF = "'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif"
FM = "'JetBrains Mono',ui-monospace,monospace"


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
    (IMG / fname).write_text("\n".join(parts), encoding="utf-8")
    print("wrote", fname, f"({W}x{H})")


# the mirror sync pipeline (usage.md / README)
pipeline(
    [("fetch", "list repos"), ("clone", "missing"), ("update", "pull"),
     ("branches", "most active"), ("verify", "structure"), ("audit", "health")],
    "pipeline-sync.svg", "contextlake sync pipeline",
    sub="contextlake sync")

# the bootstrap pipeline (knowledge layer / quickstart)
pipeline(
    [("sync", "mirror"), ("index", "graph"), ("connect", "links"),
     ("embed", "vectors"), ("wiki", "prose"), ("steer", "editors")],
    "pipeline-bootstrap.svg", "contextlake bootstrap pipeline",
    sub="contextlake bootstrap")
