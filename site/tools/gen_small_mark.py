#!/usr/bin/env python3
"""The small-size mark: the context-pebble on deepwater, for 16-64px favicons.

WHY THIS IS NOT THE MASCOT. Pebble is the identity at every size the format allows
(BRANDING.md 2.1), and 16 pixels is not one of those sizes. The previous small mark
tried anyway: a pale mist-teal otter on a deepwater-teal ground, which is a difference
in TONE, not in CONTRAST. Head, two ears, two eyes, a snout, a body and an orb -- six
shapes competing inside 256 pixels -- resolved to a single blue-grey mass in a tab
strip. The favicon slot got a picture of the mascot and showed the user nothing.

So the small tier renders the thing Pebble is holding: the context-pebble itself. It is
a detail of the same artwork rather than a second logo, which is why it can be legible
without being a different brand. One saturated teal disc on deepwater is the highest
contrast pair the palette contains, and a disc is the shape that survives downscaling
best -- it has no thin features to lose.

PROGRESSIVE DETAIL is the other half. Each size draws only what that size can hold:

    16, 32   the pebble and one gold glint. Nothing else fits, and BRANDING.md 2.3
             already asked for "a single glowing dot" here.
    48, 64   the constellation appears: three nodes and the edges between them, which
             is what contextlake actually does to a codebase.

Drawing one artwork and downscaling it is what produced the mud this replaces. Every
size here is composed at 8x and reduced once, so edges land on pixel boundaries.

Run: python3 site/tools/gen_small_mark.py
Writes into docs/img (override with CONTEXTLAKE_ASSET_OUT). The tracked copies live in
docs/img; site/ and site/img are gitignored and synced by site/build_docs.py.
"""
import math
import os
import pathlib

from PIL import Image, ImageDraw, ImageFilter

OUT = pathlib.Path(os.environ.get(
    "CONTEXTLAKE_ASSET_OUT",
    str(pathlib.Path(__file__).resolve().parents[2] / "docs/img")))

# Locked palette (BRANDING.md 1.2). Deepwater is never pure black and never
# transparent: a transparent favicon vanishes on a dark tab strip.
DEEP = (14, 42, 51, 255)      # deepwater  #0E2A33
TEAL = (43, 179, 163, 255)    # current    #2BB3A3
SUN = (231, 181, 60, 255)     # sun        #E7B53C
MIST = (214, 234, 236, 255)   # mist       #D6EAEC

SS = 8            # supersample factor: compose big, reduce once
RADIUS = 0.22     # squircle corner radius, as a fraction of the edge
ORB = 0.34        # orb radius, as a fraction of the edge


def _plate(S):
    """The deepwater squircle every size sits on."""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * RADIUS), fill=DEEP)
    return im


def _glow(S, cx, cy, r):
    """A soft teal halo, so the pebble sits in water rather than on top of it.

    Kept faint and strictly OUTSIDE the disc: the glow is atmosphere, and the moment
    it reaches across the disc's edge it softens the one boundary the whole mark
    depends on."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(12, 0, -1):
        rr = int(r * (1 + i * 0.075))
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                  fill=TEAL[:3] + (int(13 * i / 12),))
    return layer.filter(ImageFilter.GaussianBlur(S * 0.018))


def _constellation(d, S, cx, cy, r):
    """Three nodes and two edges: a graph, drawn small.

    Placed off-centre and rotated slightly so it reads as a fragment of something
    larger rather than a symmetrical ornament -- a symmetrical triangle reads as a
    'play' glyph or a caret."""
    pts = []
    for ang, dist in ((-118, 0.52), (-8, 0.60), (128, 0.46)):
        a = math.radians(ang)
        pts.append((cx + math.cos(a) * r * dist, cy + math.sin(a) * r * dist))
    # Drawn in DEEPWATER, not in mist, and that is a measurement rather than a taste.
    # Against the teal disc, deepwater is 5.78:1 while mist is 2.08:1 and the sun gold
    # is 1.37:1 -- so light-on-teal was the version that would have disappeared first
    # when the icon is scaled or shown on a low-quality display. Dark marks on a bright
    # disc also match how the painterly pebble reads: a lit stone with things inside it.
    lw = max(1, int(S * 0.018))
    for p, q in ((pts[0], pts[1]), (pts[1], pts[2])):
        d.line([p, q], fill=DEEP[:3] + (215,), width=lw)
    for i, p in enumerate(pts):
        nr = S * (0.042 if i == 1 else 0.034)
        d.ellipse([p[0] - nr, p[1] - nr, p[0] + nr, p[1] + nr], fill=DEEP)
    return pts[1]


def make(size, detail=None):
    """One icon at ``size``. ``detail`` overrides the size-driven choice."""
    if detail is None:
        detail = "full" if size >= 48 else "minimal"
    S = size * SS
    im = _plate(S)
    cx = cy = S / 2
    r = S * ORB

    im.alpha_composite(_glow(S, cx, cy, r))
    d = ImageDraw.Draw(im)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEAL)

    # A single light arc along the upper-left rim. One stroke is enough to say
    # "sphere"; a full gradient would only average back to flat once reduced.
    rim = int(S * 0.020)
    d.arc([cx - r + rim, cy - r + rim, cx + r - rim, cy + r - rim],
          start=185, end=265, fill=MIST[:3] + (150,), width=rim)

    if detail == "full":
        gx, gy = _constellation(d, S, cx, cy, r)
    else:
        # Off-centre so the mark has a direction even at 16px.
        gx, gy = cx + r * 0.16, cy - r * 0.20

    # The glint. Its halo is dropped below 48px: at 16 the glint is about two pixels
    # across, and a soft ring around two pixels does not read as a glow -- it averages
    # into the disc and turns a crisp dot into a yellow smudge.
    gr = S * (0.052 if detail == "full" else 0.060)
    if detail == "full":
        d.ellipse([gx - gr * 2.1, gy - gr * 2.1, gx + gr * 2.1, gy + gr * 2.1],
                  fill=SUN[:3] + (60,))
    # A deepwater collar under the glint. Sun gold on teal is only 1.37:1, so without
    # a dark edge the one warm accent in the palette washes into the disc; the collar
    # buys the separation that the hue difference alone does not.
    cr = gr * (1.5 if detail == "full" else 1.42)
    d.ellipse([gx - cr, gy - cr, gx + cr, gy + cr], fill=DEEP)
    d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=SUN)

    return im.resize((size, size), Image.LANCZOS)


def maskable(size=512, pad=0.10):
    """Android adaptive icon: the same mark inside a safe area, full-bleed ground.

    No rounded corners -- the launcher applies its own mask, and a squircle inside a
    squircle is the classic double-rounded artefact."""
    im = Image.new("RGBA", (size, size), DEEP)
    inner = make(int(size * (1 - pad * 2)))
    # Drop the inner tile's corners: it is sitting on its own colour anyway.
    im.alpha_composite(inner, (int(size * pad), int(size * pad)))
    return im


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for s in (16, 32, 48, 64):
        make(s).save(OUT / f"icon-{s}.png", optimize=True)
        print("wrote", OUT / f"icon-{s}.png")

    # A review sheet: real sizes, plus 16px magnified so the pixels are visible.
    zoom = 8
    cols = (16, 32, 48, 64)
    w = 40 + sum(cols) + 24 * len(cols) + 16 * zoom
    sheet = Image.new("RGBA", (w, 16 * zoom + 40), (242, 246, 246, 255))
    x = 20
    for s in cols:
        sheet.alpha_composite(make(s), (x, 20 + (16 * zoom - s) // 2))
        x += s + 24
    sheet.alpha_composite(make(16).resize((16 * zoom, 16 * zoom), Image.NEAREST), (x, 20))
    # The sheet is for judging a change, not for shipping: written only when an
    # output directory is named, so a plain run never drops it into docs/img.
    if os.environ.get("CONTEXTLAKE_ASSET_OUT"):
        sheet.save(OUT / "small-mark-sheet.png")
        print("wrote", OUT / "small-mark-sheet.png")


if __name__ == "__main__":
    main()
