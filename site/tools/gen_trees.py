#!/usr/bin/env python3
"""Decision-cascade SVG diagrams: a start chip, decisions along the bottom
joined by 'no' arrows, each 'yes' branching up to an outcome chip."""
import sys

from gen_diagrams import (  # one writer, one palette, one connector: see gen_diagrams
    CHIP_H, CUR, RESULT_W, result_dot, DEEP, FI, FM, GAP, INK, LAKE, LINE, MIST, MUTED, RADIUS, SURFACE, TINT,
    _write, finish, link, marker)


def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def cascade(start, steps, tail, fname, title, sub):
    """start, each decision, each outcome and tail are nodes: (title, note, note_mono).
    steps: [(question_node, yes_node)]."""
    colw, gap = 196, GAP
    n = len(steps)
    pad = 22
    W = pad*2 + 150 + gap + n*(colw+gap) + 170
    H = 250
    yd = 168   # decision row baseline (centre y)
    yo = 64    # outcome row (centre y)
    ch = CHIP_H
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
         f'height="{H}" role="img" aria-label="{esc(title)}" font-family="{FI}">',
         f'<defs>{marker()}</defs>']
    if sub:
        p.append(f'<text x="{pad}" y="26" font-size="13" font-weight="600" fill="{MUTED}">{esc(sub)}</text>')

    def chip(x, y, w, node, fill, stroke, tcol, mono=False, h=ch, fs=14, result=False):
        """node: (title, note, note_mono). A note is a second, smaller line."""
        title, note, note_mono = node
        ff = FM if mono else FI
        p.append(f'<rect x="{x}" y="{y-h//2}" width="{w}" height="{h}" rx="{RADIUS}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{RESULT_W if result else 1.5}"/>')
        if result:
            p.append(result_dot(x, y - h//2, w))
        if not note:
            p.append(f'<text x="{x+w//2}" y="{y+5}" text-anchor="middle" font-size="{fs}" '
                     f'font-weight="{500 if mono else 600}" fill="{tcol}" font-family="{ff}">{esc(title)}</text>')
        else:
            nf = FM if note_mono else FI
            p.append(f'<text x="{x+w//2}" y="{y-3}" text-anchor="middle" font-size="{fs}" '
                     f'font-weight="{500 if mono else 600}" fill="{tcol}" font-family="{ff}">{esc(title)}</text>')
            p.append(f'<text x="{x+w//2}" y="{y+15}" text-anchor="middle" font-size="12" '
                     f'fill="{MUTED}" font-family="{nf}">{esc(note)}</text>')

    # start chip (deepwater)
    sx = pad
    chip(sx, yd, 150, start, DEEP, DEEP, MIST, mono=True)
    prev_r = sx + 150
    for i, (q, yes) in enumerate(steps):
        x = pad + 150 + gap + i*(colw+gap)
        # 'no' arrow into this decision
        p.append(link(prev_r+3, yd, x-6, yd))
        if i > 0:
            p.append(f'<text x="{(prev_r+x)//2}" y="{yd-8}" text-anchor="middle" font-size="12" '
                     f'fill="{MUTED}">no</text>')
        # decision chip
        chip(x, yd, colw, q, MIST, LAKE, INK, h=ch, fs=14)
        # 'yes' arrow up to outcome
        cx = x + colw//2
        p.append(link(cx, yd-32, cx, yo+ch//2+3))
        p.append(f'<text x="{cx+12}" y="{(yd+yo)//2}" font-size="12" fill="{LAKE}" '
                 f'font-weight="600">yes</text>')
        chip(x, yo, colw, yes, TINT, CUR, INK, mono=True, fs=14, result=True)
        prev_r = x + colw
    # tail outcome (final 'no')
    tx = prev_r + gap
    p.append(link(prev_r+3, yd, tx-6, yd))
    p.append(f'<text x="{(prev_r+tx)//2}" y="{yd-8}" text-anchor="middle" font-size="12" fill="{MUTED}">no</text>')
    chip(tx, yd, 170, tail, SURFACE, LINE, MUTED, mono=True)
    p.append("</svg>")
    _write(fname, p, W, H)


def main(png=False):
    """Regenerate the two decision cascades into gen_diagrams.IMG."""
    # Mirrors src/contextlake/kb/embeddings/base.py `_resolve_auto_embedder` and
    # kb/llm/base.py `_resolve_auto_llm`: Ollama needs the daemon AND the target model.
    cascade(("provider = auto", "", False),
            [(("Ollama reachable?", "AND target model pulled", False), ("use Ollama", "local model", False)),
             (("built-in extra installed?", "", False), ("use built-in", "CPU model", False))],
            ("skip", "this tier", False),
            "provider-resolution.svg", "Provider auto-resolution", "provider = auto resolves to")

    # Mirrors core.py: update_repository stashes only when auto_stash is set; the branch
    # switch path skips a dirty tree outright, then skips when `protect and not
    # is_safe_branch(current)`. The skip fires on a NON-safe branch.
    cascade(("each repo", "", False),
            [(("dirty working tree?", "require_clean_workspace", True),
              ("skip", "update stashes if auto_stash", False)),
             (("on a non-safe branch?", "protect_working_branches", True),
              ("leave", "no switch, pull still runs", False))],
            ("act", "pull or switch branch", False),
            "branch-safety.svg", "Branch-safety decision", "what update and branches do to each repo")
    finish(png)


if __name__ == "__main__":
    main(png="--png" in sys.argv[1:])
