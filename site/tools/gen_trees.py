#!/usr/bin/env python3
"""Decision-cascade SVG diagrams: a start chip, decisions along the bottom
joined by 'no' arrows, each 'yes' branching up to an outcome chip."""
import pathlib
IMG = pathlib.Path(__file__).resolve().parents[2] / "docs/img"  # <repo>/docs/img
DEEP="#0E2A33"; LAKE="#137A8B"; CUR="#2BB3A3"; MIST="#EAF4F4"; INK="#0E2A33"
MUTED="#41606a"; LINE="#cfe0e3"; SUN="#E7B53C"; GREEN="#2f8f7f"
FF="'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif"
FM="'JetBrains Mono',ui-monospace,monospace"


def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def cascade(start, steps, tail, fname, title, sub):
    """start: (label). steps: [(question, yes_label)]. tail: final outcome."""
    colw, gap = 188, 30
    n = len(steps)
    pad = 22
    W = pad*2 + 150 + gap + n*(colw+gap) + 150
    H = 250
    yd = 168   # decision row baseline (centre y)
    yo = 64    # outcome row (centre y)
    ch = 56
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
         f'height="{H}" role="img" aria-label="{esc(title)}" font-family="{FF}">',
         '<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
         f'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{LAKE}"/></marker>'
         '<marker id="ag" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
         f'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{GREEN}"/></marker></defs>']
    if sub:
        p.append(f'<text x="{pad}" y="26" font-size="13" font-weight="600" fill="{MUTED}">{esc(sub)}</text>')

    def chip(x, y, w, label, fill, stroke, tcol, mono=False, h=ch, fs=14):
        ff = FM if mono else FF
        p.append(f'<rect x="{x}" y="{y-h//2}" width="{w}" height="{h}" rx="12" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1.5"/>')
        # wrap up to 2 lines on " / " or " "
        words = label.split("  ")
        if len(words) == 1:
            p.append(f'<text x="{x+w//2}" y="{y+5}" text-anchor="middle" font-size="{fs}" '
                     f'font-weight="600" fill="{tcol}" font-family="{ff}">{esc(label)}</text>')
        else:
            p.append(f'<text x="{x+w//2}" y="{y-3}" text-anchor="middle" font-size="{fs}" '
                     f'font-weight="600" fill="{tcol}" font-family="{ff}">{esc(words[0])}</text>')
            p.append(f'<text x="{x+w//2}" y="{y+15}" text-anchor="middle" font-size="11.5" '
                     f'fill="{MUTED}" font-family="{FF}">{esc(words[1])}</text>')

    # start chip (deepwater)
    sx = pad
    chip(sx, yd, 150, start, DEEP, DEEP, MIST, mono=True)
    prev_r = sx + 150
    for i, (q, yes) in enumerate(steps):
        x = pad + 150 + gap + i*(colw+gap)
        # 'no' arrow into this decision
        p.append(f'<line x1="{prev_r+3}" y1="{yd}" x2="{x-6}" y2="{yd}" stroke="{LAKE}" '
                 f'stroke-width="2" marker-end="url(#a)"/>')
        if i > 0:
            p.append(f'<text x="{(prev_r+x)//2}" y="{yd-8}" text-anchor="middle" font-size="11" '
                     f'fill="{MUTED}">no</text>')
        # decision chip
        chip(x, yd, colw, q, "#eef6f7", LAKE, INK, h=62, fs=13.5)
        # 'yes' arrow up to outcome
        cx = x + colw//2
        p.append(f'<line x1="{cx}" y1="{yd-32}" x2="{cx}" y2="{yo+ch//2+3}" stroke="{GREEN}" '
                 f'stroke-width="2" marker-end="url(#ag)"/>')
        p.append(f'<text x="{cx+12}" y="{(yd+yo)//2}" font-size="11" fill="{GREEN}" '
                 f'font-weight="600">yes</text>')
        chip(x, yo, colw, yes, "#e4f5f1", CUR, INK, mono=True, fs=13.5)
        prev_r = x + colw
    # tail outcome (final 'no')
    tx = prev_r + gap
    p.append(f'<line x1="{prev_r+3}" y1="{yd}" x2="{tx-6}" y2="{yd}" stroke="{LAKE}" '
             f'stroke-width="2" marker-end="url(#a)"/>')
    p.append(f'<text x="{(prev_r+tx)//2}" y="{yd-8}" text-anchor="middle" font-size="11" fill="{MUTED}">no</text>')
    chip(tx, yd, 150, tail, "#f3f7f8", LINE, MUTED, mono=True)
    p.append("</svg>")
    (IMG/fname).write_text("\n".join(p), encoding="utf-8")
    print("wrote", fname, f"({W}x{H})")


cascade("provider = auto",
        [("local Ollama reachable?", "use  Ollama"),
         ("built-in extra installed?", "built-in  CPU model")],
        "skip  this tier",
        "provider-resolution.svg", "Provider auto-resolution", "provider = auto resolves to")

cascade("clean repo?",
        [("dirty working tree?", "skip  (stash if auto_stash)"),
         ("branches off a safe branch?", "leave  (protect_working_branches)")],
        "act  update / switch",
        "branch-safety.svg", "Branch-safety decision", "what happens to each repo")
