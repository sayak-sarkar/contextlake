#!/usr/bin/env python3
"""Bold simplified mark for 16/32 favicons: high-contrast otter silhouette +
glowing orb on the deepwater square. Drawn as vector primitives at 16x then
downsampled, so it stays crisp where the painterly downscale turned to mud."""
from PIL import Image, ImageDraw, ImageFilter
import math

OUT = "/home/sayak.sarkar/Work/web-redesign-lab/contextlake-ds"
DEEP = (14, 42, 51, 255)
SIL = (175, 206, 211, 255)   # light mist-teal otter silhouette (reads on deepwater)
SIL_D = (120, 158, 165, 255)  # shade
SUN = (231, 181, 60)
CUR = (43, 179, 163)


def rmask(S, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=255)
    return m


def glow_orb(S, cx, cy, rad):
    """Radial bright orb: sun core -> cyan -> transparent."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = layer.load()
    for y in range(int(cy - rad * 2.2), int(cy + rad * 2.2)):
        if y < 0 or y >= S:
            continue
        for x in range(int(cx - rad * 2.2), int(cx + rad * 2.2)):
            if x < 0 or x >= S:
                continue
            d = math.hypot(x - cx, y - cy) / rad
            if d <= 1.0:
                t = d
                r = int(SUN[0] * (1 - t) + CUR[0] * t)
                g = int(SUN[1] * (1 - t) + CUR[1] * t)
                b = int(SUN[2] * (1 - t) + CUR[2] * t)
                px[x, y] = (r, g, b, 255)
            elif d <= 2.1:
                a = int(150 * (1 - (d - 1) / 1.1))
                px[x, y] = (CUR[0], CUR[1], CUR[2], max(0, a))
    return layer.filter(ImageFilter.GaussianBlur(S * 0.006))


def make(size, ss=16, plate=True):
    S = size * ss
    c = Image.new("RGBA", (S, S), DEEP if plate else (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    cx = S * 0.5
    # body: rounded blob lower
    d.rounded_rectangle([S*0.23, S*0.44, S*0.77, S*0.82], radius=S*0.24, fill=SIL)
    # head: wide, flatter ellipse (otter, not bear)
    d.ellipse([S*0.24, S*0.20, S*0.76, S*0.56], fill=SIL)
    # ears: small, set wide and low on the head
    for ex in (0.30, 0.70):
        d.ellipse([S*ex - S*0.055, S*0.205, S*ex + S*0.055, S*0.30], fill=SIL)
        d.ellipse([S*ex - S*0.026, S*0.225, S*ex + S*0.026, S*0.275], fill=SIL_D)
    # muzzle: lighter rounded patch low-centre
    d.ellipse([S*0.39, S*0.36, S*0.61, S*0.55], fill=(207, 228, 231, 255))
    # eyes
    for ex in (0.41, 0.59):
        d.ellipse([S*ex - S*0.032, S*0.31, S*ex + S*0.032, S*0.385], fill=DEEP)
    # nose
    d.ellipse([cx - S*0.038, S*0.40, cx + S*0.038, S*0.455], fill=DEEP)
    # whiskers (help read 'otter' at 32/48)
    for sgn in (-1, 1):
        for wy in (0.45, 0.485):
            d.line([cx + sgn*S*0.06, S*wy, cx + sgn*S*0.20, S*(wy-0.02)],
                   fill=SIL_D, width=max(1, int(S*0.006)))
    # glowing orb held low-centre, overlapping body
    orb = glow_orb(S, cx, S*0.70, S*0.145)
    c = Image.alpha_composite(c, orb)
    # paws cupping the orb
    d2 = ImageDraw.Draw(c)
    for px_ in (0.39, 0.61):
        d2.ellipse([S*px_ - S*0.06, S*0.67, S*px_ + S*0.06, S*0.80], fill=SIL_D)
    if plate:
        # clip the orb glow / shapes to the rounded square
        base_alpha = c.split()[3]
        from PIL import ImageChops
        c.putalpha(ImageChops.multiply(base_alpha, rmask(S, int(S * 0.225))))
    return c.resize((size, size), Image.LANCZOS)


import os
FIN = f"{OUT}/icons-out"
os.makedirs(FIN, exist_ok=True)
# plated bold mark -> small favicons + nav glyph
for s in (16, 32, 48, 64):
    make(s, plate=True).save(f"{FIN}/icon-{s}.png", optimize=True)
# transparent bold mark -> footer / arch-core (sit on deepwater)
make(256, ss=4, plate=False).save(f"{FIN}/mark.png", optimize=True)

# review sheet
sheet = Image.new("RGBA", (16*8 + 32*5 + 48*4 + 64*3 + 110, 16*8 + 40), (235, 244, 244, 255))
x = 16
for s, sc in [(16, 8), (32, 5), (48, 4), (64, 3)]:
    up = Image.open(f"{FIN}/icon-{s}.png").resize((s*sc, s*sc), Image.NEAREST)
    sheet.alpha_composite(up, (x, 16)); x += s*sc + 24
# transparent mark on a deepwater swatch
sw = Image.new("RGBA", (160, 160), (14, 42, 51, 255))
sw.alpha_composite(Image.open(f"{FIN}/mark.png").resize((160, 160)))
sheet.alpha_composite(sw, (x, 8))
sheet.save(f"{OUT}/bold-sheet.png")
print("bold marks written to", FIN)
