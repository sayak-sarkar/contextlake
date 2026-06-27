#!/usr/bin/env python3
"""Two internals diagrams: config-precedence cascade (horizontal) and the
sync data flow (vertical). Brand-styled SVG."""
import pathlib
IMG = pathlib.Path("/home/sayak.sarkar/Work/contextlake/docs/img")
DEEP="#0E2A33"; LAKE="#137A8B"; CUR="#2BB3A3"; MIST="#EAF4F4"; INK="#0E2A33"
MUTED="#41606a"; LINE="#cfe0e3"
FF="'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif"
FM="'JetBrains Mono',ui-monospace,monospace"


def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def precedence(items, fname, title, sub):
    """Horizontal cascade; last item accented as the result."""
    cw, gap, h = 150, 34, 56
    n=len(items); pad=22
    W=pad*2+n*cw+(n-1)*gap; H=120
    cy=72
    p=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'role="img" aria-label="{esc(title)}" font-family="{FF}">',
       '<defs><marker id="f" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
       f'orient="auto"><path d="M0 0L10 5L0 10z" fill="{LAKE}"/></marker></defs>',
       f'<text x="{pad}" y="26" font-size="13" font-weight="600" fill="{MUTED}">{esc(sub)}</text>']
    for i,(label,note) in enumerate(items):
        x=pad+i*(cw+gap); last=i==n-1
        fill="#e4f5f1" if last else MIST; stroke=CUR if last else LINE
        p.append(f'<rect x="{x}" y="{cy-h//2}" width="{cw}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        p.append(f'<text x="{x+cw//2}" y="{cy-3}" text-anchor="middle" font-size="13.5" font-weight="600" fill="{INK}" font-family="{FM}">{esc(label)}</text>')
        if note:
            p.append(f'<text x="{x+cw//2}" y="{cy+15}" text-anchor="middle" font-size="10.5" fill="{MUTED}">{esc(note)}</text>')
        if i<n-1:
            ax=x+cw+3
            p.append(f'<line x1="{ax}" y1="{cy}" x2="{ax+gap-8}" y2="{cy}" stroke="{LAKE}" stroke-width="2" marker-end="url(#f)"/>')
    p.append("</svg>")
    (IMG/fname).write_text("\n".join(p),encoding="utf-8"); print("wrote",fname,f"({W}x{H})")


def vflow(stages, fname, title, sub):
    """Vertical flow; stages = [(label, note, kind)] kind: 'in'/'step'/'out'."""
    cw, ch, gap = 300, 50, 26
    pad=22; n=len(stages)
    W=pad*2+cw+120; H=46+n*ch+(n-1)*gap+pad
    cx=pad+ (W-2*pad-cw)//2 + 0
    x=(W-cw)//2
    p=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'role="img" aria-label="{esc(title)}" font-family="{FF}">',
       '<defs><marker id="v" viewBox="0 0 10 10" refX="5" refY="8" markerWidth="7" markerHeight="7" '
       f'orient="auto"><path d="M0 0L10 0L5 10z" fill="{LAKE}"/></marker></defs>',
       f'<text x="{x}" y="26" font-size="13" font-weight="600" fill="{MUTED}">{esc(sub)}</text>']
    y0=46
    for i,(label,note,kind) in enumerate(stages):
        y=y0+i*(ch+gap)
        if kind=="in": fill=DEEP; tcol=MIST; stroke=DEEP
        elif kind=="out": fill="#e4f5f1"; tcol=INK; stroke=CUR
        else: fill=MIST; tcol=INK; stroke=LINE
        p.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="11" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        ty = y+ch//2+5 if not note else y+ch//2-2
        p.append(f'<text x="{x+18}" y="{ty}" font-size="14.5" font-weight="600" fill="{tcol}" font-family="{FM}">{esc(label)}</text>')
        if note:
            p.append(f'<text x="{x+18}" y="{y+ch//2+15}" font-size="10.5" fill="{MUTED if kind!="in" else "#9fc2c9"}">{esc(note)}</text>')
        if i<n-1:
            mx=x+cw//2
            p.append(f'<line x1="{mx}" y1="{y+ch+3}" x2="{mx}" y2="{y+ch+gap-7}" stroke="{LAKE}" stroke-width="2" marker-end="url(#v)"/>')
    p.append("</svg>")
    (IMG/fname).write_text("\n".join(p),encoding="utf-8"); print("wrote",fname,f"({W}x{H})")


def snake(stages, cols, fname, title, sub):
    """2D serpentine flow: rows alternate L->R / R->L, joined by a vertical turn."""
    cw, gap, ch, rgap, pad = 146, 40, 50, 44, 20
    W = pad*2 + cols*cw + (cols-1)*gap
    nrows = (len(stages)+cols-1)//cols
    H = 42 + nrows*ch + (nrows-1)*rgap + pad
    def pos(i):
        r = i//cols; pir = i%cols
        c = pir if r % 2 == 0 else cols-1-pir
        return c, r
    def xy(i):
        c, r = pos(i)
        return pad + c*(cw+gap), 42 + r*(ch+rgap)
    aw = 6  # half-arrowhead size for explicit triangles
    def arrow(x1, y1, x2, y2):
        """line + an explicit filled triangle head (orientation-proof)."""
        out=[f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LAKE}" stroke-width="2.2"/>']
        if y1==y2 and x2>x1:   tri=f'{x2},{y2} {x2-2*aw},{y2-aw} {x2-2*aw},{y2+aw}'
        elif y1==y2:           tri=f'{x2},{y2} {x2+2*aw},{y2-aw} {x2+2*aw},{y2+aw}'
        else:                  tri=f'{x2},{y2} {x2-aw},{y2-2*aw} {x2+aw},{y2-2*aw}'  # down
        out.append(f'<polygon points="{tri}" fill="{LAKE}"/>')
        return "".join(out)
    p=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'role="img" aria-label="{esc(title)}" font-family="{FF}">',
       f'<text x="{pad}" y="26" font-size="12.5" font-weight="600" fill="{MUTED}">{esc(sub)}</text>']
    # connectors first (under chips)
    for i in range(len(stages)-1):
        x1,y1=xy(i); x2,y2=xy(i+1); cyl=y1+ch//2
        if y1==y2 and x2>x1:   p.append(arrow(x1+cw+4, cyl, x2-4, cyl))
        elif y1==y2:           p.append(arrow(x1-4, cyl, x2+cw+4, cyl))
        else:                  mx=x1+cw//2; p.append(arrow(mx, y1+ch+4, mx, y2-4))
    for i,(label,note,kind) in enumerate(stages):
        x,y=xy(i)
        if kind=="in": fill=DEEP; tcol=MIST; stroke=DEEP; ncol="#9fc2c9"
        elif kind=="out": fill="#e4f5f1"; tcol=INK; stroke=CUR; ncol=MUTED
        else: fill=MIST; tcol=INK; stroke=LINE; ncol=MUTED
        p.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" rx="11" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        p.append(f'<text x="{x+cw//2}" y="{y+ch//2-4}" text-anchor="middle" font-size="12.5" font-weight="600" fill="{tcol}" font-family="{FM}">{esc(label)}</text>')
        p.append(f'<text x="{x+cw//2}" y="{y+ch//2+12}" text-anchor="middle" font-size="9.5" fill="{ncol}">{esc(note)}</text>')
    p.append("</svg>")
    (IMG/fname).write_text("\n".join(p),encoding="utf-8"); print("wrote",fname,f"({W}x{H})")


precedence(
    [("defaults","built-in"),("~/.ini","global"),("./.ini","local"),("--config","custom file"),("CLI flags","effective")],
    "config-precedence.svg","Configuration precedence","each layer overrides the one before it")

snake(
    [("contextlake <cmd>","parse + dispatch","in"),
     ("fetch (glab)","accessible projects","step"),
     ("cache","projects .txt / .json","step"),
     ("scan workspace","find local .git","step"),
     ("compare","GitLab vs local","step"),
     ("git ops","clone / fetch / pull","step"),
     ("log & report","per-repo status","out")],
    4, "data-flow.svg", "Sync data flow", "what a sync command does")
