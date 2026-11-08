#!/usr/bin/env python3
"""The config-precedence cascade diagram (horizontal). Brand-styled SVG.

The sync data-flow chart that lived here was removed: no page embedded it."""
import sys

from gen_diagrams import (  # one writer, one palette, one connector: see gen_diagrams
    CHIP_H, CUR, RESULT_W, result_dot, DEEP, FI, FM, GAP, INK, LAKE, LINE, MIST, MUTED, RADIUS, TINT,
    _write, finish, link, marker)


def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def precedence(items, fname, title, sub):
    """Horizontal cascade; last item accented as the result."""
    cw, gap, h = 176, GAP, CHIP_H
    n=len(items); pad=22
    W=pad*2+n*cw+(n-1)*gap; H=120
    cy=72
    p=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'role="img" aria-label="{esc(title)}" font-family="{FI}">',
       f'<defs>{marker()}</defs>',
       f'<text x="{pad}" y="26" font-size="13" font-weight="600" fill="{MUTED}">{esc(sub)}</text>']
    for i,(label,note) in enumerate(items):
        x=pad+i*(cw+gap); last=i==n-1
        fill=TINT if last else MIST; stroke=CUR if last else LINE
        p.append(f'<rect x="{x}" y="{cy-h//2}" width="{cw}" height="{h}" rx="{RADIUS}" fill="{fill}" stroke="{stroke}" stroke-width="{RESULT_W if last else 1.5}"/>')
        if last:
            p.append(result_dot(x, cy-h//2, cw))
        p.append(f'<text x="{x+cw//2}" y="{cy-3}" text-anchor="middle" font-size="14" font-weight="500" fill="{INK}" font-family="{FM}">{esc(label)}</text>')
        if note:
            p.append(f'<text x="{x+cw//2}" y="{cy+15}" text-anchor="middle" font-size="12" fill="{MUTED}">{esc(note)}</text>')
        if i<n-1:
            ax=x+cw+3
            p.append(link(ax, cy, ax+gap-8, cy))
    p.append("</svg>")
    _write(fname, p, W, H)


def main(png=False):
    """Regenerate the flow diagrams into gen_diagrams.IMG."""
    precedence(
        # config.py: CONFIG_FILE, then find_ancestor_config(LOCAL_CONFIG_FILE), then --config
        [("defaults","built-in"),("~/.contextlake.ini","global"),(".contextlake.ini","nearest ancestor"),("--config","custom file"),("CLI flags","effective")],
        "config-precedence.svg","Configuration precedence","each layer overrides the one before it")

    finish(png)


if __name__ == "__main__":
    main(png="--png" in sys.argv[1:])
